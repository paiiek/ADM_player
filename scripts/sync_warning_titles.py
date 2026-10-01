#!/usr/bin/env python3
"""Check (default) or refresh (--write) adm_recorder/warning_titles.json against
the spatial_engine checkout's ui/warning_catalog.json — P-175.

    python scripts/sync_warning_titles.py --engine-root /path/to/spatial_engine
    python scripts/sync_warning_titles.py --engine-root /path/to/spatial_engine --write

Exit 0 = in sync (or written), 1 = drift (listed), 2 = engine catalog unreadable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adm_recorder.warning_catalog import (  # noqa: E402
    SNAPSHOT_PATH,
    SNAPSHOT_SCHEMA,
    snapshot_drift,
    titles_from_engine_catalog,
)


def _engine_commit(root: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(root), "rev-parse", "--short=8", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--engine-root", type=Path, required=True)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)

    src = args.engine_root / "ui" / "warning_catalog.json"
    try:
        raw = src.read_bytes()
        engine = titles_from_engine_catalog(json.loads(raw))
    except (OSError, ValueError, KeyError) as e:
        print(f"cannot read engine catalog {src}: {e}", file=sys.stderr)
        return 2

    if args.write:
        snap = {
            "schema": SNAPSHOT_SCHEMA,
            "_comment": "VENDORED from spatial_engine ui/warning_catalog.json (titles only). "
                        "Do not edit by hand: scripts/sync_warning_titles.py --write.",
            "source_commit": _engine_commit(args.engine_root),
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "codes": engine,
        }
        SNAPSHOT_PATH.write_text(json.dumps(snap, ensure_ascii=False, indent=1) + "\n",
                                 encoding="utf-8")
        print(f"wrote {SNAPSHOT_PATH} ({len(engine)} codes)")
        return 0

    snap = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    drift = snapshot_drift(snap["codes"], engine)
    if drift:
        print(f"{len(drift)} difference(s) between {SNAPSHOT_PATH.name} and {src}:")
        for d in drift:
            print(f"  {d}")
        print("refresh with --write")
        return 1
    print(f"in sync: {len(engine)} codes ({src})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
