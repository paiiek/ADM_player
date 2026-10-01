"""P-175 — the recorder shows the engine catalog's operator title for a
``/sys/warning`` code, not the raw code. Headless (no PySide6 / PortAudio), like
P-171's parser test; the GUI wiring is pinned by reading ``gui_app.py`` as source.
"""
from __future__ import annotations

import ast
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from adm_recorder import warning_catalog as wc

_ROOT = Path(__file__).resolve().parents[1]
_GUI_APP = _ROOT / "adm_recorder" / "gui_app.py"


def _load_sync_script():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "sync_warning_titles", _ROOT / "scripts" / "sync_warning_titles.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SnapshotTest(unittest.TestCase):
    def test_snapshot_is_the_engine_catalog_shape(self) -> None:
        data = json.loads(wc.SNAPSHOT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(data["schema"], wc.SNAPSHOT_SCHEMA)
        codes = data["codes"]
        # The engine catalog had 156 codes at e96185c7; a near-empty snapshot
        # would make every lookup fall back silently.
        self.assertGreaterEqual(len(codes), 150)
        for code, e in codes.items():
            self.assertTrue(e.get("title"), code)
            self.assertNotEqual(e["title"], code, f"{code}: title is the raw code")

    def test_codes_the_engine_emits_since_p172_are_present(self) -> None:
        for code in ("echo_rate_limited", "meta_sidecar_unbound",
                     "object_input_unfed", "wfs_delay_envelope_clamped"):
            self.assertIsNotNone(wc.warning_title(code), code)


class FormatTest(unittest.TestCase):
    def test_known_code_shows_title_and_keeps_code_and_detail(self) -> None:
        title = wc.warning_title("echo_rate_limited")
        line = wc.format_engine_warning("echo_rate_limited", "dropped=3")
        self.assertEqual(line, f"{title} (echo_rate_limited dropped=3)")
        self.assertFalse(line.startswith("echo_rate_limited"))

    def test_known_code_empty_detail(self) -> None:
        title = wc.warning_title("echo_rate_limited")
        self.assertEqual(wc.format_engine_warning("echo_rate_limited", ""),
                         f"{title} (echo_rate_limited)")

    def test_default_locale_is_korean(self) -> None:
        e = wc.load_titles()["echo_rate_limited"]
        self.assertEqual(wc.warning_title("echo_rate_limited"), e["title"])

    def test_en_locale_and_its_fallback_to_korean(self) -> None:
        titles = wc.load_titles()
        with_en = next(c for c, e in titles.items() if e.get("title_en"))
        without_en = next(c for c, e in titles.items() if not e.get("title_en"))
        self.assertEqual(wc.warning_title(with_en, "en"), titles[with_en]["title_en"])
        self.assertEqual(wc.warning_title(without_en, "en"), titles[without_en]["title"])

    def test_unknown_code_falls_back_to_the_raw_pre_p175_line(self) -> None:
        self.assertIsNone(wc.warning_title("no_such_code_p175"))
        self.assertEqual(wc.format_engine_warning("no_such_code_p175", "x=1"),
                         "no_such_code_p175 (x=1)")
        self.assertEqual(wc.format_engine_warning("no_such_code_p175", ""),
                         "no_such_code_p175")

    def test_missing_snapshot_falls_back_to_raw_never_raises(self) -> None:
        wc.load_titles.cache_clear()
        try:
            with mock.patch.object(wc, "SNAPSHOT_PATH", Path("/nonexistent/p175.json")):
                self.assertEqual(wc.load_titles(), {})
                self.assertEqual(wc.format_engine_warning("echo_rate_limited", "dropped=3"),
                                 "echo_rate_limited (dropped=3)")
        finally:
            wc.load_titles.cache_clear()


class SyncCheckTest(unittest.TestCase):
    """The snapshot is only as good as its sync check, so the check is tested
    both ways against a fake engine root built from the real snapshot."""

    def _engine_root(self, codes: dict) -> Path:
        d = Path(tempfile.mkdtemp(prefix="p175-engine-"))
        (d / "ui").mkdir()
        (d / "ui" / "warning_catalog.json").write_text(
            json.dumps({"schema": 1, "codes": codes}, ensure_ascii=False), encoding="utf-8")
        return d

    def _run(self, root: Path) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = _load_sync_script().main(["--engine-root", str(root)])
        return rc, buf.getvalue()

    def test_in_sync_engine_passes(self) -> None:
        codes = {c: dict(e, severity="warn") for c, e in wc.load_titles().items()}
        rc, out = self._run(self._engine_root(codes))
        self.assertEqual(rc, 0, out)

    def test_drift_is_red_and_named(self) -> None:  # negative control
        codes = {c: dict(e) for c, e in wc.load_titles().items()}
        del codes["echo_rate_limited"]
        codes["p175_new_code"] = {"title": "새 경고"}
        codes["object_input_unfed"]["title"] = "changed"
        rc, out = self._run(self._engine_root(codes))
        self.assertEqual(rc, 1, out)
        self.assertIn("missing from snapshot: p175_new_code", out)
        self.assertIn("no longer in the engine catalog: echo_rate_limited", out)
        self.assertIn("title changed: object_input_unfed", out)

    def test_unreadable_engine_catalog_is_exit_2(self) -> None:
        rc, _ = self._run(Path(tempfile.mkdtemp(prefix="p175-empty-")))
        self.assertEqual(rc, 2)

    def test_titles_from_engine_catalog_rejects_other_schemas(self) -> None:
        with self.assertRaises(ValueError):
            wc.titles_from_engine_catalog({"schema": 2, "codes": {}})


class GuiAppUsesTheCatalogTest(unittest.TestCase):
    def test_on_engine_warning_formats_through_the_catalog(self) -> None:
        tree = ast.parse(_GUI_APP.read_text(encoding="utf-8"))
        imported = [
            n for n in tree.body
            if isinstance(n, ast.ImportFrom) and n.module == "warning_catalog" and n.level == 1
            and any(a.name == "format_engine_warning" and a.asname == "_format_engine_warning"
                    for a in n.names)
        ]
        self.assertEqual(len(imported), 1, "gui_app must import format_engine_warning")
        handler = next(n for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef) and n.name == "_on_engine_warning")
        calls = [n for n in ast.walk(handler)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "_format_engine_warning"]
        self.assertEqual(len(calls), 1, "_on_engine_warning must format via the catalog")


if __name__ == "__main__":
    unittest.main()
