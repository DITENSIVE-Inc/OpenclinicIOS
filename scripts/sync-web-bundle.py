#!/usr/bin/env python3
"""Refresh App/public from the Openclinic web deploy + web repo static files.

Web has no `output: 'export'`, so the compiled UI is taken from the production
(or preview) URL. Large `/data` and `/maplibre` assets are copied from a
checkout of the web repository when available.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

ASSET_RE = re.compile(
    r"""(?P<url>/(?:_next|data|maplibre)/[A-Za-z0-9_./%~+-]+|/(?:favicon\.ico))"""
)
CSS_URL_RE = re.compile(r"""url\((['"]?)(?P<url>[^'")]+)\1\)""")
DPL_RE = re.compile(r'data-dpl-id="(?P<id>dpl_[A-Za-z0-9]+)"')

SKIP_SUFFIXES = (".map",)
TEXT_SUFFIXES = (".html", ".js", ".mjs", ".css", ".json", ".txt", ".svg")


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def http_get(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "OpenclinicIOS-web-sync/1.0",
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def is_same_origin(base: str, candidate: str) -> bool:
    b = urlparse(base)
    c = urlparse(candidate)
    if not c.scheme:
        return True
    return (c.scheme, c.netloc) == (b.scheme, b.netloc)


def to_site_path(origin: str, raw: str, resolve_against: str | None = None) -> str | None:
    if raw.startswith("data:") or raw.startswith("blob:") or raw.startswith("#"):
        return None
    # CSS/JS often use relative urls (e.g. ../media/foo.woff2). Resolve those
    # against the asset URL, not the site root — otherwise fonts become /media/...
    # and are dropped by the /_next/ filter.
    base = resolve_against or origin
    if not base.endswith("/") and not re.search(r"\.[A-Za-z0-9]+$", urlparse(base).path):
        base = base + "/"
    absolute = urljoin(base, raw)
    if not is_same_origin(origin, absolute):
        return None
    path = urlparse(absolute).path
    if not path.startswith("/"):
        return None
    if path.endswith(SKIP_SUFFIXES):
        return None
    return path


def extract_paths(
    base: str,
    body: str,
    from_css: bool = False,
    source_url: str | None = None,
) -> set[str]:
    found: set[str] = set()
    for match in ASSET_RE.finditer(body):
        path = to_site_path(base, match.group("url"), resolve_against=source_url)
        if path:
            found.add(path)
    if from_css:
        for match in CSS_URL_RE.finditer(body):
            path = to_site_path(base, match.group("url"), resolve_against=source_url)
            if path and path.startswith("/_next/"):
                found.add(path)
    # Chunk paths without a leading slash sometimes appear in Turbopack payloads.
    for match in re.finditer(r"""(?P<p>static/immutable/[A-Za-z0-9_./+-]+\.(?:js|mjs|css|woff2))""", body):
        path = to_site_path(base, "/_next/" + match.group("p"), resolve_against=source_url)
        if path:
            found.add(path)
    return found


def copy_tree(src: Path, dest: Path) -> int:
    if not src.exists():
        return 0
    count = 0
    for path in src.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(src)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        count += 1
    return count


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def download_graph(
    base_url: str,
    seed_paths: Iterable[str],
    out_dir: Path,
    skip_prefixes: Iterable[str] = (),
) -> dict[str, str]:
    """Crawl deploy assets into out_dir.

    skip_prefixes: path prefixes already populated from the web checkout
    (typically /data/ and /maplibre/). Still allow explicit seed hits that
    already exist on disk to be skipped without re-download.
    """
    pending = set(seed_paths)
    seen: set[str] = set()
    downloaded: dict[str, str] = {}
    skips = tuple(skip_prefixes)

    def ignored(path: str) -> bool:
        return any(path == p.rstrip("/") or path.startswith(p) for p in skips)

    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        if ignored(path):
            continue
        url = urljoin(base_url, path.lstrip("/"))
        try:
            data = http_get(url)
        except urllib.error.HTTPError as err:
            eprint(f"skip {path}: HTTP {err.code}")
            continue
        except Exception as err:  # noqa: BLE001 - surface and continue crawl
            eprint(f"skip {path}: {err}")
            continue

        dest = out_dir / path.lstrip("/")
        write_bytes(dest, data)
        downloaded[path] = url
        eprint(f"got {path} ({len(data)} bytes)")

        suffix = Path(path).suffix.lower()
        if suffix in TEXT_SUFFIXES:
            try:
                text = data.decode("utf-8", errors="ignore")
            except Exception:  # noqa: BLE001
                continue
            more = extract_paths(
                base_url,
                text,
                from_css=(suffix == ".css"),
                source_url=url,
            )
            pending.update(p for p in more if p not in seen and not ignored(p))

    return downloaded


def preserve_cordova(out_dir: Path) -> None:
    for name in ("cordova.js", "cordova_plugins.js"):
        path = out_dir / name
        if not path.exists():
            path.write_text("")


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--web-app-url",
        default=os.environ.get("WEB_APP_URL", "https://opencrinic-ai.vercel.app"),
        help="Deployed web origin used for index.html and /_next assets",
    )
    parser.add_argument(
        "--web-checkout",
        default=os.environ.get("WEB_CHECKOUT", ""),
        help="Path to a checkout of OpencrinicAI (for public/data and public/maplibre)",
    )
    parser.add_argument(
        "--web-ref",
        default=os.environ.get("WEB_REF", "main"),
        help="Label recorded in sync metadata (branch/tag/sha)",
    )
    parser.add_argument(
        "--web-sha",
        default=os.environ.get("WEB_SHA", ""),
        help="Exact git sha recorded in sync metadata when known",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=root / "App" / "public",
        help="Destination directory (default: App/public)",
    )
    parser.add_argument(
        "--keep-existing-data",
        action="store_true",
        help="Do not delete App/public/data before copying (debug only)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base_url = args.web_app_url.rstrip("/") + "/"
    out_dir: Path = args.out_dir

    eprint(f"sync into {out_dir}")
    eprint(f"web app url: {base_url}")

    # Start from a clean shell so removed upstream files disappear.
    if out_dir.exists():
        for child in out_dir.iterdir():
            if args.keep_existing_data and child.name == "data":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    else:
        out_dir.mkdir(parents=True, exist_ok=True)

    copied_from_checkout = 0
    skip_prefixes: list[str] = []
    web_checkout = Path(args.web_checkout) if args.web_checkout else None
    if web_checkout and web_checkout.exists():
        public_dir = web_checkout / "public"
        eprint(f"copy static files from {public_dir}")
        for name in ("data", "maplibre"):
            copied = copy_tree(public_dir / name, out_dir / name)
            copied_from_checkout += copied
            if copied:
                skip_prefixes.append(f"/{name}/")
        favicon = public_dir / "favicon.ico"
        if not favicon.exists():
            favicon = web_checkout / "src" / "app" / "favicon.ico"
        if favicon.exists():
            shutil.copy2(favicon, out_dir / "favicon.ico")
            copied_from_checkout += 1
            skip_prefixes.append("/favicon.ico")
        eprint(f"copied {copied_from_checkout} files from web checkout")
        if skip_prefixes:
            eprint(f"skip re-crawl of: {', '.join(skip_prefixes)}")
    else:
        eprint("no web checkout; /data and /maplibre will be crawled from the deploy URL")

    eprint("fetch index.html")
    index_bytes = http_get(urljoin(base_url, "/"))
    index_text = index_bytes.decode("utf-8", errors="ignore")
    write_bytes(out_dir / "index.html", index_bytes)

    dpl_match = DPL_RE.search(index_text)
    deployment_id = dpl_match.group("id") if dpl_match else ""

    seed = extract_paths(base_url, index_text, source_url=urljoin(base_url, "/"))
    seed.add("/favicon.ico")
    if not (out_dir / "data").exists():
        # Minimal seeds; JS references will pull the rest when possible.
        seed.update(
            {
                "/data/prefecture-indicators.json",
                "/data/municipality-profitability.json",
                "/data/clinic-profitability-mcdb.json",
                "/data/clinic-filings.json",
                "/maplibre/maplibre-gl-worker.mjs",
            }
        )

    downloaded = download_graph(base_url, seed, out_dir, skip_prefixes=skip_prefixes)
    preserve_cordova(out_dir)

    meta = {
        "synced_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "web_app_url": base_url.rstrip("/"),
        "web_ref": args.web_ref,
        "web_sha": args.web_sha,
        "deployment_id": deployment_id,
        "copied_from_checkout": copied_from_checkout,
        "downloaded_paths": len(downloaded),
        "note": (
            "UI shell (/_next, index.html) comes from the deployed web app. "
            "Static /data and /maplibre prefer the web git checkout when provided."
        ),
    }
    write_bytes(out_dir / ".web-sync-meta.json", (json.dumps(meta, indent=2) + "\n").encode())
    eprint(json.dumps(meta, indent=2))

    if not (out_dir / "index.html").exists():
        eprint("error: index.html missing after sync")
        return 1
    if not (out_dir / "_next").exists():
        eprint("error: _next assets missing after sync")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
