"""Threaded UDP OSC listener with SO_REUSEADDR for faster rebinding after stop."""

from __future__ import annotations

import socketserver
import threading
from typing import Tuple

from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import OSCUDPServer


class ReuseThreadingOSCUDPServer(socketserver.ThreadingMixIn, OSCUDPServer):
    """
    Same as pythonosc ThreadingOSCUDPServer, with allow_reuse_address enabled.
    Helps when restarting recording quickly on macOS/Linux after closing the socket.
    """

    allow_reuse_address = True
    daemon_threads = True


def start_threading_osc_udp(
    address: Tuple[str, int],
    dispatcher: Dispatcher,
) -> tuple[ReuseThreadingOSCUDPServer, threading.Thread]:
    srv = ReuseThreadingOSCUDPServer(address, dispatcher)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, th
