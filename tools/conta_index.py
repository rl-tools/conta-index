#!/usr/bin/env python3
"""ingest: store files as <checkout>/data/<sha1> and <sha1>.gz by default, append descriptions to metadata.json, then scan.
scan: verify a shard checkout's blobs and optional gzip companions, update shards/<shard>.json, then build.
build: for every version/<n>/, combine its shards.json with the shared shards/*.json into version/<n>/generated/index.json and write its sha256 into version/<n>/meta.json.
check: validate everything and fail if any generated file is stale, hand-edited or re-encoded."""

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHARDS = ROOT / "shards"
VERSIONS = ROOT / "version"
CURRENT = ROOT / "current.json"
INDEX_LOCATION = "generated/index.json"
VERSION_NAME = re.compile(r"^[1-9][0-9]*$")
SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
LFS_POINTER = b"version https://git-lfs"


def fail(message):
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def warn(message):
    print(f"warning: {message}", file=sys.stderr)


def read_json(path):
    return json.loads(path.read_bytes().decode("utf-8"))


def lines_json(items):
    return "[\n" + ",\n".join(json.dumps(item, ensure_ascii=False) for item in items) + "\n]"


def load_meta(directory, version):
    path = directory / "meta.json"
    meta = read_json(path)
    if type(meta.get("format")) is not int or meta["format"] != version or (meta.get("index") or {}).get("location") != INDEX_LOCATION:
        fail(f"{path}: expected format {version} and index.location \"{INDEX_LOCATION}\"")
    return meta


def load_shard_table(directory):
    path = directory / "shards.json"
    shards = read_json(path)
    if not shards:
        fail(f"{path}: no shards")
    for shard, spec in shards.items():
        url = spec.get("url", "")
        if not spec.get("type") or ("{sha1}" not in url and "{sha256}" not in url):
            fail(f"{path}: shard {shard} needs a type and a url with {{sha1}} or {{sha256}}")
    return shards


def load_shard(shard):
    path = SHARDS / f"{shard}.json"
    entries = read_json(path) if path.exists() else []
    seen = set()
    for entry in entries:
        sha1 = entry.get("sha1", "")
        if not SHA1.match(sha1) or not SHA256.match(entry.get("sha256", "")):
            fail(f"{path}: bad digests in {entry}")
        if sha1 in seen:
            fail(f"{path}: {sha1} is listed twice")
        if type(entry.get("gzip", False)) is not bool:
            fail(f"{path}: gzip must be a boolean for {sha1}")
        seen.add(sha1)
    return entries


def shard_order(entry):
    description = entry.get("description", "")
    parts = re.split(r"([0-9]+)", description)
    return [(1, int(part)) if part.isdigit() else (0, part.casefold()) for part in parts], description, entry["sha1"]


def save_shard(shard, entries):
    entries.sort(key=shard_order)
    (SHARDS / f"{shard}.json").write_bytes((lines_json(entries) + "\n").encode("utf-8"))


def combine(shards):
    combined = {}
    for shard in shards:
        for entry in load_shard(shard):
            location = {"id": shard, "gzip": entry.get("gzip", False)}
            existing = combined.get(entry["sha1"])
            if existing is None:
                combined[entry["sha1"]] = {"sha1": entry["sha1"], "sha256": entry["sha256"], "shards": [location], "description": entry.get("description", "")}
            else:
                if existing["sha256"] != entry["sha256"]:
                    fail(f"{entry['sha1']}: sha256 differs between shards {existing['shards'][0]['id']} and {shard}")
                if entry.get("description") and existing["description"] and entry["description"] != existing["description"]:
                    warn(f"{entry['sha1']}: description differs between shards ({existing['description']!r} from {existing['shards'][0]['id']}, {entry['description']!r} from {shard})")
                existing["shards"].append(location)
                existing["description"] = existing["description"] or entry.get("description", "")
    entries = [combined[sha1] for sha1 in sorted(combined)]
    for entry in entries:
        if not entry["description"]:
            warn(f"{entry['sha1']}: no description")
    table = ",\n".join(f"{json.dumps(shard)}: {json.dumps(spec, ensure_ascii=False)}" for shard, spec in shards.items())
    return entries, "{\n\"shards\": {\n" + table + "\n},\n\"entries\": " + lines_json(entries) + "\n}\n"


def generate_format_1(directory):
    meta = load_meta(directory, 1)
    entries, text = combine(load_shard_table(directory))
    index_bytes = text.encode("utf-8")
    meta["index"]["sha256"] = hashlib.sha256(index_bytes).hexdigest()
    return entries, index_bytes, (json.dumps(meta, indent=2) + "\n").encode("utf-8"), meta["index"]["sha256"]


GENERATORS = {1: generate_format_1}


def versions():
    found = {}
    for directory in sorted(VERSIONS.iterdir()):
        if directory.name.startswith("."):
            continue
        if not directory.is_dir() or not VERSION_NAME.match(directory.name):
            fail(f"{directory}: everything in version/ is a directory named by a positive integer")
        found[int(directory.name)] = directory
    if not found:
        fail(f"{VERSIONS} contains no versions")
    for version in found:
        if version not in GENERATORS:
            fail(f"{found[version]}: tools/conta_index.py has no generator for version {version}")
    return dict(sorted(found.items()))


def build():
    for version, directory in versions().items():
        entries, index_bytes, meta_bytes, index_sha256 = GENERATORS[version](directory)
        index = directory / INDEX_LOCATION
        index.parent.mkdir(exist_ok=True)
        index.write_bytes(index_bytes)
        (directory / "meta.json").write_bytes(meta_bytes)
        print(f"{index}: {len(entries)} entries, sha256 {index_sha256[:12]}… written to {directory / 'meta.json'}")


def check():
    available = versions()
    for version, directory in available.items():
        entries, index_bytes, meta_bytes, index_sha256 = GENERATORS[version](directory)
        index = directory / INDEX_LOCATION
        if not index.exists() or index.read_bytes() != index_bytes:
            fail(f"{index} is stale, hand-edited or re-encoded (line endings, encoding): run tools/conta_index.py build")
        if (directory / "meta.json").read_bytes() != meta_bytes:
            fail(f"{directory / 'meta.json'}: index.sha256 is stale, or the file was re-encoded: run tools/conta_index.py build")
        print(f"ok: version {version}, {len(read_json(directory / 'shards.json'))} shards, {len(entries)} entries, index sha256 {index_sha256[:12]}…")
    if CURRENT.exists():
        current = read_json(CURRENT).get("version")
        if type(current) is not int or current not in available:
            fail(f"{CURRENT}: version {current!r} is not one of {list(available)}")


def digests(path, *, compressed=False):
    """Hash original bytes, rejecting unmaterialized LFS files and invalid gzip."""
    sha1, sha256 = hashlib.sha1(), hashlib.sha256()
    try:
        with open(path, "rb") as file:
            prefix = file.read(len(LFS_POINTER))
        if prefix == LFS_POINTER:
            fail(f'{path} is a git-lfs pointer, run "git lfs pull" in the shard checkout')
        if compressed and not prefix.startswith(b"\x1f\x8b"):
            fail(f"{path}: not a gzip file")
        with (gzip.open if compressed else open)(path, "rb") as file:
            for chunk in iter(lambda: file.read(1 << 20), b""):
                sha1.update(chunk)
                sha256.update(chunk)
    except (OSError, EOFError, zlib.error) as error:
        fail(f"{path}: cannot read {'gzip blob' if compressed else 'blob'}: {error}")
    return sha1.hexdigest(), sha256.hexdigest()


def verify_blob(path, expected, *, compressed=False):
    if digests(path, compressed=compressed) != expected:
        fail(f"{path}: {'decompressed content' if compressed else 'content'} does not match expected sha1/sha256 {expected}")


def store_blob(source, destination, expected, *, compressed=False):
    """Publish only complete, verified files; never overwrite an existing blob."""
    if destination.exists():
        verify_blob(destination, expected, compressed=compressed)
        return
    with tempfile.TemporaryDirectory(prefix=".ingest-", dir=destination.parent) as staging:
        temporary = Path(staging) / destination.name
        if compressed:
            with open(source, "rb") as original, open(temporary, "wb") as output:
                with gzip.GzipFile(filename="", mode="wb", fileobj=output, compresslevel=9, mtime=0) as zipped:
                    shutil.copyfileobj(original, zipped, length=1 << 20)
        else:
            shutil.copyfile(source, temporary)
        verify_blob(temporary, expected, compressed=compressed)
        try:
            # Same-filesystem hard link publishes atomically without replacing a
            # blob another ingestion may have created while we were working.
            os.link(temporary, destination)
        except FileExistsError:
            verify_blob(destination, expected, compressed=compressed)


def descriptions_from_metadata(checkout):
    path = checkout / "metadata.json"
    if not path.exists():
        return {}
    descriptions = {}
    for entry in read_json(path):
        sha1 = str(entry.get("hash", "")).lower()
        if not SHA1.match(sha1):
            warn(f"{path}: invalid hash {entry.get('hash')!r}")
        elif sha1 in descriptions:
            warn(f"{path}: {sha1} listed twice ({descriptions[sha1]!r}, {entry.get('description')!r}); keeping the first")
        else:
            descriptions[sha1] = str(entry.get("description", ""))
    return descriptions


def require_listed(shard):
    if not any(shard in load_shard_table(directory) for directory in versions().values()):
        fail(f"{shard} is in no version/<n>/shards.json")


def description_for(file):
    path = Path(file)
    return path.name if path.is_absolute() or ".." in path.parts else path.as_posix()


def append_metadata(path, additions):
    """Append entries in the file's one-per-line style, aligning "hash" with the last entry, so the hand-kept formatting survives."""
    text = path.read_bytes().decode("utf-8") if path.exists() else "[\n]\n"
    end = text.rfind("]")
    if end < 0:
        fail(f"{path}: not a JSON array")
    head = text[:end].rstrip()
    last_line = head.split("\n")[-1]
    hash_column = last_line.find('"hash"') if last_line.lstrip().startswith("{") else -1
    lines = []
    for description, sha1 in additions:
        prefix = '{"description": ' + json.dumps(description, ensure_ascii=False) + ","
        lines.append(prefix + " " * max(hash_column - len(prefix), 1) + '"hash": "' + sha1 + '"}')
    separator = "\n" if head.endswith("[") else ",\n"
    path.write_bytes((head + separator + ",\n".join(lines) + "\n" + text[end:]).encode("utf-8"))
    read_json(path)


def ingest(shard, checkout, files, *, compress=True):
    require_listed(shard)
    checkout = Path(checkout).expanduser().resolve()
    data = checkout / "data"
    data.mkdir(parents=True, exist_ok=True)
    descriptions = descriptions_from_metadata(checkout)
    additions = []
    for file in files:
        if not Path(file).is_file():
            fail(f"{file} is not a file")
        expected = digests(file)
        sha1 = expected[0]
        blob = data / sha1
        store_blob(file, blob, expected)
        if compress:
            store_blob(blob, data / f"{sha1}.gz", expected, compressed=True)
        description = description_for(file)
        if sha1 in descriptions:
            if descriptions[sha1] != description:
                warn(f"{file}: {sha1} is already described as {descriptions[sha1]!r}, keeping that")
            continue
        descriptions[sha1] = description
        additions.append((description, sha1))
    if additions:
        append_metadata(checkout / "metadata.json", additions)
    scan(shard, checkout)


def scan(shard, checkout):
    require_listed(shard)
    checkout = Path(checkout).expanduser().resolve()
    data = checkout / "data"
    if not data.is_dir():
        fail(f"{data} is not a directory")
    descriptions = descriptions_from_metadata(checkout)
    entries = {entry["sha1"]: entry for entry in load_shard(shard)}
    on_disk = set()
    for path in sorted(data.iterdir()):
        name = path.name
        if name.endswith(".gz") and SHA1.fullmatch(name[:-3]):
            if not path.is_file() or not (data / name[:-3]).is_file():
                fail(f"{path}: gzip companions require a file and a matching raw blob")
            continue  # Verified together with the raw blob below.
        if not path.is_file() or not SHA1.fullmatch(name):
            warn(f"{path}: not a sha1-named blob, skipped")
            continue
        on_disk.add(name)
        sha1, sha256 = digests(path)
        if sha1 != name:
            fail(f"{path}: content hashes to {sha1}")
        if name not in entries:
            entries[name] = {"sha1": sha1, "sha256": sha256, "description": ""}
        elif entries[name]["sha256"] != sha256:
            fail(f"{path}: sha256 differs from shards/{shard}.json")
        companion = data / f"{name}.gz"
        if companion.exists():
            verify_blob(companion, (sha1, sha256), compressed=True)
            entries[name]["gzip"] = True
        elif entries[name].get("gzip", False):
            fail(f"{companion}: advertised gzip is missing; shards are append-only")
        if name in descriptions:
            entries[name]["description"] = descriptions[name]
    for sha1 in sorted(set(descriptions) - on_disk):
        warn(f"metadata.json lists {sha1} ({descriptions[sha1]}) but {data / sha1} does not exist")
    for sha1 in sorted(set(entries) - on_disk):
        fail(f"{sha1} is in shards/{shard}.json but missing from {data}: shards are append-only")
    save_shard(shard, list(entries.values()))
    print(f"{SHARDS / f'{shard}.json'}: {len(entries)} entries")
    build()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    ingest_parser = commands.add_parser("ingest", help="store raw blobs and gzip companions, then scan")
    ingest_parser.add_argument("--no-gzip", action="store_true", help="do not create gzip companions (existing copies are still verified)")
    ingest_parser.add_argument("shard")
    ingest_parser.add_argument("checkout")
    ingest_parser.add_argument("files", nargs="+")
    scan_parser = commands.add_parser("scan", help="verify blobs and gzip companions, then build")
    scan_parser.add_argument("shard")
    scan_parser.add_argument("checkout")
    commands.add_parser("build", help="rebuild all index versions")
    commands.add_parser("check", help="validate generated indexes")
    args = parser.parse_args()
    if args.command == "ingest":
        ingest(args.shard, args.checkout, args.files, compress=not args.no_gzip)
    elif args.command == "scan":
        scan(args.shard, args.checkout)
    else:
        globals()[args.command]()


if __name__ == "__main__":
    main()
