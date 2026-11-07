#!/usr/bin/env python3
"""Unit tests for clinic PMTiles rewrite / verify in sync-web-bundle.py."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock


def _load_sync() -> ModuleType:
    path = Path(__file__).resolve().parent / "sync-web-bundle.py"
    spec = importlib.util.spec_from_file_location("sync_web_bundle", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sync = _load_sync()

CLINIC = sync.DEFAULT_CLINIC_PMTILES_UPSTREAM
OTHER = "https://pub-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.r2.dev/other_layer.pmtiles"
WEB = "https://opencrinic-ai.vercel.app"
PROXY = f"{WEB}/api/clinic-pmtiles"


class ClinicPmtilesRewriteTests(unittest.TestCase):
    def _write(self, root: Path, rel: str, text: str) -> Path:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_rewrites_known_clinic_url_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(
                root,
                "chunk.js",
                f'const clinic="{CLINIC}"; const other="{OTHER}";',
            )
            n = sync.rewrite_clinic_pmtiles_urls(root, WEB)
            text = (root / "chunk.js").read_text(encoding="utf-8")
            self.assertEqual(n, 1)
            self.assertIn(PROXY, text)
            self.assertNotIn(CLINIC, text)
            self.assertIn(OTHER, text)

    def test_does_not_rewrite_unrelated_pmtiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "chunk.js", f'url="{OTHER}"')
            n = sync.rewrite_clinic_pmtiles_urls(root, WEB)
            self.assertEqual(n, 0)
            self.assertEqual(
                (root / "chunk.js").read_text(encoding="utf-8"), f'url="{OTHER}"'
            )
            self.assertEqual(sync.verify_clinic_pmtiles_proxy(root, WEB), [])

    def test_already_proxied_is_ok_with_zero_rewrites(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "chunk.js", f'url="{PROXY}"')
            n = sync.rewrite_clinic_pmtiles_urls(root, WEB)
            self.assertEqual(n, 0)
            self.assertEqual(sync.verify_clinic_pmtiles_proxy(root, WEB), [])

    def test_layer_disabled_allows_zero_rewrites(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "chunk.js", 'url="/maplibre/maplibre-gl-worker.mjs"')
            n = sync.rewrite_clinic_pmtiles_urls(root, WEB)
            self.assertEqual(n, 0)
            self.assertEqual(sync.verify_clinic_pmtiles_proxy(root, WEB), [])

    def test_verify_fails_when_upstream_url_format_drifts(self) -> None:
        drifted = (
            "https://pub-d14db819fe324e45b72d41d7c7cf70c6.r2.dev/"
            "clinic_map_20990101.pmtiles"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "chunk.js", f'url="{drifted}"')
            n = sync.rewrite_clinic_pmtiles_urls(root, WEB)
            self.assertEqual(n, 0)
            errors = sync.verify_clinic_pmtiles_proxy(root, WEB)
            self.assertTrue(errors)
            joined = "\n".join(errors)
            self.assertIn("unsupported clinic PMTiles", joined)
            self.assertIn(drifted, joined)

    def test_env_overrides_known_upstream(self) -> None:
        custom = (
            "https://pub-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.r2.dev/clinic_custom.pmtiles"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "chunk.js", f'a="{custom}"; b="{CLINIC}"; c="{OTHER}"')
            with mock.patch.dict(os.environ, {"CLINIC_TILES_UPSTREAM_URL": custom}):
                n = sync.rewrite_clinic_pmtiles_urls(root, WEB)
                errors = sync.verify_clinic_pmtiles_proxy(root, WEB)
            text = (root / "chunk.js").read_text(encoding="utf-8")
            self.assertEqual(n, 1)
            self.assertIn(PROXY, text)
            self.assertNotIn(custom, text)
            # Default clinic URL is not in the override list; treat as unsupported.
            self.assertTrue(any("unsupported" in e for e in errors))
            self.assertIn(CLINIC, text)
            self.assertIn(OTHER, text)


if __name__ == "__main__":
    unittest.main()
