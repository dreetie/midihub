#!/usr/bin/env python3
"""End-to-end check on a real ALSA system: CC -> SysEx through the hub, no MIDI hardware needed.

Uses the ALSA "Midi Through" port, which echoes input -> output, as the
loopback device. Run:  python3 check.py
"""
import time

from rtmidi import MidiIn, MidiOut

from midihub import Hub

CONFIG = {
    "devices": {"vini": {"match": "Midi Through"}, "vout": {"match": "Midi Through"}},
    "routes": [],
    "mappings": [{
        "id": "m1",
        "input": {"device": "vini", "type": "cc", "channel": 0, "cc": 20},
        "output": {"device": "vout", "type": "sysex",
                   "template": [0xF0, 0x7D, 0x01, 0x10, 0x00, 0x00, 0x00, 0xF7],
                   "value_index": 6},
    }],
    "init": {},
    "network": {},
}
TEMPLATE = CONFIG["mappings"][0]["output"]["template"]


def normalize(msg):
    if isinstance(msg, (list, tuple)) and len(msg) == 2 and isinstance(msg[1], float):
        msg = msg[0]
    return msg


def main():
    ports = MidiOut().get_ports()
    idx = next((i for i, p in enumerate(ports) if "Midi Through" in p), None)
    if idx is None:
        raise SystemExit("no 'Midi Through' port found")

    hub = Hub(config=CONFIG)
    time.sleep(1.0)

    out = MidiOut()
    out.open_port(idx)
    inn = MidiIn()
    inn.ignore_types(False, False, False)
    received = []
    inn.set_callback(lambda msg, dt: received.append(normalize(msg)))
    inn.open_port(idx)

    out.send_message([0xB0, 20, 42])
    end = time.time() + 5
    sysex = None
    while time.time() < end:
        sysex = next((list(m) for m in received if m and m[0] == 0xF0), None)
        if sysex:
            break
        time.sleep(0.05)

    expected = TEMPLATE[:6] + [42] + TEMPLATE[7:]
    assert sysex == expected, f"got {sysex}, expected {expected}"
    print("PASS: CC -> SysEx end-to-end through hub")


if __name__ == "__main__":
    main()
