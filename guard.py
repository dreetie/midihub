#!/usr/bin/env python3
"""Loop and feedback protection (pure logic, no I/O).

Two layers:

1. ``plan()`` looks at the routing graph before anything is connected. A route
   that would close a loop (A -> B -> A, or longer) is blocked by default and
   reported, so a loop never gets wired up in the first place. Mappings and
   network export that close a loop are reported as warnings.

2. ``LoopGuard`` watches live traffic. Every message midihub forwards to a
   device is remembered for a few milliseconds; when the same bytes come back
   *from* that device, that's an echo. A burst of echoes from a device that also
   sends somewhere means MIDI is going round in circles, so that device's
   outgoing routes and mappings are muted for a few seconds. A device sending
   an unusual number of messages per second raises a flood warning.
"""
import itertools
import time
from collections import deque

NETWORK_DEV = "qmidinet"

DEFAULTS = {
    "block_cycles": True,      # refuse to wire routes that close a loop
    "echo_guard": True,        # detect messages bouncing back and mute the source
    "echo_window_ms": 50,      # how long a forwarded message counts as "just sent"
    "echo_trip": 25,           # echoes per second that count as a loop
    "flood_limit": 2000,       # messages per second per device before warning
    "flood_mute": False,       # also mute a flooding device's outputs
    "mute_seconds": 5,         # how long a tripped device stays muted
}


def settings(config):
    lg = (config or {}).get("loop_guard") or {}
    return {k: lg.get(k, v) for k, v in DEFAULTS.items()}


def _path(adj, start, goal):
    """A path start -> ... -> goal in adj (list of nodes) or None."""
    if start == goal:
        return [start]
    seen = {start}
    stack = [(start, [start])]
    while stack:
        node, path = stack.pop()
        for nxt in sorted(adj.get(node, ())):
            if nxt == goal:
                return path + [nxt]
            if nxt not in seen:
                seen.add(nxt)
                stack.append((nxt, path + [nxt]))
    return None


def mapping_targets(m, routes_from):
    out = m.get("output") or {}
    dev = (m.get("input") or {}).get("device")
    if out.get("device"):
        return [out["device"]]
    return list(routes_from.get(dev, []))


def plan(config):
    """Decide which routes are wired directly, which go through midihub
    (filtered), which are blocked, and what to warn about."""
    cfg = config or {}
    lg = settings(cfg)
    devices = set((cfg.get("devices") or {}).keys())

    edges = []
    for r in cfg.get("routes") or []:
        a = r.get("from")
        for b in r.get("to") or []:
            if a in devices and b in devices and a != b and (a, b) not in edges:
                edges.append((a, b))

    adj = {}
    accepted, blocked, warnings = [], [], []
    for a, b in edges:
        back = _path(adj, b, a)
        if back is not None:
            cycle = [a] + back
            if lg["block_cycles"]:
                blocked.append({"from": a, "to": b, "cycle": cycle})
                warnings.append({"level": "err", "kind": "route-loop", "devices": cycle,
                                 "text": f"Route {a} → {b} was not connected: it would make a loop "
                                         f"({' → '.join(cycle)})."})
                continue
            warnings.append({"level": "warn", "kind": "route-loop", "devices": cycle,
                             "text": f"Routes form a loop: {' → '.join(cycle)}. Loop blocking is off, "
                                     f"the echo guard is your only protection."})
        adj.setdefault(a, set()).add(b)
        accepted.append((a, b))

    routes_from = {}
    for a, b in accepted:
        routes_from.setdefault(a, []).append(b)

    # Routes whose source has a "replace original" mapping towards that target
    # go through midihub so the original message can be held back.
    filtered = set()
    for m in cfg.get("mappings") or []:
        if not (m.get("output") or {}).get("block_original"):
            continue
        src = (m.get("input") or {}).get("device")
        for t in mapping_targets(m, routes_from):
            if (src, t) in accepted:
                filtered.add((src, t))

    # Mappings and network export: report loops, don't block (they only carry
    # some messages, and the live echo guard covers them).
    full = {k: set(v) for k, v in adj.items()}
    for i, m in enumerate(cfg.get("mappings") or []):
        src = (m.get("input") or {}).get("device")
        for t in mapping_targets(m, routes_from):
            if src == t:
                warnings.append({"level": "warn", "kind": "mapping-loop", "devices": [src], "mapping": i,
                                 "text": f"Mapping {i + 1} sends back to {src}, the device it listens to. "
                                         f"Fine for LED or motor-fader feedback, risky otherwise."})
                continue
            back = _path(full, t, src)
            if back is not None:
                cycle = [src] + back
                warnings.append({"level": "warn", "kind": "mapping-loop", "devices": cycle, "mapping": i,
                                 "text": f"Mapping {i + 1} ({src} → {t}) can feed back through "
                                         f"{' → '.join(cycle)}."})
            full.setdefault(src, set()).add(t)

    export = [d for d in ((cfg.get("network") or {}).get("export") or []) if d in devices]
    for a, b in itertools.permutations(export, 2):
        if (a, b) in accepted:
            warnings.append({"level": "warn", "kind": "network-loop", "devices": [a, b, NETWORK_DEV],
                             "text": f"{a} and {b} are both shared on the network and routed to each other; "
                                     f"messages can come back to {a} twice."})

    return {
        "direct": [e for e in accepted if e not in filtered],
        "filtered": sorted(filtered),
        "blocked": blocked,
        "warnings": warnings,
        "routes_from": routes_from,
    }


class LoopGuard:
    """Live echo and flood detector. Not thread-safe by itself; the caller
    serialises calls (midihub calls it under its own lock)."""

    def __init__(self, config=None, clock=time.monotonic):
        self.clock = clock
        self.cfg = dict(DEFAULTS)
        self._recent = {}        # dest -> {bytes: t}
        self._echo = {}          # dev -> [window_start, count]
        self._rate = {}          # dev -> [window_start, count]
        self.rates = {}          # dev -> last full-second message count
        self.muted = {}          # dev -> until (float('inf') = until reset)
        self._trips = {}         # dev -> deque of trip times
        self._flood_warned = {}  # dev -> t
        self.alerts = deque(maxlen=30)
        self.on_alert = None     # optional callback(alert), e.g. to log it
        self._ids = itertools.count(1)
        self.configure(config)

    def configure(self, config):
        self.cfg = settings(config)

    # ---------------------------------------------------------- recording
    def forwarded(self, dest, data, now=None):
        """midihub (or a direct route) just delivered data to dest."""
        if not self.cfg["echo_guard"] or not data or data[0] >= 0xF8:
            return
        now = self.clock() if now is None else now
        rec = self._recent.setdefault(dest, {})
        rec[bytes(data)] = now
        if len(rec) > 512:
            cutoff = now - self.cfg["echo_window_ms"] / 1000.0
            for k in [k for k, t in rec.items() if t < cutoff]:
                del rec[k]

    def incoming(self, dev, data, has_outputs=True, now=None):
        """Account for a message from dev. Returns (is_echo, tripped)."""
        now = self.clock() if now is None else now
        cfg = self.cfg
        tripped = False

        w = self._rate.setdefault(dev, [now, 0])
        if now - w[0] >= 1.0:
            self.rates[dev] = w[1]
            w[0], w[1] = now, 0
        w[1] += 1
        if w[1] > cfg["flood_limit"]:
            last = self._flood_warned.get(dev, -1e9)
            if now - last > 10:
                self._flood_warned[dev] = now
                self._alert("warn", "flood", dev,
                            f"{dev} is sending more than {cfg['flood_limit']} messages per second.")
                if cfg["flood_mute"] and has_outputs:
                    tripped = self._trip(dev, now, "flood")

        is_echo = False
        if cfg["echo_guard"] and data and data[0] < 0xF8:
            t = self._recent.get(dev, {}).get(bytes(data))
            if t is not None and now - t <= cfg["echo_window_ms"] / 1000.0:
                is_echo = True
                e = self._echo.setdefault(dev, [now, 0])
                if now - e[0] >= 1.0:
                    e[0], e[1] = now, 0
                e[1] += 1
                if e[1] >= cfg["echo_trip"] and has_outputs and not self.is_muted(dev, now):
                    e[1] = 0
                    tripped = self._trip(dev, now, "loop") or tripped
        return is_echo, tripped

    # ---------------------------------------------------------- muting
    def _trip(self, dev, now, why):
        trips = self._trips.setdefault(dev, deque(maxlen=3))
        trips.append(now)
        latched = len(trips) == 3 and now - trips[0] < 60
        self.muted[dev] = float("inf") if latched else now + self.cfg["mute_seconds"]
        if latched:
            text = (f"{dev} kept looping, so its outputs stay muted until you press Reset. "
                    f"Check for a route or device setting (MIDI thru, echo) that sends MIDI back.")
        elif why == "loop":
            text = (f"MIDI loop detected: messages sent to {dev} came straight back. "
                    f"Its outputs are muted for {self.cfg['mute_seconds']} s.")
        else:
            text = f"{dev} is flooding; its outputs are muted for {self.cfg['mute_seconds']} s."
        self._alert("err", why, dev, text)
        return True

    def is_muted(self, dev, now=None):
        until = self.muted.get(dev)
        if until is None:
            return False
        now = self.clock() if now is None else now
        if now >= until:
            del self.muted[dev]
            return False
        return True

    def expire(self, now=None):
        """Drop finished mutes; True if any device was unmuted."""
        now = self.clock() if now is None else now
        done = [d for d, u in self.muted.items() if now >= u]
        for d in done:
            del self.muted[d]
        return bool(done)

    def reset(self):
        self.muted.clear()
        self._trips.clear()
        self._echo.clear()
        self.alerts.clear()

    def _alert(self, level, kind, dev, text):
        alert = {"id": next(self._ids), "t": time.time(), "level": level,
                 "kind": kind, "device": dev, "text": text}
        self.alerts.append(alert)
        if self.on_alert:
            self.on_alert(alert)

    def snapshot(self, now=None):
        now = self.clock() if now is None else now
        self.expire(now)
        return {
            "muted": {d: (None if u == float("inf") else round(u - now, 1)) for d, u in self.muted.items()},
            "alerts": list(self.alerts),
            "rates": dict(self.rates),
        }
