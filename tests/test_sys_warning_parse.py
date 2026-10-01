"""P-171 — the recorder's ``/sys/warning`` payload parser, tested headless.

``gui_app`` imports PySide6 and sounddevice (PortAudio) at module load, so the
parser lives in ``adm_recorder.sys_warning`` and is imported from there. The
last test pins that wiring by reading ``gui_app.py`` as source (no import), so
the GUI cannot drift back to a private, untested copy.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

from adm_recorder.sys_warning import parse_sys_warning_args

_GUI_APP = Path(__file__).resolve().parents[1] / "adm_recorder" / "gui_app.py"


class ParseSysWarningArgsTest(unittest.TestCase):
    # ── the two wire shapes ────────────────────────────────────────────────
    def test_iiss_shape(self) -> None:
        # ",iiss <int> <int> category detail" — e.g. EchoSubscriber's rate limit.
        self.assertEqual(
            parse_sys_warning_args([3, 0, "echo_rate_limited", "dropped=3"]),
            ("echo_rate_limited", "dropped=3"),
        )

    def test_iiss_shape_empty_detail(self) -> None:
        # The engine sends "" as the detail when it has none (binaural_sofa_empty_path).
        self.assertEqual(
            parse_sys_warning_args([0, 0, "binaural_sofa_empty_path", ""]),
            ("binaural_sofa_empty_path", ""),
        )

    def test_s_shape(self) -> None:
        # ",s <message>" — no ints, no detail (spatial_engine_core.cpp's
        # control-loop emitters send one free-text string).
        self.assertEqual(
            parse_sys_warning_args(["some message"]),
            ("some message", ""),
        )

    # ── malformed / unexpected payloads degrade, never raise ───────────────
    def test_empty_args(self) -> None:
        self.assertEqual(parse_sys_warning_args([]), ("unknown", ""))

    def test_ints_only(self) -> None:
        # A ",ii" relapse (strings dropped) — no category to show.
        self.assertEqual(parse_sys_warning_args([1, 2]), ("unknown", ""))

    def test_iis_shape_missing_detail(self) -> None:
        # A ",iis" relapse drops the detail; the category must still come through.
        self.assertEqual(
            parse_sys_warning_args([0, 0, "binaural_sofa_file_missing"]),
            ("binaural_sofa_file_missing", ""),
        )

    def test_non_string_values_are_skipped_in_order(self) -> None:
        # Floats, None, bytes and bools are not strings; the first two strings win.
        self.assertEqual(
            parse_sys_warning_args([1.5, None, b"raw", True, "cat", 7, "det"]),
            ("cat", "det"),
        )

    def test_extra_strings_are_ignored(self) -> None:
        self.assertEqual(parse_sys_warning_args(["a", "b", "c"]), ("a", "b"))

    def test_tuple_payload(self) -> None:
        # python-osc hands handlers a tuple via *args in some call paths.
        self.assertEqual(parse_sys_warning_args((0, 0, "c", "d")), ("c", "d"))

    def test_returns_str_pair(self) -> None:
        cat, det = parse_sys_warning_args([0, 0, "c", "d"])
        self.assertIsInstance(cat, str)
        self.assertIsInstance(det, str)


class GuiAppUsesTheTestedParserTest(unittest.TestCase):
    def test_gui_app_imports_and_does_not_redefine(self) -> None:
        tree = ast.parse(_GUI_APP.read_text(encoding="utf-8"))
        imported = [
            node
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
            and node.module == "sys_warning"
            and node.level == 1
            and any(
                a.name == "parse_sys_warning_args" and a.asname == "_parse_sys_warning_args"
                for a in node.names
            )
        ]
        self.assertEqual(len(imported), 1, "gui_app must import the parser from .sys_warning")
        local_defs = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in ("_parse_sys_warning_args", "parse_sys_warning_args")
        ]
        self.assertEqual(local_defs, [], "gui_app must not carry its own copy of the parser")
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_parse_sys_warning_args"
        ]
        self.assertGreaterEqual(len(calls), 1, "the /sys/warning handler must call the parser")


if __name__ == "__main__":
    unittest.main()
