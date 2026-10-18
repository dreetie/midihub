#!/usr/bin/env python3
"""Pure MIDI translation logic (no I/O).

A mapping has an ``input`` spec (what to listen for) and an ``output`` spec
(what to send). Both use the same message types, so any combination works:
MIDI -> MIDI, MIDI -> SysEx, SysEx -> MIDI and SysEx -> SysEx.

Types and the field that holds their number:
    cc         control change      "cc"
    note       note on + note off  "note"
    noteon     note on only        "note"   (kept for older configs)
    noteoff    note off only       "note"
    pc         program change      "pc"
    pressure   channel pressure    -
    polyat     poly aftertouch     "note"
    pitchbend  pitch bend (14 bit) -
    sysex      system exclusive    "template" + "value_index"
"""

STATUS = {
    "cc": 0xB0, "note": 0x90, "noteon": 0x90, "noteoff": 0x80, "pc": 0xC0,
    "pressure": 0xD0, "polyat": 0xA0, "pitchbend": 0xE0, "sysex": 0xF0,
}
TYPES = tuple(STATUS)

# Field holding the "which one" number (CC number, note number, program).
NUMBER_FIELD = {"cc": "cc", "note": "note", "noteon": "note", "noteoff": "note",
                "polyat": "note", "pc": "pc"}


def number_field(t):
    return NUMBER_FIELD.get(t)


def statuses(t):
    """Status nibbles (or 0xF0 for sysex) an input of type t can match."""
    if t == "note":
        return (0x80, 0x90)
    return (STATUS[t],)


def bits(t):
    return 14 if t == "pitchbend" else 7


def _number_of(spec, t):
    f = NUMBER_FIELD.get(t)
    n = spec.get(f) if f else None
    if n is None and t in ("note", "noteon", "noteoff", "polyat"):
        n = spec.get("noteon")  # very old configs used the type as the key
    return n


def match(msg, spec):
    """True if msg bytes match an input spec."""
    if not msg:
        return False
    t = spec.get("type", "cc")
    if t not in STATUS:
        return False
    if t == "sysex":
        if msg[0] != 0xF0:
            return False
        tpl = spec.get("template")
        if not tpl:
            return True
        if len(msg) != len(tpl):
            return False
        vi = spec.get("value_index")
        return all(a == b for i, (a, b) in enumerate(zip(msg, tpl)) if i != vi)
    if msg[0] >= 0xF0 or (msg[0] & 0xF0) not in statuses(t):
        return False
    ch = spec.get("channel")
    if ch is not None and (msg[0] & 0x0F) != (ch & 0x0F):
        return False
    if t == "pc":
        p = spec.get("pc")
        return p is None or (len(msg) > 1 and msg[1] == p)
    n = _number_of(spec, t)
    if n is not None and t not in ("pressure", "pitchbend") and (len(msg) < 2 or msg[1] != n):
        return False
    return True


def value_of(msg, spec):
    """The value carried by msg, in the input type's resolution."""
    t = spec.get("type", "cc")
    if t == "sysex":
        vi = spec.get("value_index")
        return msg[vi] if vi is not None and vi < len(msg) else 0
    if t in ("pc", "pressure"):
        return msg[1] if len(msg) > 1 else 0
    if t == "pitchbend":
        return (msg[1] | (msg[2] << 7)) if len(msg) > 2 else 8192
    if (msg[0] & 0xF0) == 0x80:
        return 0  # note off: treat as release
    return msg[2] if len(msg) > 2 else 0


def is_release(msg):
    """Note off, or note on with velocity 0."""
    if not msg or msg[0] >= 0xF0:
        return False
    hi = msg[0] & 0xF0
    return hi == 0x80 or (hi == 0x90 and len(msg) > 2 and msg[2] == 0)


def scale_value(value, scale, in_bits=7, out_bits=7):
    """Map value to the output range.

    scale = [in_min, in_max, out_min, out_max]; out_min > out_max inverts.
    Without a scale only the resolution is converted (7 <-> 14 bit).
    """
    out_max_v = (1 << out_bits) - 1
    if scale and len(scale) == 4:
        i0, i1, o0, o1 = scale
        if i1 == i0:
            v = o0
        else:
            v = o0 + (value - i0) * (o1 - o0) / (i1 - i0)
            lo, hi = min(o0, o1), max(o0, o1)
            v = min(max(v, lo), hi)
        v = int(round(v))
    elif in_bits != out_bits:
        in_max_v = (1 << in_bits) - 1
        v = int(round(value * out_max_v / in_max_v))
    else:
        v = int(value)
    return min(max(v, 0), out_max_v)


def build_sysex(template, value_index, value, scale=None):
    """Template with value written at value_index (None = send as-is)."""
    out = list(template)
    if value_index is not None and 0 <= value_index < len(out):
        out[value_index] = scale_value(value, scale) & 0x7F
    return out


def render(outp, value, in_msg=None, in_spec=None):
    """Build output bytes for an already scaled value."""
    t = outp.get("type", "sysex")
    if t == "sysex":
        tpl = outp.get("template") or []
        vi = outp.get("value_index")
        out = list(tpl)
        if vi is not None and 0 <= vi < len(out):
            out[vi] = value & 0x7F
        return out or None
    if t not in STATUS:
        return None
    in_t = (in_spec or {}).get("type")
    in_is_channel = in_msg and in_msg[0] < 0xF0
    ch = outp.get("channel")
    if ch is None:
        ch = (in_msg[0] & 0x0F) if in_is_channel else 0
    ch &= 0x0F
    # Number: explicit, or reuse the input's number when both have one.
    n = _number_of(outp, t)
    if n is None and in_is_channel and in_t in NUMBER_FIELD and t in NUMBER_FIELD and len(in_msg) > 1:
        n = in_msg[1]
    n = (n or 0) & 0x7F
    v = value
    if t == "cc":
        return [0xB0 | ch, n, v & 0x7F]
    if t in ("note", "noteon", "noteoff"):
        release = t == "noteoff" or (in_msg is not None and is_release(in_msg)) or v == 0
        if release:
            return [0x80 | ch, n, 0 if t != "noteoff" else v & 0x7F]
        return [0x90 | ch, n, v & 0x7F]
    if t == "polyat":
        return [0xA0 | ch, n, v & 0x7F]
    if t == "pc":
        p = outp.get("pc")
        return [0xC0 | ch, (p if p is not None else v) & 0x7F]
    if t == "pressure":
        return [0xD0 | ch, v & 0x7F]
    if t == "pitchbend":
        return [0xE0 | ch, v & 0x7F, (v >> 7) & 0x7F]
    return None


def translate(msg, inp, outp):
    """Translate one input message to output bytes, or None if it doesn't match."""
    if not match(msg, inp):
        return None
    in_t = inp.get("type", "cc")
    out_t = outp.get("type", "sysex")
    value = value_of(msg, inp)
    value = scale_value(value, outp.get("scale"), bits(in_t), bits(out_t))
    return render(outp, value, msg, inp)


def detect_value_index(messages):
    """Index of the single byte that varies across all sysex messages; None if ambiguous."""
    if not messages:
        return None
    first = messages[0]
    if any(len(m) != len(first) for m in messages):
        return None
    varying = [i for i in range(len(first)) if any(m[i] != first[i] for m in messages[1:])]
    return varying[0] if len(varying) == 1 else None


if __name__ == "__main__":
    tpl = [0xF0, 0x7D, 0x01, 0x10, 0x00, 0xF7]
    m = {"type": "cc", "channel": 0, "cc": 20}
    o = {"type": "sysex", "template": tpl, "value_index": 4}
    assert translate([0xB0, 20, 42], m, o) == [0xF0, 0x7D, 0x01, 0x10, 42, 0xF7]
    assert translate([0xB0, 20, 42], {**m, "channel": 1}, o) is None
    assert translate([0xB0, 21, 42], m, o) is None
    assert build_sysex(tpl, 4, 127, [0, 127, 0, 63])[4] == 63
    # SysEx -> CC
    assert translate([0xF0, 0x7D, 0x01, 0x10, 99, 0xF7], o, {"type": "cc", "cc": 7}) == [0xB0, 7, 99]
    # CC -> CC remap, channel kept
    assert translate([0xB3, 1, 10], {"type": "cc", "cc": 1}, {"type": "cc", "cc": 74}) == [0xB3, 74, 10]
    assert detect_value_index([[1, 2, 3], [1, 5, 3]]) == 1
    assert detect_value_index([[1, 2, 3], [1, 5, 7]]) is None
    print("translator self-check OK")
