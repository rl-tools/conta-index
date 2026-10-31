# conta-index

Which shard holds each conta blob: the data served at the conta index entry points. `current.json`, `shards/` and `version/` are maintained with `conta index`, run from the repository root with the conta release pinned in `requirements.txt`, which the check workflow uses to verify the committed generated files; their formats, the ingestion procedure and the client contract are documented in the [conta README](https://pypi.org/project/conta/). Generator output changes therefore land as a conta release plus a pin bump in the same commit as the regenerated files.

`tools/build_web.py` publishes these files with the `web/` explorer to GitHub Pages (`https://conta.rl.tools`); the explorer shows the uncompressed size in a sortable “File size” column and each raw or gzip download's size beside its link, with exact byte counts in tooltips.

Entries that predate `first_indexed_at` were backfilled from Git history using the committer timestamp of the first commit introducing each shard entry; these dates do not identify the originating RLtools commit.
