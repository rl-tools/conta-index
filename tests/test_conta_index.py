import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "conta_index.py"
SPEC = importlib.util.spec_from_file_location("conta_index", SCRIPT)
INDEX = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INDEX)


class IngestionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "tools").mkdir()
        shutil.copyfile(SCRIPT, self.root / "tools" / SCRIPT.name)
        (self.root / "shards").mkdir()
        self.version = self.root / "version" / "1"
        self.version.mkdir(parents=True)
        (self.version / "meta.json").write_text(json.dumps({
            "format": 1, "index": {"location": "generated/index.json"},
        }))
        (self.version / "shards.json").write_text(json.dumps({
            name: {"type": "http", "url": f"https://example.test/{name}/{{sha1}}"}
            for name in ("primary", "mirror")
        }))
        self.checkout = self.root / "checkout"
        self.data = self.checkout / "data"
        self.input = self.root / "example.bin"
        self.payload = b"a seekable blob\x00\xff" * 1000
        self.input.write_bytes(self.payload)
        self.sha1 = hashlib.sha1(self.payload).hexdigest()
        self.raw = self.data / self.sha1
        self.zipped = self.data / f"{self.sha1}.gz"

    def run_cli(self, *args, success=True):
        result = subprocess.run(
            [sys.executable, "-I", str(self.root / "tools" / SCRIPT.name), *map(str, args)],
            capture_output=True, text=True, cwd=self.root,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def ingest(self, *options):
        return self.run_cli("ingest", *options, "primary", self.checkout, self.input)

    def index(self):
        return json.loads((self.version / "generated" / "index.json").read_bytes())

    def snapshot(self):
        return {path: path.read_bytes() for path in (
            self.root / "shards" / "primary.json",
            self.version / "generated" / "index.json",
            self.version / "meta.json",
        )}

    def assert_scan_fails_without_publishing(self, message):
        before = self.snapshot()
        result = self.run_cli("scan", "primary", self.checkout, success=False)
        self.assertIn(message, result.stderr)
        self.assertEqual(before, self.snapshot())

    def test_default_ingestion_round_trips_empty_binary_and_large_files(self):
        empty = self.root / "empty.bin"
        empty.write_bytes(b"")
        large = self.root / "large.bin"
        large.write_bytes(bytes(range(256)) * 10000)
        self.run_cli("ingest", "primary", self.checkout, self.input, empty, large)
        for source in (self.input, empty, large):
            payload = source.read_bytes()
            sha1 = hashlib.sha1(payload).hexdigest()
            self.assertEqual((self.data / sha1).read_bytes(), payload)
            compressed = (self.data / f"{sha1}.gz").read_bytes()
            self.assertEqual(gzip.decompress(compressed), payload)
            self.assertEqual(compressed[3] & 8, 0)  # No original filename.
            self.assertEqual(compressed[4:8], b"\x00" * 4)  # Fixed timestamp.
        self.assertEqual(len(list(self.data.iterdir())), 6)
        for entry in self.index()["entries"]:
            self.assertEqual(entry["shards"], [{"id": "primary", "compressed": ["gz"]}])
        self.run_cli("check")

    def test_opt_out_then_reingest_backfills_without_changing_description(self):
        self.ingest("--no-gzip")
        self.assertFalse(self.zipped.exists())
        self.assertEqual(self.index()["entries"][0]["shards"], [{"id": "primary", "compressed": []}])
        metadata = (self.checkout / "metadata.json").read_bytes()
        raw_mtime = self.raw.stat().st_mtime_ns
        renamed = self.root / "renamed.bin"
        renamed.write_bytes(self.payload)
        self.run_cli("ingest", "primary", self.checkout, renamed)
        self.assertEqual(gzip.decompress(self.zipped.read_bytes()), self.payload)
        self.assertEqual((self.checkout / "metadata.json").read_bytes(), metadata)
        self.assertEqual(self.raw.stat().st_mtime_ns, raw_mtime)
        before = self.snapshot()
        zipped_mtime = self.zipped.stat().st_mtime_ns
        self.ingest()
        self.ingest("--no-gzip")
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.zipped.stat().st_mtime_ns, zipped_mtime)

    def test_mixed_availability_uses_shard_preference_not_ingestion_order(self):
        mirror = self.root / "mirror-checkout"
        self.run_cli("ingest", "--no-gzip", "mirror", mirror, self.input)
        self.ingest()
        entry = self.index()["entries"][0]
        self.assertEqual(entry["shards"], [
            {"id": "primary", "compressed": ["gz"]}, {"id": "mirror", "compressed": []},
        ])
        self.assertEqual(entry["sha256"], hashlib.sha256(self.payload).hexdigest())
        self.run_cli("check")

    def test_gzip_output_does_not_depend_on_input_name_or_checkout(self):
        self.ingest()
        renamed = self.root / "renamed.bin"
        renamed.write_bytes(self.payload)
        mirror = self.root / "mirror-checkout"
        self.run_cli("ingest", "mirror", mirror, renamed)
        self.assertEqual(self.zipped.read_bytes(), (mirror / "data" / self.zipped.name).read_bytes())

    def test_scan_discovers_existing_gzip_and_ingestion_preserves_it(self):
        self.ingest("--no-gzip")
        compressed = gzip.compress(self.payload, compresslevel=1, mtime=123)
        self.zipped.write_bytes(compressed)
        self.run_cli("scan", "primary", self.checkout)
        self.assertEqual(self.index()["entries"][0]["shards"][0]["compressed"], ["gz"])
        self.ingest()
        self.assertEqual(self.zipped.read_bytes(), compressed)

    def test_scan_rejects_corrupt_or_mismatched_gzip_without_publishing(self):
        self.ingest()
        valid = self.zipped.read_bytes()
        wrong_crc = bytearray(valid)
        wrong_crc[-8] ^= 1
        for compressed in (b"", b"not gzip", valid[:-4], bytes(wrong_crc), gzip.compress(b"wrong content")):
            with self.subTest(compressed=compressed[:10]):
                self.zipped.write_bytes(compressed)
                self.assert_scan_fails_without_publishing(str(self.zipped))

    def test_empty_file_is_not_a_valid_gzip_companion_for_an_empty_blob(self):
        self.input.write_bytes(b"")
        self.ingest()
        empty_sha1 = hashlib.sha1(b"").hexdigest()
        (self.data / f"{empty_sha1}.gz").write_bytes(b"")
        self.assert_scan_fails_without_publishing("not a gzip file")

    def test_missing_advertised_gzip_is_rejected(self):
        self.ingest()
        self.zipped.unlink()
        self.assert_scan_fails_without_publishing("advertised gzip is missing")

    def test_orphan_gzip_is_rejected(self):
        self.ingest()
        self.raw.unlink()
        self.assert_scan_fails_without_publishing("matching raw blob")

    def test_missing_raw_only_blob_is_rejected(self):
        self.ingest("--no-gzip")
        self.raw.unlink()
        self.assert_scan_fails_without_publishing("shards are append-only")

    def test_known_raw_content_is_reverified(self):
        self.ingest()
        self.raw.write_bytes(b"changed")
        self.assert_scan_fails_without_publishing("content hashes to")

    def test_ingestion_rejects_corrupt_existing_raw_before_compressing(self):
        self.ingest("--no-gzip")
        self.raw.write_bytes(b"changed")
        before = self.snapshot()
        result = self.run_cli("ingest", "primary", self.checkout, self.input, success=False)
        self.assertIn("does not match expected", result.stderr)
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.zipped.exists())
        self.assertEqual(self.raw.read_bytes(), b"changed")

    def test_ingestion_does_not_rewrite_corrupt_existing_gzip(self):
        self.ingest()
        self.zipped.write_bytes(b"corrupt")
        before = self.snapshot()
        result = self.run_cli("ingest", "primary", self.checkout, self.input, success=False)
        self.assertIn("not a gzip file", result.stderr)
        self.assertEqual(self.zipped.read_bytes(), b"corrupt")
        self.assertEqual(before, self.snapshot())

    def test_lfs_pointers_are_rejected_for_both_representations(self):
        self.ingest()
        pointer = b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 10\n"
        for path in (self.raw, self.zipped):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(pointer)
                self.assert_scan_fails_without_publishing("git lfs pull")
                path.write_bytes(original)

    def test_invalid_source_compressed_arrays_are_rejected(self):
        self.ingest()
        source = self.root / "shards" / "primary.json"
        entries = json.loads(source.read_bytes())
        for compression in (True, "gz", None, {}, [""], ["gz", "gz"], ["gz", 1], [[]], [".gz"], ["../gz"], ["GZ"], ["gz?x"]):
            with self.subTest(compression=compression):
                entries[0]["compressed"] = compression
                source.write_text(json.dumps(entries))
                result = self.run_cli("check", success=False)
                self.assertIn("compressed must be an array of unique lowercase alphanumeric extensions", result.stderr)

    def test_algorithm_name_requires_the_extension_token(self):
        self.ingest()
        source = self.root / "shards" / "primary.json"
        entries = json.loads(source.read_bytes())
        entries[0]["compressed"] = ["gzip"]
        source.write_text(json.dumps(entries))
        result = self.run_cli("check", success=False)
        self.assertIn("use gz instead of gzip in compressed", result.stderr)

    def test_missing_source_compressed_means_raw_only(self):
        self.ingest("--no-gzip")
        source = self.root / "shards" / "primary.json"
        entries = json.loads(source.read_bytes())
        entries[0].pop("compressed")
        source.write_text(json.dumps(entries))
        self.run_cli("build")
        self.assertEqual(self.index()["entries"][0]["shards"], [{"id": "primary", "compressed": []}])
        self.run_cli("check")

    def test_scan_preserves_future_compressions_when_adding_gzip(self):
        self.ingest("--no-gzip")
        source = self.root / "shards" / "primary.json"
        entries = json.loads(source.read_bytes())
        entries[0]["compressed"] = ["zst"]
        source.write_text(json.dumps(entries))
        self.run_cli("build")
        self.assertEqual(self.index()["entries"][0]["shards"][0]["compressed"], ["zst"])
        self.zipped.write_bytes(gzip.compress(self.payload))
        self.run_cli("scan", "primary", self.checkout)
        self.run_cli("scan", "primary", self.checkout)
        self.assertEqual(json.loads(source.read_bytes())[0]["compressed"], ["zst", "gz"])
        self.assertEqual(self.index()["entries"][0]["shards"][0]["compressed"], ["zst", "gz"])
        self.run_cli("check")
        self.zipped.unlink()
        self.assert_scan_fails_without_publishing("advertised gzip is missing")

    def test_legacy_compression_fields_are_not_silently_dropped(self):
        self.ingest()
        source = self.root / "shards" / "primary.json"
        entries = json.loads(source.read_bytes())
        entries[0].pop("compressed")
        for field, value, message in (
            ("gzip", True, "replace the gzip boolean with a compressed array"),
            ("compression", ["gz"], "rename compression to compressed"),
        ):
            with self.subTest(field=field):
                entries[0][field] = value
                source.write_text(json.dumps(entries))
                result = self.run_cli("check", success=False)
                self.assertIn(message, result.stderr)
                entries[0].pop(field)

    def test_interrupted_compression_never_publishes_partial_gzip(self):
        self.ingest("--no-gzip")

        def interrupt(source, output, **kwargs):
            output.write(b"partial")
            raise OSError("interrupted")

        with mock.patch.object(INDEX.shutil, "copyfileobj", side_effect=interrupt):
            with self.assertRaisesRegex(OSError, "interrupted"):
                INDEX.store_blob(self.raw, self.zipped, INDEX.digests(self.raw), compressed=True)
        self.assertFalse(self.zipped.exists())
        self.assertEqual(list(self.data.iterdir()), [self.raw])

    def test_concurrent_publication_never_overwrites_existing_gzip(self):
        self.ingest("--no-gzip")
        for winner in (gzip.compress(self.payload, mtime=123), b"corrupt"):
            with self.subTest(valid=winner != b"corrupt"):
                def publish_first(source, destination):
                    destination.write_bytes(winner)
                    raise FileExistsError(destination)

                with mock.patch.object(INDEX.os, "link", side_effect=publish_first):
                    with contextlib.redirect_stderr(io.StringIO()):
                        if winner == b"corrupt":
                            with self.assertRaises(SystemExit):
                                INDEX.store_blob(self.raw, self.zipped, INDEX.digests(self.raw), compressed=True)
                        else:
                            INDEX.store_blob(self.raw, self.zipped, INDEX.digests(self.raw), compressed=True)
                self.assertEqual(self.zipped.read_bytes(), winner)
                self.assertEqual(len(list(self.data.iterdir())), 2)
                self.zipped.unlink()


if __name__ == "__main__":
    unittest.main()
