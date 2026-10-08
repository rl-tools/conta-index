# conta-index

Which shard holds each conta blob. Consumers never read this repository directly; the client hardcodes the entry points below and reads `meta.json` of the version it implements. Blobs are immutable and verified by hash, so the index is trusted for availability only.

- `version/<n>/` — one directory per contract version. Every version stays published and keeps being generated from the shared shard files for as long as the store exists, so a client hardcodes its version; extensions go into a new version. `tools/conta_index.py` has one generator per version and refuses a version directory it has no generator for.
- `version/<n>/meta.json` — hand-written except `index.sha256`: `format` (equal to `<n>`), `index` (`location`, and `sha256` of the generated index written by `build`).
- `version/<n>/shards.json` — hand-written shard table, id → `{type, url, repo}` in order of preference. `url` is the http transport, a template with `{sha1}` or `{sha256}`, so a changed download location is an edit here and no client update. `type` pre-empts transports where inserting a hash into a url is not enough; a new type needs a client that knows it. `repo` is informational.
- `shards/<id>.json` — source of truth per shard, shared by all versions: entries `{sha1, sha256, description}` sorted by `sha1`, one per line. Edit by hand or run `tools/conta_index.py scan <id> <checkout>`, which adds every `data/<sha1>` of a shard checkout, verifies it hashes to its name, takes descriptions from the checkout's optional `metadata.json`, and rebuilds.
- `version/<n>/generated/index.json` — written by `tools/conta_index.py build`, never edited: the shard table plus `entries` `{sha1, sha256, shards, description}` merged across shards. Committed so the raw GitHub URL serves it; `tools/conta_index.py check` (also the CI workflow) fails when it or `index.sha256` does not match the sources.

A shard is a directory of blobs named by their sha1 and nothing else is required of it. Never delete or rewrite a blob in a shard; the index is append-only, so a newer index never has fewer entries.

## Client contract

Entry points, tried in order:

1. `https://conta.rl.tools/version/1/meta.json`
2. `https://raw.githubusercontent.com/rl-tools/conta-index/main/version/1/meta.json`

A cached blob never touches the network. On a blob-cache miss the client consults its cached index; if the hash is absent or every location fails, it refreshes once: fetch `meta.json`, and only when `index.sha256` differs from the cached index fetch `index.location` (relative to `meta.json`), verify its sha256 (a CDN can briefly serve `meta.json` and the index from different commits; on mismatch retry or try the next entry point), replace the cache and retry the lookup. A lookup matches an entry by `sha1` or `sha256`; the locations are the entry's `shards` in order, each shard's `url` with the digest substituted.

GitHub Pages serves the `main` branch root. A mirror on Hugging Face is `git push` of the same repository to a dataset.
