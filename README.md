# conta-index

Which shard holds each conta blob. Consumers never read this repository directly; the client hardcodes the entry points below and reads `meta.json` of the version it implements. Blobs are immutable and verified by hash, so the index is trusted for availability only.

- `version/<n>/` — one directory per contract version. Version 1 has no released consumers yet and can change in place. Once released, every version stays published and keeps being generated from the shared shard files for as long as the store exists, so a client hardcodes its version; extensions then go into a new version. `tools/conta_index.py` has one generator per version and refuses a version directory it has no generator for.
- `version/<n>/meta.json` — hand-written except `index.sha256`: `format` (equal to `<n>`), `index` (`location`, and `sha256` of the generated index written by `build`).
- `version/<n>/shards.json` — hand-written shard table, id → `{type, url, repo}` in order of preference. `url` is the http transport, a template with `{sha1}` or `{sha256}`, so a changed download location is an edit here and no client update. `type` pre-empts transports where inserting a hash into a url is not enough; a new type needs a client that knows it. `repo` is informational.
- `shards/<id>.json` — source of truth per shard, shared by all versions: entries `{sha1, sha256, description}` with an optional boolean `gzip` (absent means false), sorted by description with numbers compared by value (`Train-2` before `Train-10`), one per line; `scan` restores the order. Run `tools/conta_index.py scan <id> <checkout>` to verify every `data/<sha1>`, verify any `data/<sha1>.gz` decompresses to the same SHA-1 and SHA-256, record `gzip: true` for verified companions, take descriptions from the checkout's optional `metadata.json`, and rebuild. Scanning discovers companions; it does not create them.
- `version/<n>/generated/index.json` — written by `tools/conta_index.py build`, never edited: the shard table plus `entries` `{sha1, sha256, shards, description}` merged across shards. Each entry's `shards` is an ordered list of `{id, gzip}` locations; raw bytes are available at every listed location, and `gzip: true` advertises an additional gzip copy there. Committed so the raw GitHub URL serves it; `tools/conta_index.py check` (also the CI workflow) fails when it or `index.sha256` does not match the sources.

A shard stores raw blobs named by their SHA-1, optionally accompanied by `<sha1>.gz`. Both digests always identify the original, uncompressed bytes. Never delete or rewrite a raw blob or an advertised gzip companion; the index is append-only, so a newer index never has fewer entries or advertised copies. `scan` rejects corrupt files, orphan gzip companions, and missing previously indexed copies.

To ingest files into a new shard `<name>`, add `<name>` to `version/1/shards.json` with the url template under which `<checkout>/data` will be hosted, then run from the repository root:

```
python3 -I tools/conta_index.py ingest <name> <checkout> <files>
```

It copies each file to `<checkout>/data/<sha1>` and creates `<checkout>/data/<sha1>.gz` by default. Compression streams at gzip level 9 with no embedded filename or timestamp. New files are verified and published atomically; existing copies are verified and never overwritten. It appends the file's path as given (just the file name for an absolute path) as its description to `<checkout>/metadata.json`, creates `shards/<name>.json` and rebuilds the index. A blob already described in `metadata.json` keeps its description. Re-ingesting it creates a missing gzip companion, so the same command can backfill previously ingested files.

To ingest raw copies only, use `python3 -I tools/conta_index.py ingest --no-gzip <name> <checkout> <files>`. This still verifies and advertises any existing gzip companions. Files that have not been backfilled remain raw-only; there is no shard-wide assumption that gzip is available.

Upload `<checkout>/data` and `<checkout>/metadata.json` to the host before pushing the index. Serve `.gz` companions as gzip files (for example, `Content-Type: application/gzip`), without `Content-Encoding: gzip`; clients explicitly decompress them.

## Client contract

Entry points, tried in order:

1. `https://conta.rl.tools/version/1/meta.json`
2. `https://raw.githubusercontent.com/rl-tools/conta-index/main/version/1/meta.json`

A cached blob never touches the network. The blob cache stores verified, uncompressed bytes so consumers can read, seek, or memory-map them directly. Gzip is a download representation and is not retained as a second cached copy.

On a blob-cache miss the client consults its cached index. A lookup matches an entry by `sha1` or `sha256`; for example, one blob might have these locations:

```json
"shards": [
  {"id": "primary", "gzip": true},
  {"id": "mirror", "gzip": false}
]
```

For each location in order, look up its `id` in the top-level shard table and substitute the entry's digest into that shard's `url`. If `gzip` is true and the client supports gzip, first try the URL with `.gz` appended to its path, preserving any query string or fragment: `https://host/data/<sha1>?download=1` becomes `https://host/data/<sha1>.gz?download=1`. No separate gzip URL template or preliminary `HEAD` request is needed. Missing or false `gzip`, or a client without gzip support, means try raw directly.

Stream gzip decompression (or the raw response) into a temporary file while hashing the original bytes. Verify the requested digest and the entry's SHA-1 and SHA-256 before atomically placing the plain file in the cache. A failed download, decompression, or hash check discards the temporary file. Try raw at the same shard after a gzip failure, then proceed to the next shard after a raw failure. The example above tries `primary/<sha1>.gz`, `primary/<sha1>`, then `mirror/<sha1>`.

If the hash is absent or every location fails, refresh once: fetch `meta.json`, and only when `index.sha256` differs from the cached index fetch `index.location` (relative to `meta.json`), verify its SHA-256 (a CDN can briefly serve `meta.json` and the index from different commits; on mismatch retry or try the next entry point), replace the index cache and retry the lookup. Report failure if that retry also fails. These download and cache rules are the contract for external clients; the website exposes direct raw and gzip download links.


