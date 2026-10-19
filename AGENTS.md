# AGENTS.md: notes for contributors and coding agents

## Layout
- `midihub.py`: the daemon. Port discovery, ALSA direct routes (`aconnect`),
  input polling, translation dispatch, learn buffer, status.
- `translator.py`: pure message matching/translation (no I/O). Self-check:
  `python3 translator.py`.
- `guard.py`: loop protection. `plan()` analyses the routing graph;
  `LoopGuard` detects echoes and floods at runtime. Pure logic.
- `web.py`: entry point. Runs the hub in the main thread and Flask in a daemon
  thread; login, CLI (`--no-web`, `--set-password`, `--clear-password`).
- `static/`: single-page UI, plain JS, no build step. Same design language as
  wled-midi-bridge (tokens in `app.css`).
- `tests/`: `python3 -m unittest discover -s tests -t .`. Uses
  `tests/fake_rtmidi.py`, so no ALSA or hardware is needed.
- `check.py`: end-to-end on a real ALSA host through the "Midi Through" port.
- `setup.sh` / `update.sh` / `uninstall.sh` + `*.service`: Debian-family install
  to `/opt/midihub`, `midihub` system user, systemd.

Personal configs and notes go in `config.json` and `personal/`; both are
git-ignored. Never commit device-specific setups, paths, hostnames or users.

## Design decisions
- Engine: custom Python on `python-rtmidi` with a JSON config that hot-reloads
  (a DSL like mididings would need regeneration + restart per edit).
- Direct routes are ALSA subscriptions via `aconnect`, so passthrough never
  touches Python. Don't move them into Python; the latency win is the point.
  A route only goes through Python ("filtered") when a mapping with
  `block_original` must hold messages back on it.
- midihub owns connections between the devices it manages: on every port
  layout change it removes unwanted ones (stale loops after a crash), and it
  removes its own on exit (`Hub.close`, SIGTERM handler in `web.py`).
- Init messages are sent when a device's output ports appear or its init list
  changes, never on every rescan.
- Mappings are indexed by (device, status nibble) when the config is applied,
  so traffic without a mapping costs a dict lookup.
- Web UI and hub must be one process: two processes can't both own the
  hardware ports (double sends). A web crash restarts only the web thread.
- Web defaults to 127.0.0.1 without a password; binding to the network
  without one is refused unless `allow_no_password`. Security settings are
  never writable from the browser (`/api/save` keeps the server's `web`).

## python-rtmidi / ALSA quirks (verified, don't re-derive)
- python-rtmidi leaks one ALSA seq client every time a MidiIn/MidiOut is
  destroyed. midihub pools handles per port name and never destroys them for a
  live port. Port names include `client:port`, so a replugged device gets a new
  name and a fresh handle. One MidiIn and one MidiOut are reused for
  enumeration.
- In callback mode the ALSA backend drops SysEx (seen on 1.5.8). Inputs are
  polled with `get_message()` in one thread per port, napping 1 ms only when
  idle.
- The ALSA MidiIn queue defaults to 1024 messages and overflows with active
  sensing + SysEx bursts ("message queue limit reached"). The third
  constructor argument raises it: `MidiIn(API_LINUX_ALSA, "midihub-in", 100000)`;
  import `API_LINUX_ALSA` explicitly.
- A device replugged quickly can come back on the same ALSA card, so with the
  same `client:port` and port name, while every subscription to the old port
  is gone. midihub tells the two apart with the card's USB device number
  (`/proc/asound/cardN/usbbus`, new on every plug-in; card clients are
  16 + 4 * card) and then reopens handles, reconnects routes and resends init.
- Open a port by the index from the opening handle's own `get_ports()`, never
  from an earlier probe: indexes shift while devices are being plugged in.
- `MidiOut.get_ports()` lists writable ports, `MidiIn.get_ports()` readable
  ones; virtual ports can be one-directional, so both lists are needed.
- `snd-virmidi` works for testing, but its rawmidi side is unreliable where
  PipeWire grabs it; "Midi Through" is the dependable loopback.
