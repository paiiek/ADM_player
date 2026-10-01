"""Pure parsing of the engine's ``/sys/warning`` OSC payload.

Kept out of ``gui_app`` (which imports PySide6 and sounddevice/PortAudio at
module load) so the parser can be unit-tested headless — P-171.
"""

from __future__ import annotations


def parse_sys_warning_args(args: list) -> tuple[str, str]:
    """Extract ``(category, detail)`` from a ``/sys/warning`` payload.

    Wire shapes observed (docs/ipc_schema.md, EchoSubscriber.h:19): the common
    one is ``,iiss <int> <int> "category" "detail"`` (e.g.
    ``echo_rate_limited`` / ``"dropped=N"``), but some emitters send just
    ``,s "category"`` with no detail string. Pull out the string arguments in
    order rather than assuming a fixed arity, so either shape degrades to a
    readable category with an empty/partial detail instead of raising.
    """
    strings = [a for a in args if isinstance(a, str)]
    category = strings[0] if strings else "unknown"
    detail = strings[1] if len(strings) > 1 else ""
    return category, detail
