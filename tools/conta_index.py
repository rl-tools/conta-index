#!/usr/bin/env python3
"""scan: add the blobs of a shard checkout (<checkout>/data/<sha1>, descriptions from an optional metadata.json) to shards/<shard>.json, then build.
build: for every version/<n>/, combine its shards.json with the shared shards/*.json into version/<n>/generated/index.json and write its sha256 into version/<n>/meta.json.
check: validate everything and fail if any generated file is stale, hand-edited or re-encoded."""

import hashlib
import json
import re
import sys
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
    previous = ""
    for entry in entries:
        sha1 = entry.get("sha1", "")
        if not SHA1.match(sha1) or not SHA256.match(entry.get("sha256", "")):
            fail(f"{path}: bad digests in {entry}")
        if sha1 <= previous:
            fail(f"{path}: {sha1} out of order or duplicated")
        previous = sha1
    return entries


def save_shard(shard, entries):
    entries.sort(key=lambda entry: entry["sha1"])
    (SHARDS / f"{shard}.json").write_bytes((lines_json(entries) + "\n").encode("utf-8"))


def combine(shards):
    combined = {}
    for shard in shards:
        for entry in load_shard(shard):
            existing = combined.get(entry["sha1"])
            if existing is None:
                combined[entry["sha1"]] = {"sha1": entry["sha1"], "sha256": entry["sha256"], "shards": [shard], "description": entry.get("description", "")}
            else:
                if existing["sha256"] != entry["sha256"]:
                    fail(f"{entry['sha1']}: sha256 differs between shards {existing['shards'][0]} and {shard}")
                if entry.get("description") and existing["description"] and entry["description"] != existing["description"]:
                    warn(f"{entry['sha1']}: description differs between shards ({existing['description']!r} from {existing['shards'][0]}, {entry['description']!r} from {shard})")
                existing["shards"].append(shard)
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


def digests(path):
    sha1, sha256 = hashlib.sha1(), hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            sha1.update(chunk)
            sha256.update(chunk)
    return sha1.hexdigest(), sha256.hexdigest()


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


def scan(shard, checkout):
    if not any(shard in load_shard_table(directory) for directory in versions().values()):
        fail(f"{shard} is in no version/<n>/shards.json")
    checkout = Path(checkout).expanduser().resolve()
    data = checkout / "data"
    if not data.is_dir():
        fail(f"{data} is not a directory")
    descriptions = descriptions_from_metadata(checkout)
    entries = {entry["sha1"]: entry for entry in load_shard(shard)}
    on_disk = set()
    for path in sorted(data.iterdir()):
        name = path.name
        if not path.is_file() or not SHA1.match(name):
            warn(f"{path}: not a sha1-named blob, skipped")
            continue
        if path.stat().st_size < 1024 and path.open("rb").read(len(LFS_POINTER)) == LFS_POINTER:
            fail(f"{path} is a git-lfs pointer, run \"git lfs pull\" in {checkout}")
        on_disk.add(name)
        if name not in entries:
            sha1, sha256 = digests(path)
            if sha1 != name:
                fail(f"{path}: content hashes to {sha1}")
            entries[name] = {"sha1": sha1, "sha256": sha256, "description": ""}
        if name in descriptions:
            entries[name]["description"] = descriptions[name]
    for sha1 in sorted(set(descriptions) - on_disk):
        warn(f"metadata.json lists {sha1} ({descriptions[sha1]}) but {data / sha1} does not exist")
    for sha1 in sorted(set(entries) - on_disk):
        fail(f"{sha1} is in shards/{shard}.json but missing from {data}: shards are append-only")
    save_shard(shard, list(entries.values()))
    print(f"{SHARDS / f'{shard}.json'}: {len(entries)} entries")
    build()


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "scan":
        scan(sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 2 and sys.argv[1] in ("build", "check"):
        globals()[sys.argv[1]]()
    else:
        print(f"usage: {sys.argv[0]} scan <shard> <checkout> | build | check", file=sys.stderr)
        sys.exit(1)
