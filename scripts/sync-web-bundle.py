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
# Turbopack registers MapLibre dev builds as modules, but the app sets
# WORKER_URL to /maplibre/maplibre-gl-worker.mjs and never loads these.
MAPLIBRE_DEV_NAME_RE = re.compile(
    r"^maplibre-gl(?:-worker|-shared)?-dev\."
)
# National clinic PMTiles are hosted on R2. Capacitor's origin
# (capacitor://localhost) is not in that bucket's CORS allowlist, and PMTiles
# requires HTTP Range — so the iOS shell must load them through the Web
# deploy's byte-serving proxy instead of R2 directly.
#
# Only rewrite the known clinic upstream URL(s). A broad pub-*.r2.dev/*.pmtiles
# match would incorrectly point future non-clinic archives at /api/clinic-pmtiles.
# Keep DEFAULT in sync with OpencrinicAI's clinic-pmtiles route DEFAULT_UPSTREAM.
DEFAULT_CLINIC_PMTILES_UPSTREAM = (
    "https://pub-d14db819fe324e45b72d41d7c7cf70c6.r2.dev/clinic_map_20261017.pmtiles"
)
CLINIC_PMTILES_PROXY_PATH = "/api/clinic-pmtiles"
# Catch clinic archives whose URL drifted from the known list (fail sync loudly).
UNSUPPORTED_CLINIC_R2_PMTILES_RE = re.compile(
    r"https://pub-[a-z0-9]+\.r2\.dev/[A-Za-z0-9_./%~+-]*clinic[A-Za-z0-9_./%~+-]*\.pmtiles",
    re.IGNORECASE,
)


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def is_skipped_asset(path: str) -> bool:
    """Return True for known-unused deploy assets we should not ship."""
    return bool(MAPLIBRE_DEV_NAME_RE.match(Path(path).name))


def clinic_pmtiles_upstream_urls() -> tuple[str, ...]:
    """Upstream R2 URL(s) eligible for proxy rewrite.

    Override with CLINIC_TILES_UPSTREAM_URL (same name as the Web proxy env).
    """
    from_env = (os.environ.get("CLINIC_TILES_UPSTREAM_URL") or "").strip()
    if from_env:
        return (from_env,)
    return (DEFAULT_CLINIC_PMTILES_UPSTREAM,)


def clinic_pmtiles_proxy_url(web_app_url: str) -> str:
    return urljoin(web_app_url.rstrip("/") + "/", CLINIC_PMTILES_PROXY_PATH.lstrip("/"))


def iter_text_bundle_files(out_dir: Path) -> Iterable[Path]:
    for path in out_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
            yield path


def rewrite_clinic_pmtiles_urls(out_dir: Path, web_app_url: str) -> int:
    """Point bundled JS at the Vercel Range/CORS proxy for Capacitor."""
    proxy = clinic_pmtiles_proxy_url(web_app_url)
    upstreams = clinic_pmtiles_upstream_urls()
    rewritten = 0
    for path in iter_text_bundle_files(out_dir):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        new_text = text
        n = 0
        for upstream in upstreams:
            count = new_text.count(upstream)
            if not count:
                continue
            new_text = new_text.replace(upstream, proxy)
            n += count
        if n:
            path.write_text(new_text, encoding="utf-8")
            rewritten += n
            eprint(f"rewrite clinic pmtiles → proxy in {path.relative_to(out_dir)} ({n})")
    return rewritten


def verify_clinic_pmtiles_proxy(out_dir: Path, web_app_url: str) -> list[str]:
    """Fail sync when clinic tiles still hit R2, or the proxy URL is missing.

    Already-proxied bundles and builds without a clinic layer are OK (0 rewrites).
    """
    proxy = clinic_pmtiles_proxy_url(web_app_url)
    upstreams = set(clinic_pmtiles_upstream_urls())
    leftover_direct: list[str] = []
    unsupported: list[str] = []
    saw_proxy = False
    saw_clinic_layer = False

    for path in iter_text_bundle_files(out_dir):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = str(path.relative_to(out_dir))
        if proxy in text:
            saw_proxy = True
            saw_clinic_layer = True
        for upstream in upstreams:
            if upstream in text:
                saw_clinic_layer = True
                leftover_direct.append(f"{rel}: {upstream}")
        for match in UNSUPPORTED_CLINIC_R2_PMTILES_RE.finditer(text):
            url = match.group(0)
            if url in upstreams:
                continue
            saw_clinic_layer = True
            unsupported.append(f"{rel}: {url}")

    errors: list[str] = []
    if leftover_direct:
        errors.append("clinic PMTiles still reference R2 directly after rewrite:")
        errors.extend(f"  {item}" for item in leftover_direct[:20])
    if unsupported:
        errors.append(
            "unsupported clinic PMTiles R2 URLs "
            "(add to CLINIC_TILES_UPSTREAM_URL / DEFAULT_CLINIC_PMTILES_UPSTREAM):"
        )
        errors.extend(f"  {item}" for item in unsupported[:20])
    if saw_clinic_layer and not saw_proxy:
        errors.append(
            "clinic layer appears enabled but expected absolute proxy URL is missing: "
            f"{proxy}"
        )
    return errors


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
        if is_skipped_asset(path):
            return True
        return any(path == p.rstrip("/") or path.startswith(p) for p in skips)

    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        if ignored(path):
            if is_skipped_asset(path):
                eprint(f"omit unused asset {path}")
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


def prune_skipped_assets(out_dir: Path) -> list[str]:
    """Remove any skipped assets that still landed on disk."""
    removed: list[str] = []
    media = out_dir / "_next" / "static" / "immutable" / "media"
    if not media.exists():
        return removed
    for path in media.iterdir():
        if path.is_file() and is_skipped_asset(path.name):
            path.unlink()
            removed.append("/" + str(path.relative_to(out_dir)).replace("\\", "/"))
            eprint(f"pruned {removed[-1]}")
    return removed


def collect_required_refs(out_dir: Path, origin: str) -> set[str]:
    """Local paths that must exist for the shell/CSS to load correctly.

    Intentionally checks index.html links and CSS url(...) only. JS chunk
    string tables mention unused Turbopack modules (e.g. maplibre-*-dev) that
    are never fetched at runtime.
    """
    required: set[str] = {"/index.html", "/favicon.ico"}
    index = out_dir / "index.html"
    if index.exists():
        text = index.read_text(errors="ignore")
        required.update(
            extract_paths(origin, text, source_url=urljoin(origin, "/"))
        )

    for css in (out_dir / "_next").rglob("*.css") if (out_dir / "_next").exists() else []:
        rel = "/" + str(css.relative_to(out_dir)).replace("\\", "/")
        required.add(rel)
        source_url = urljoin(origin, rel.lstrip("/"))
        required.update(
            extract_paths(
                origin,
                css.read_text(errors="ignore"),
                from_css=True,
                source_url=source_url,
            )
        )
    return {p for p in required if not is_skipped_asset(p)}


def verify_required_refs(out_dir: Path, origin: str) -> list[str]:
    missing: list[str] = []
    for path in sorted(collect_required_refs(out_dir, origin)):
        if path.startswith("/data/") or path.startswith("/maplibre/"):
            # Large static trees are validated lightly: only require the tree
            # root to exist when the path was discovered.
            root_name = path.strip("/").split("/", 1)[0]
            if not (out_dir / root_name).exists():
                missing.append(path)
            continue
        local = out_dir / path.lstrip("/")
        if not local.is_file():
            missing.append(path)
    return missing


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
    pruned = prune_skipped_assets(out_dir)

    missing = verify_required_refs(out_dir, base_url)
    if missing:
        eprint("error: required local refs missing after sync:")
        for path in missing[:50]:
            eprint(f"  {path}")
        if len(missing) > 50:
            eprint(f"  ... and {len(missing) - 50} more")
        return 1

    clinic_pmtiles_rewrites = rewrite_clinic_pmtiles_urls(out_dir, base_url)
    clinic_pmtiles_errors = verify_clinic_pmtiles_proxy(out_dir, base_url)
    if clinic_pmtiles_errors:
        eprint("error: clinic PMTiles proxy verification failed:")
        for line in clinic_pmtiles_errors:
            eprint(f"  {line}")
        return 1

    meta = {
        "synced_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "web_app_url": base_url.rstrip("/"),
        "web_ref": args.web_ref,
        "web_sha": args.web_sha,
        "deployment_id": deployment_id,
        "copied_from_checkout": copied_from_checkout,
        "downloaded_paths": len(downloaded),
        "pruned_unused_assets": pruned,
        "clinic_pmtiles_proxy_rewrites": clinic_pmtiles_rewrites,
        "clinic_pmtiles_proxy_url": clinic_pmtiles_proxy_url(base_url),
        "clinic_pmtiles_upstream_urls": list(clinic_pmtiles_upstream_urls()),
        "verified_required_refs": True,
        "verified_clinic_pmtiles_proxy": True,
        "note": (
            "UI shell (/_next, index.html) comes from the deployed web app. "
            "Static /data and /maplibre prefer the web git checkout when provided. "
            "MapLibre *-dev media modules are omitted; CSS/index refs are verified. "
            "Known R2 clinic PMTiles URLs are rewritten to /api/clinic-pmtiles on the "
            "web deploy so Capacitor can use Range requests with CORS."
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
