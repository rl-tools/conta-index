#!/usr/bin/env python3
"""scan: add the blobs of a shard checkout (<checkout>/data/<sha1>, descriptions from an optional metadata.json) to shards/<shard>.json, then build.
build: combine shards.json and shards/*.json into generated/index.json and write its sha256 into meta.json. check: validate everything and fail if the generated files are stale or hand-edited."""

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
META = ROOT / "meta.json"
SHARD_TABLE = ROOT / "shards.json"
SHARDS = ROOT / "shards"
INDEX = ROOT / "generated" / "index.json"
INDEX_LOCATION = INDEX.relative_to(ROOT).as_posix()
SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
LFS_POINTER = b"version https://git-lfs"


def fail(message):
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def warn(message):
    print(f"warning: {message}", file=sys.stderr)


def lines_json(items):
    return "[\n" + ",\n".join(json.dumps(item, ensure_ascii=False) for item in items) + "\n]"


def load_meta():
    meta = json.loads(META.read_text())
    if meta.get("format") != 1 or (meta.get("index") or {}).get("location") != INDEX_LOCATION:
        fail(f"meta.json: expected format 1 and index.location \"{INDEX_LOCATION}\"")
    return meta


def load_shard_table():
    shards = json.loads(SHARD_TABLE.read_text())
    if not shards:
        fail("shards.json: no shards")
    for shard, spec in shards.items():
        url = spec.get("url", "")
        if not spec.get("type") or ("{sha1}" not in url and "{sha256}" not in url):
            fail(f"shards.json: shard {shard} needs a type and a url with {{sha1}} or {{sha256}}")
    return shards


def load_shard(shard):
    path = SHARDS / f"{shard}.json"
    entries = json.loads(path.read_text()) if path.exists() else []
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
    (SHARDS / f"{shard}.json").write_text(lines_json(entries) + "\n")


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


def generate():
    meta = load_meta()
    entries, text = combine(load_shard_table())
    meta["index"]["sha256"] = hashlib.sha256(text.encode()).hexdigest()
    return entries, text, json.dumps(meta, indent=2) + "\n"


def build():
    entries, text, meta_text = generate()
    INDEX.parent.mkdir(exist_ok=True)
    INDEX.write_text(text)
    META.write_text(meta_text)
    print(f"{INDEX}: {len(entries)} entries, sha256 {meta_text.split(chr(34))[-2][:12]}… written to meta.json")


def check():
    entries, text, meta_text = generate()
    if not INDEX.exists() or INDEX.read_text() != text:
        fail(f"{INDEX_LOCATION} is stale or hand-edited: run tools/conta_index.py build")
    if META.read_text() != meta_text:
        fail("meta.json index.sha256 is stale: run tools/conta_index.py build")
    print(f"ok: {len(json.loads(SHARD_TABLE.read_text()))} shards, {len(entries)} entries")


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
    for entry in json.loads(path.read_text()):
        sha1 = str(entry.get("hash", "")).lower()
        if not SHA1.match(sha1):
            warn(f"{path}: invalid hash {entry.get('hash')!r}")
        elif sha1 in descriptions:
            warn(f"{path}: {sha1} listed twice ({descriptions[sha1]!r}, {entry.get('description')!r}); keeping the first")
        else:
            descriptions[sha1] = str(entry.get("description", ""))
    return descriptions


def scan(shard, checkout):
    if shard not in load_shard_table():
        fail(f"{shard} is not in shards.json")
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
