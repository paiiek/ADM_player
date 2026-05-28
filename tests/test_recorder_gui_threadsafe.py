"""The capture-error bridge MUST marshal cross-thread emits to the GUI thread.

Audio callbacks run on PortAudio's thread; calling Qt widget methods directly
from there would race. The bridge is a tiny QObject living on the GUI thread,
so ``bridge.error.emit(...)`` from any thread gets delivered via
Qt.AutoConnection → QueuedConnection.

If this test fails, somebody removed the QObject parentage or moved the bridge
off the main thread — direct Qt access from the audio thread is back.
"""
from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication  # noqa: E402

from adm_recorder.gui_app import _CaptureErrorBridge  # noqa: E402


def _ensure_app() -> QCoreApplication:
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


def test_capture_error_signal_marshals_to_gui_thread() -> None:
    app = _ensure_app()
    bridge = _CaptureErrorBridge()
    received: list[tuple[str, int]] = []
    main_tid = threading.get_ident()

    def slot(msg: str) -> None:
        received.append((msg, threading.get_ident()))

    bridge.error.connect(slot)

    emitter_tid: list[int] = []

    def emit_from_worker() -> None:
        emitter_tid.append(threading.get_ident())
        bridge.error.emit("disk full")

    t = threading.Thread(target=emit_from_worker)
    t.start()
    t.join()

    # The slot is queued; pump the event loop until it lands (or time out).
    deadline = time.monotonic() + 1.0
    while not received and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)

    assert len(received) == 1, "queued signal never reached the GUI thread"
    msg, slot_tid = received[0]
    assert msg == "disk full"
    # Cross-thread emit must NOT have invoked the slot on the emitter thread.
    assert slot_tid == main_tid, (
        f"slot ran on emitter thread (tid={slot_tid}) instead of "
        f"main thread (tid={main_tid}) — bridge not on GUI thread?"
    )
    assert emitter_tid and emitter_tid[0] != main_tid


def test_multiple_emits_all_delivered_in_order() -> None:
    """Coalescing is the GUI slot's job, not the bridge's — the bridge
    must deliver every emit in the order issued."""
    app = _ensure_app()
    bridge = _CaptureErrorBridge()
    received: list[str] = []
    bridge.error.connect(received.append)

    def emit_three() -> None:
        bridge.error.emit("a")
        bridge.error.emit("b")
        bridge.error.emit("c")

    t = threading.Thread(target=emit_three)
    t.start()
    t.join()
    deadline = time.monotonic() + 1.0
    while len(received) < 3 and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)

    assert received == ["a", "b", "c"]
