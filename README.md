# ADM Player & Recorder (DreamScape)

ADM BWF playback + ADM-OSC metadata streaming, plus a companion recorder for
capturing live OSC into a Dolby/Nuendo-compatible ADM master.

The player drives any ADM-OSC v1.0 endpoint — L-ISA, Spat Revolution,
Soundscape, AFC Image, and the in-house `spatial_engine` renderer — from
the same multichannel WAV.

## Install

Python ≥ 3.10. From the repository root:

```bash
pip install -e .[gui,packaging]
```

`gui` pulls PySide6 + psutil; `packaging` pulls PyInstaller for native bundles.
Headless installs (CLI only) can omit both extras.

## Run

CLI:

```bash
# Stream a master to spatial_engine on the default port
adm-player path/to/master.wav --osc-host 127.0.0.1 --osc-port 9100

# Inspect axml/chna without rendering audio
adm-player path/to/master.wav --dry-run

# Interactive — pick channels, scrub, hot-toggle OSC
adm-player path/to/master.wav --interactive

# Show the audio output device table
adm-player --list-audio-devices
```

GUI:

```bash
adm-player-gui      # player
adm-recorder-gui    # recorder
```

## OSC preset matrix

| Preset           | Address shape                          | Distance contract                     | Notes                                          |
| ---------------- | -------------------------------------- | ------------------------------------- | ---------------------------------------------- |
| `adm` (default)  | `/adm/obj/N/{aed,xyz}`                 | normalized [0,1], > 1 = `/20` clamp   | ADM-OSC v1.0                                   |
| `spatial_engine` | `/adm/obj/N/{aed,xyz}`                 | same as `adm`                         | Auto endpoint `127.0.0.1:9100`, MAX_OBJECTS=64 |
| `spat_revolution`| `/source/N/xyz`                        | scaled by 10                          | xyz-only                                       |
| `lisa`           | `/ext/src/N/pwdes`                     | distance mapped to L-ISA [0.1, 1.0]   | Bundles az/width/dist/el/aux into one packet   |
| `soundscape`     | `/dbaudio1/positioning/source_position`| scaled by 10, z forced to 0           | 2D bed only                                    |
| `adamson_fm`     | `/fm/obj/pos/xyz/N`                    | normalized                            | Fletcher Machine                               |
| `afc_image`      | `/yosc:req/set/.../PhysicalPosition/N` | scaled by 10                          | AFC Image                                      |
| `custom`         | user template                          | as configured                         | `{i}` substitutes object index                 |

Switch presets at runtime; switching between polar and cartesian per block is
handled (cache is invalidated on transition so the first post-switch packet
always goes out).

## spatial_engine integration

The `spatial_engine` preset speaks ADM-OSC v1.0 verbatim — same wire format as
`adm`, with a default endpoint of `127.0.0.1:9100`.

```bash
# Terminal 1: engine
spatial_engine_core --backend null --osc-port 9100 --osc-bind 127.0.0.1 \
                    --osc-dialect adm --channels 8 --rate 48000

# Terminal 2: player
adm-player dreamscape/01.wav --osc-host 127.0.0.1 --osc-port 9100

# Terminal 3 (optional): contract smoke (no engine required)
python scripts/smoke_spatial_engine.py
```

The full integration roadmap is in
[`.omc/plans/adm_player_integration.md`](.omc/plans/adm_player_integration.md);
audio routing recipes for Linux / macOS / Windows are in
[`docs/audio_routing.md`](docs/audio_routing.md).

### Known limits (Phase A)

- **MAX_OSC_OBJECTS = 64**: masters with > 64 audio objects log a one-line
  warning and only the first 64 slots stream OSC metadata. Audio playback is
  unaffected. Object mapping for > 64 lands in Phase B.
- **No sample-accurate sync**: OSC packets arrive ~1 audio block ahead of the
  PCM (~10 ms at 48 kHz/512). Phase B adds a transport timetag; Phase C moves
  to PCM IPC and removes the drift entirely.
- **Bed channels not OSC-streamed**: only `typeDefinition=objects` tracks emit
  OSC. Bed channels route to the engine's speaker layout via the audio path.

## Coordinate convention

- `azimuth`: degrees, LEFT positive (ADM-OSC / AmbiX standard, `azimuth_flip=False`).
- `elevation`: degrees, UP positive, `[-90, +90]`.
- `distance`: normalized `[0, 1]`; values > 1 are treated as meters and
  divided by `ADM_OSC_MAX_DIST = 20` (aligned with `spatial_engine` ADR 0006).
- Cartesian: `+X = right`, `+Y = front`, `+Z = up`; player emits in `[-1, 1]`
  cube, engine receives in the same convention.

Use `--flip-azimuth` to flip for legacy targets that expect right-positive.
`--azimuth-offset DEG` adds a constant rotation.

## Recorder

`adm-recorder-gui` captures live ADM-OSC traffic and a multichannel audio
stream into a single Nuendo/Pro Tools-compatible ADM BWF master:

- EBU `ebuCoreMain` axml + `chna` index per WAV channel
- Bed layouts: stereo, 5.1, 7.1, 7.1.2, 7.1.4 (top labels distinguish front
  vs rear pairs — `Ltf/Rtf` vs `Ltr/Rtr`)
- Live OSC events compacted into `audioBlockFormat` with the per-sample-rate
  `jumpPosition` interpolation length

The recorder accepts the same preset OSC sources as the player can emit.

## Tests

```bash
pytest tests/ -v
```

The OSC integration suite (`tests/test_osc_integration.py`) covers the
contracts that gate Phase A: distance normalization, mode-transition cache
invalidation, 7.1.4 bed-label correctness, the `MAX_OSC_OBJECTS` guard, and
the `spatial_engine` preset wire format.

## Packaging

PyInstaller specs live under `packaging/`:

```bash
python -m PyInstaller packaging/adm_player_mac.spec       # → ADM Player.app
python -m PyInstaller packaging/adm_player_linux.spec     # → dist/adm-player/
python -m PyInstaller packaging/adm_player_win.spec       # → dist/adm-player/
```

The macOS spec produces a notarizable `.app` bundle. The Linux and Windows
specs ship a `COLLECT` directory; bundle further into AppImage or NSIS as
needed.
