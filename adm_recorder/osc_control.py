from __future__ import annotations

from pythonosc.dispatcher import Dispatcher
from PySide6.QtCore import QObject, Signal

from .osc_udp_server import start_threading_osc_udp


class OscControlBridge(QObject):
    """Relay record/stop requests from OSC thread to the GUI thread."""

    record_requested = Signal()
    stop_requested = Signal()


def start_osc_control_server(host: str, port: int, bridge: OscControlBridge):
    """
    Listen for ``/record`` and ``/stop`` on ``host:port``.
    Must use a different port than position OSC.
    """
    d = Dispatcher()

    def _record(_addr: str, *_args) -> None:
        bridge.record_requested.emit()

    def _stop(_addr: str, *_args) -> None:
        bridge.stop_requested.emit()

    d.map("/record", _record)
    d.map("/stop", _stop)

    return start_threading_osc_udp((str(host), int(port)), d)
