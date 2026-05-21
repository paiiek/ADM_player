# Audio Routing for ADM Player ↔ spatial_engine (Phase A)

ADM Player streams ADM-OSC object metadata to `spatial_engine` over UDP, but
the **multichannel audio** itself still flows through the operating-system
audio stack. The two endpoints have to share the same multichannel device, or
they have to be bridged by a virtual audio router. This page documents the
working setups on Linux, macOS, and Windows for the Phase A demo.

The Phase A topology is:

```
        ADM-OSC (UDP 9100, loopback)
   ┌────────────────────────────────────┐
   │                                    ▼
ADM Player ──── multichannel PCM ───► spatial_engine
(GUI/CLI)      (JACK / BlackHole /     (renders Ambisonic /
               ASIO Link / Dante)       HRTF / speaker bed)
```

Phase B/C (transport sync, PCM IPC) remove the OS-level audio routing — until
those land, this page is the source of truth for getting bits between the
player and the engine.

---

## Common to all platforms

1. Start `spatial_engine_core` first, **bound to localhost**:
   ```bash
   spatial_engine_core --backend null --osc-port 9100 --osc-bind 127.0.0.1 \
                       --osc-dialect adm --channels 8 --rate 48000
   ```
   (`--backend null` runs the engine without an audio device for OSC-only smoke
   tests. Swap in `--backend dante` or your platform-specific backend for live
   audio.)

2. Launch ADM Player and pick the **Spatial Engine (DreamScape)** preset. Host
   and port auto-populate to `127.0.0.1:9100`.

3. Confirm OSC traffic with the bundled smoke test (no engine required):
   ```bash
   python scripts/smoke_spatial_engine.py
   ```

   For a live engine, watch the engine log for `/adm/obj/N/aed` lines.

---

## Linux — JACK or PipeWire (recommended)

Linux desktops with PipeWire ≥ 0.3.50 ship a JACK-compatible API by default,
so the same `jack_lsp` / `qjackctl` tooling works regardless of backend.

```bash
# Confirm PipeWire is exposing JACK
LD_PRELOAD=libjack.so.0 pw-jack jack_lsp
```

Steps:

1. Configure ADM Player's output device to JACK (`--audio-device "system"` on
   CLI, or pick *JACK* from the GUI device dropdown).

2. Run `spatial_engine_core` with the JACK backend in a separate terminal.

3. Patch ports with `qjackctl` or `pw-link`:
   ```bash
   for ch in {1..8}; do
       pw-link "adm-player:output_FL_${ch}" "spatial_engine:input_${ch}"
   done
   ```

4. Verify with `pw-top`: both clients should appear at the configured sample
   rate (48000 by default) with matching block size.

**Pitfall**: PulseAudio's auto-resampler bridges everything to 44.1 kHz unless
you set `default.clock.rate = 48000` in `/etc/pipewire/pipewire.conf.d/`.
Sample-rate mismatch shows up as audible chirps in the engine output, not as
an error.

---

## macOS — BlackHole 16ch loopback

[BlackHole](https://existential.audio/blackhole/) provides a virtual
multichannel device that both apps can simultaneously see.

1. Install BlackHole 16ch from the official installer or `brew install --cask blackhole-16ch`.

2. Create an Aggregate Device in **Audio MIDI Setup** that mux'es BlackHole
   16ch with your physical speakers/HP. This lets the engine record metadata
   while you still hear something.

3. ADM Player: pick "BlackHole 16ch" as the output device. (`--audio-device`
   accepts a substring match — `--audio-device BlackHole` works.)

4. `spatial_engine_core --backend coreaudio --device "BlackHole 16ch"`.

**Pitfall**: BlackHole is *exclusive per direction*. Two apps can both read
from BlackHole, both write to it, but not at the same time. ADM Player must
own the playback (write) side; the engine reads.

---

## Windows — ASIO Link Pro or VB-CABLE

The cleanest path is ASIO Link Pro (free) since it exposes a real ASIO router.
For a quicker no-driver setup, VB-CABLE Multichannel (paid) works.

ASIO Link Pro:

1. Install ASIO Link Pro; it registers a virtual ASIO device.

2. In its routing panel, send ADM Player's output to channels 1-8 of the
   internal bus, then route the same channels back into `spatial_engine`'s
   input.

3. ADM Player: pick the ASIO Link Pro device (sounddevice auto-discovers it
   under the device list). Set block size = 512.

4. `spatial_engine_core --backend asio --device "ASIO Link Pro"` (or whatever
   name `--list-devices` reports).

**Pitfall**: WDM/MME devices in Windows are limited to 2 channels regardless
of physical capability. Always use the ASIO entry of the device, not its WDM
twin. ADM Player's `--list-audio-devices` shows both — pick the one whose name
starts with `[ASIO]`.

---

## Sample-rate and block-size invariants

The engine enforces a fixed sample rate at startup and rejects mismatched
inputs with a runtime error. Set both endpoints to the same:

| Setting           | Recommended | Notes                                |
| ----------------- | ----------- | ------------------------------------ |
| Sample rate       | 48000 Hz    | 96 kHz is supported but rarely worth |
| Audio block size  | 512 frames  | Player default; matches engine's     |
| OSC block size    | 512 frames  | Same param — controls throttle rate  |
| Bit depth         | 24-bit PCM  | Engine quantizes float → PCM24       |

Mismatches between player and engine block size cause OSC packet bursts at
block boundaries; the engine handles them but the resulting jitter is visible
on a scope. Phase C PCM IPC will eliminate this entirely.

---

## Troubleshooting

| Symptom                                       | Likely cause                                  | Fix                                                              |
| --------------------------------------------- | --------------------------------------------- | ---------------------------------------------------------------- |
| Engine logs zero OSC messages                 | Wrong port, or `--osc-bind 0.0.0.0` blocked   | Verify `127.0.0.1:9100`, check firewall                          |
| Audio drops every few seconds                 | Sample-rate or block-size mismatch            | Align both endpoints (see invariants table)                      |
| Engine receives `/adm/obj/N/aed` but no audio | Audio routing not wired                       | Re-check JACK/BlackHole/ASIO patch matrix                        |
| Object position lags audio by ~10 ms          | Expected — Phase A has no transport sync      | Phase B/C fixes this; for now, hear it as "metadata leads audio" |
| Indexes > 64 missing in engine                | Bumped against MAX_OSC_OBJECTS guard          | Player warns once; trim BWF or wait for object mapper            |
| Distance feels too close/too far              | Old build with `/10` heuristic still in path  | Update to >= 0.1.0 — contract is now `/ADM_OSC_MAX_DIST=20`      |

---

## Where this stops working

This document covers Phase A only. The following are *out of scope* until the
respective phase ships:

- **Sample-accurate sync** — Phase C PCM IPC removes the OS audio path entirely.
- **Sample-rate negotiation** — Phase B handshake will let endpoints agree.
- **Multi-host setups** — `--osc-bind 0.0.0.0` works, but lacks auth; not for
  production until ADR-0007 lands.

See `.omc/plans/adm_player_integration.md` for the full roadmap.
