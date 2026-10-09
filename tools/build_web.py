#!/usr/bin/env python3
"""Build the static website and publishable JSON files into _site/."""

from html import escape
from pathlib import Path
import shutil
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
OUTPUT = ROOT / "_site"
SITEMAP = "<!-- sitemap -->"


def build():
    template = (WEB / "index.html").read_text(encoding="utf-8")
    if template.count(SITEMAP) != 1:
        raise SystemExit(f"web/index.html must contain exactly one {SITEMAP} placeholder")

    paths = sorted(path.relative_to(ROOT) for path in [
        ROOT / "current.json",
        *(ROOT / "version").rglob("*.json"),
        *(ROOT / "shards").glob("*.json"),
    ])
    links = "\n    ".join(
        f'<li><a href="{quote(path.as_posix())}">{escape(path.as_posix())}</a></li>'
        for path in paths
    )

    # Rebuild from scratch so removed assets cannot linger in the deployment.
    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    shutil.copytree(WEB, OUTPUT)
    for path in paths:
        destination = OUTPUT / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / path, destination)
    (OUTPUT / "index.html").write_text(template.replace(SITEMAP, links), encoding="utf-8")
    print(f"Built {OUTPUT} with {len(paths)} JSON files")


if __name__ == "__main__":
    build()
