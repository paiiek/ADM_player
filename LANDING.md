# LANDING — lane-companion

Branch: `fix/companion-p33-p37-p48`, off `fix/osc-seqid-vestigial-comment`
@ `4756379`.

## P-33 — ARM64 memory fence missing in MetaRingSink.publish() (MED, partial fix carried)

Register note at hand-off: "ipc_sink fence committed (dreamscape `b5e459d`);
`meta_sink.py:321` still `sched_yield`, no fence (ARM64 Mac)". `ipc_sink.py`
already carries a reusable cross-process `_fence()`/`FENCE_IMPL` (resolved via
`atomic_thread_fence`/Darwin barrier/pthread-mutex fallback — see its
`_resolve_fence` docstring) matching the C++ consumer's release/acquire wire
contract. `meta_sink.py`'s `publish()` used `os.sched_yield()` — a scheduler
hint, not a barrier — before the `write_idx` store, and `_set_state()` had no
barrier at all before the `producer_state` store. Both are read with
`std::memory_order_acquire` by the C++ consumer
(`MetaRingConsumer.cpp:171/252/254/285` for `write_idx`, `:322` for
`producer_state`) — correct only by accident on x86-64 TSO, wrong on ARM64
(the operator's Mac).

Fix: import and reuse `ipc_sink._fence` in `meta_sink.py` (no duplicated
barrier logic); call it immediately before each store.

Commit: `dbd9cfb`.

### Tests

Added to `tests/test_meta_sink.py` (mirrors `test_ipc_sink.py`'s existing
`test_publish_order_write_idx_last` / fence-impl tests):
- `test_publish_order_write_idx_last` — asserts the fence fires strictly
  before the `write_idx` store.
- `test_set_state_fences_before_producer_state_store` — same for
  `_set_state`.
- `test_fence_impl_is_a_real_barrier` — `FENCE_IMPL` names a real mechanism.

```
python3 -m pytest -q tests/test_meta_sink.py tests/test_ipc_sink.py
# 50 passed
```

### Residuals

- None for the fence itself. The register also mentioned the blocking report
  wrongly claiming dreamscape "is not a git repo" — it plainly is
  (`.git` + `origin` remote present); no action needed there, it was a stale
  observation, not a defect in this repo.

## P-37 — no `/sys/warning` handler in the recorder (LOW)

The recorder (`adm_recorder/gui_app.py`) had no explicit handling for
`/sys/warning`: it fell through `OscIngestRouter`'s `set_default_handler`
fallback into `_on_osc_raw`, which only appends it to the 800-line raw OSC
log widget (`QPlainTextEdit`, `setMaximumBlockCount(800)`) — the same widget
that carries the full echo/position firehose during capture. That firehose is
heaviest exactly when `echo_rate_limited` fires (the engine's rate-limit
warning only fires under load), so the warning routinely scrolled past
unseen; nothing marked it distinct from ordinary position traffic.

Fix: `_on_osc_raw` now recognizes `addr == "/sys/warning"`, parses its
category/detail strings via a new `_parse_sys_warning_args` helper (tolerant
of both the common `,iiss <int> <int> "category" "detail"` shape used by
`echo_rate_limited` and a bare `,s "category"` shape some emitters use), and
emits a `WARN`-level log line plus an 8s status-bar message through a new
`_EngineWarningBridge` (`QObject` + `Signal(str, str)`, thread-marshaled the
same way as the existing `_OscLogBridge`/`_CaptureErrorBridge`).

Commit: `c2f1a7c`.

### Tests / verification

**Not unit-tested**: `adm_recorder.gui_app` cannot be imported in this
environment — `import sounddevice as sd` at module scope raises `OSError:
PortAudio library not found`. This is pre-existing and environmental (see
`tests/test_audio_recorder.py` / `tests/test_recorder_gui_threadsafe.py`,
which fail the same way at collection, with or without this change). Verified
with `python3 -c "import ast; ast.parse(open('adm_player/gui_app.py' [sic
adm_recorder]).read())"` for syntax only, and by reading the Qt
signal/thread-marshaling pattern against the existing bridges in the same
file (`_OscLogBridge`, `_CaptureErrorBridge`) to confirm it is thread-safe
the same way they are.

### Residuals

- If a box in CI/dev ever has PortAudio installed, `test_recorder_gui_threadsafe.py`
  would be the natural place to add a `_on_osc_raw("/sys/warning", [...])` →
  `_EngineWarningBridge.warning` → `_log_ui`/`_status` assertion. Flagging
  this rather than silently skipping it (no silent disarm).

## P-48 (spatial_engine side, not this repo)

Landed on `spatial_engine-proto` (clone `/home/seung/mmhoa/al-companion`,
branch `al/companion`, commit `1bf1bc1a`): `adm_bwf.py` converts ADM
`<width>` (degrees for polar, relative units for Cartesian — BS.2076) to
`width_rad` instead of passing the raw number straight through. See that
clone's own `LANDING.md` for detail.

## General

No pytest regressions found. Full dreamscape suite, excluding the two
PortAudio-dependent files (environmental, unrelated to this lane):

```
python3 -m pytest -q tests/ --ignore=tests/test_audio_recorder.py --ignore=tests/test_recorder_gui_threadsafe.py
# 164 passed
```

No push performed (per instructions). Register rows P-33/P-37/P-48 are ready
for lead reconciliation against these branches/SHAs.
