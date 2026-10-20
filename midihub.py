#!/usr/bin/env python3
"""midihub daemon: headless USB-MIDI router + translator. Config hot-reloads.

Two paths for MIDI:

* Direct routes are ALSA sequencer subscriptions made with ``aconnect``. The
  kernel delivers those messages itself; they never touch Python, so they add
  no latency. This is the default for every route.
* The translation path: midihub listens to every device input, matches mappings
  and sends the translated message. Routes that must hold back some messages
  ("replace original" mappings) and network export also run here.
"""
import atexit
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque

from rtmidi import API_LINUX_ALSA, MidiIn, MidiOut

from guard import NETWORK_DEV, LoopGuard, mapping_targets, plan
from translator import STATUS, match, statuses, translate

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("MIDIHUB_CONFIG", os.path.join(HERE, "config.json"))

DEFAULT = {"devices": {}, "routes": [], "mappings": [], "init": {}, "network": {}}


def _client_port(port_name):
    """ALSA client:port from an rtmidi port name ('Synth:Synth MIDI 1 24:0' -> '24:0')."""
    found = re.findall(r"(\d+):(\d+)", port_name)
    return ":".join(found[-1]) if found else port_name


def _port_token(port_name):
    """Something that changes when the USB device behind a port is replugged.

    A device that comes back on the same ALSA card gets the same client:port,
    so the same port name. Its USB device number (/proc/asound/cardN/usbbus)
    is new on every plug-in, which tells a replug apart from "still there".
    Card clients are numbered 16 + 4 * card. None for non-USB ports."""
    try:
        client = int(_client_port(port_name).split(":")[0])
    except ValueError:
        return None
    if not 16 <= client < 128:
        return None
    try:
        with open(f"/proc/asound/card{(client - 16) // 4}/usbbus") as f:
            return f.read().strip()
    except OSError:
        return None


def _log(text):
    # Goes to the journal under systemd (stdout is a pipe there, so flush).
    print(f"midihub: {text}", flush=True)


class Hub:
    def __init__(self, config_path=CONFIG_PATH, config=None):
        if config is not None:
            config_path = None
        self.config_path = config_path
        self.config = dict(DEFAULT)
        self.lock = threading.RLock()
        self.ins = {}
        self.outs = {}
        self.port_dev = {}
        self.port_names = {}
        self.activity = {}
        self.guard = LoopGuard()
        self.guard.on_alert = lambda a: _log(a["text"])
        self.plan = plan({})
        self._key = None
        self._mtime = None
        self._dev_in = {}          # device -> readable port names
        self._dev_out = {}         # device -> writable port names
        self._map_index = {}       # (device, status) -> [(input, output, targets)]
        self._filtered_from = {}   # device -> [targets] routed through Python
        self._direct_from = {}     # device -> [targets] routed by ALSA
        self._held = {}            # device -> [(input spec, target set)] of "replace original" mappings
        self._has_outputs = set()
        self._exported_list = []
        self._init_sent = {}       # device -> (out ports, init messages) last sent
        self._learn = None
        self._learn_seq = 0
        self._cleaned_key = None
        self._tokens = {}          # port name -> _port_token when last connected
        # Reuse ONE MidiIn and ONE MidiOut for port enumeration: each new RtMidi
        # client opens /dev/snd/seq, and python-rtmidi leaks that fd on close
        # (hits the ALSA seq-client limit -> 'Cannot allocate memory').
        # MidiOut.get_ports() lists writable ports only, MidiIn.get_ports() lists
        # readable ports only (virtual ports are one-directional), so both are needed.
        self._probe_in = MidiIn()
        self._probe_out = MidiOut()
        # MidiIn/MidiOut objects are pooled per port name: python-rtmidi leaks an
        # ALSA seq client every time one is destroyed, so we never destroy them for
        # a port that still exists. Ports that vanish are dropped (their client is
        # gone with the port); a port that reappears with a new index gets a new open.
        # Inputs use set_callback, not get_message() polling: the queue behind
        # get_message() (RtMidi 5/6) is shared between threads without memory
        # barriers, and on ARM (Raspberry Pi) a poll can copy a half-written
        # message and abort the process with std::bad_alloc.
        self._in_pool = {}
        self._out_pool = {}
        self._aconnect_conns = set()
        atexit.register(self.close)
        if config is not None:
            self.apply_config(config)
        else:
            self.load_config()
        threading.Thread(target=self._rescan_loop, daemon=True).start()
        if config_path:
            threading.Thread(target=self._reload_loop, daemon=True).start()

    # ------------------------------------------------------------- config
    def load_config(self):
        try:
            with open(self.config_path) as f:
                cfg = json.load(f)
            self._mtime = os.path.getmtime(self.config_path)
        except FileNotFoundError:
            cfg = dict(DEFAULT)
        except (OSError, ValueError) as e:
            print(f"midihub: cannot read {self.config_path}: {e}; keeping current config")
            return
        self.apply_config(cfg)

    def save_config(self, cfg):
        """Write cfg to the config file (atomically) and apply it."""
        if self.config_path:
            tmp = self.config_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(cfg, f, indent=2)
            os.replace(tmp, self.config_path)
            self._mtime = os.path.getmtime(self.config_path)
        self.apply_config(cfg)

    def apply_config(self, cfg):
        cfg = self._apply_network(cfg)
        cfg = {**DEFAULT, **cfg}
        p = plan(cfg)
        index = {}
        held = {}
        has_out = {a for a, _ in p["direct"]} | {a for a, _ in p["filtered"]}
        for m in cfg.get("mappings") or []:
            inp, outp = m.get("input") or {}, m.get("output") or {}
            dev, t = inp.get("device"), inp.get("type", "cc")
            if not dev or t not in STATUS:
                continue
            targets = mapping_targets(m, p["routes_from"])
            for s in statuses(t):
                index.setdefault((dev, s), []).append((inp, outp, targets))
            if outp.get("block_original"):
                held.setdefault(dev, []).append((inp, set(targets)))
            has_out.add(dev)
        filtered_from, direct_from = {}, {}
        for a, b in p["filtered"]:
            filtered_from.setdefault(a, []).append(b)
        for a, b in p["direct"]:
            direct_from.setdefault(a, []).append(b)
        exported = self._exported(cfg)
        has_out.update(exported)
        if exported:
            has_out.add(NETWORK_DEV)
        with self.lock:
            self.config = cfg
            self.plan = p
            self._map_index = index
            self._held = held
            self._filtered_from = filtered_from
            self._direct_from = direct_from
            self._exported_list = exported
            self._has_outputs = has_out
            self.guard.configure(cfg)
        self.connect()

    def _apply_network(self, cfg):
        """network.export: expose devices over network via qmidinet."""
        export = (cfg.get("network") or {}).get("export") or []
        if not export:
            return cfg
        devs = dict(cfg.get("devices", {}))
        devs.setdefault(NETWORK_DEV, {"match": "QmidiNet"})
        return {**cfg, "devices": devs}

    @staticmethod
    def _exported(cfg):
        export = (cfg.get("network") or {}).get("export") or []
        return [d for d in export if d in cfg.get("devices", {}) and d != NETWORK_DEV]

    # --------------------------------------------------------------- ports
    def available_ports(self):
        return sorted(set(self._probe_in.get_ports()) | set(self._probe_out.get_ports()))

    def connect(self):
        readable = self._probe_in.get_ports()    # can open MidiIn on these (sources)
        writable = self._probe_out.get_ports()   # can open MidiOut on these (sinks)
        devs = self.config.get("devices", {})
        matched = {}
        for ident, d in devs.items():
            m = d.get("match", "")
            r = [n for n in readable if m and m in n]
            w = [n for n in writable if m and m in n]
            matched[ident] = {"in": r, "out": w}
        tokens = {n: _port_token(n) for ms in matched.values() for lst in ms.values() for n in lst}
        key = tuple(sorted((ident, kind, n, tokens[n]) for ident, ms in matched.items()
                           for kind, lst in ms.items() for n in lst))
        with self.lock:
            if key != self._key:
                self._key = key
                # Ports that vanished, or came back under the same name after a
                # replug: their pooled handles and ALSA connections are dead.
                gone = {n for n, t in self._tokens.items() if tokens.get(n, -1) != t}
                for n in sorted(gone):
                    _log(f"port gone or replugged: {n}")
                for n in sorted(set(tokens) - set(self._tokens)):
                    _log(f"port found: {n}")
                self._tokens = tokens
                for name in list(self._in_pool):
                    if name not in tokens or name in gone:
                        self._stop_pool_in(name)
                for name in list(self._out_pool):
                    if name not in tokens or name in gone:
                        self._stop_pool_out(name)
                dead = {_client_port(n) for n in gone}
                self._aconnect_conns = {c for c in self._aconnect_conns if not dead & set(c)}
                self.port_names = {ident: sorted(set(ms["in"] + ms["out"]))
                                   for ident, ms in matched.items()}
                self._dev_in = {ident: ms["in"] for ident, ms in matched.items()}
                self._dev_out = {ident: ms["out"] for ident, ms in matched.items()}
                port_dev = {}
                for ident, ms in matched.items():
                    for p in set(ms["in"]):
                        port_dev.setdefault(p, []).append(ident)
                self.port_dev = port_dev
                ins, outs = {}, {}
                for ident, ms in matched.items():
                    self.activity.setdefault(ident, 0)
                    ins[ident] = [self._pool_in(n) for n in ms["in"]]
                    outs[ident] = [self._pool_out(n) for n in ms["out"]]
                self.ins = {k: [m for m in v if m] for k, v in ins.items()}
                self.outs = {k: [m for m in v if m] for k, v in outs.items()}
            self.guard.expire()
            self._manage_aconnect()
        self._send_init()

    def _stop_pool_in(self, name):
        """Close a pooled MidiIn for a dead port."""
        m = self._in_pool.pop(name, None)
        if not m:
            return
        try:
            m.cancel_callback()
            m.close_port()
        except Exception:
            pass

    def _stop_pool_out(self, name):
        """Close a pooled MidiOut for a dead port."""
        entry = self._out_pool.pop(name, None)
        if not entry:
            return
        m = entry
        try:
            m.close_port()
        except Exception:
            pass

    @staticmethod
    def _open(m, name):
        """Open port `name` on m. The index comes from m's own port list, taken
        right before opening: an index from the earlier probe can point at
        another port when devices are being plugged in at that moment."""
        ports = m.get_ports()
        if name not in ports:
            return False
        m.open_port(ports.index(name))
        return True

    def _pool_in(self, name):
        """Return an open MidiIn on this port, reusing the pooled one. A
        replugged device gets a fresh open (see connect)."""
        m = self._in_pool.get(name)
        if m:
            return m
        m = MidiIn(API_LINUX_ALSA, "midihub-in")
        m.ignore_types(False, False, False)
        m.set_callback(self._on_input, name)
        if not self._open(m, name):
            return None  # port vanished meanwhile; the next rescan retries
        self._in_pool[name] = m
        return m

    def _on_input(self, msg, name):
        """rtmidi callback, called from rtmidi's own input thread per port.
        An exception must not escape into rtmidi."""
        try:
            self._on_msg(name, msg)
        except Exception as e:
            _log(f"error handling {msg!r} from {name}: {e}")

    def _pool_out(self, name):
        m = self._out_pool.get(name)
        if m:
            return m
        m = MidiOut()
        if not self._open(m, name):
            return None
        self._out_pool[name] = m
        return m

    # ------------------------------------------------------ direct routes
    def _aconnect(self, *args):
        try:
            r = subprocess.run(["aconnect", *args], capture_output=True, text=True, timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return r.returncode == 0 or "already" in (r.stderr or "").lower()

    def _wanted_conns(self):
        conns = set()
        for a, b in self.plan["direct"]:
            if self.guard.is_muted(a):
                continue
            src, dst = self._dev_in.get(a), self._dev_out.get(b)
            if not src or not dst:
                continue
            s, d = _client_port(src[0]), _client_port(dst[0])
            if s != d:
                conns.add((s, d))
        return conns

    def _manage_aconnect(self):
        """Create/remove ALSA sequencer subscriptions for direct routes, so
        passthrough never goes through this process (no Python in the path)."""
        if not shutil.which("aconnect"):
            return False
        wanted = self._wanted_conns()
        # Once per port layout: remove connections between managed devices that
        # midihub doesn't want (left over from a crash or a route removed while
        # midihub was stopped). Otherwise a stale loop could survive a restart.
        if self._cleaned_key != self._key:
            self._cleaned_key = self._key
            srcs = {_client_port(p[0]) for d, p in self._dev_in.items() if p and d != NETWORK_DEV}
            dsts = {_client_port(p[0]) for d, p in self._dev_out.items() if p and d != NETWORK_DEV}
            for s in srcs:
                for d in dsts:
                    if s != d and (s, d) not in wanted and (s, d) not in self._aconnect_conns:
                        self._aconnect("-d", s, d)
        for conn in wanted - self._aconnect_conns:
            if self._aconnect(*conn):
                self._aconnect_conns.add(conn)
                _log(f"connected {conn[0]} -> {conn[1]}")
            else:
                _log(f"could not connect {conn[0]} -> {conn[1]}")
        for conn in self._aconnect_conns - wanted:
            self._aconnect("-d", *conn)
            self._aconnect_conns.discard(conn)
            _log(f"disconnected {conn[0]} -> {conn[1]}")
        return bool(self._aconnect_conns)

    def _rewire(self):
        with self.lock:
            self._manage_aconnect()

    def close(self):
        """Remove midihub's ALSA connections (they would outlive the process)."""
        with self.lock:
            if shutil.which("aconnect"):
                for conn in list(self._aconnect_conns):
                    self._aconnect("-d", *conn)
            self._aconnect_conns.clear()

    # --------------------------------------------------------------- init
    def _send_init(self):
        """Send init messages when a device (re)appears or its init list changes,
        not on every rescan."""
        init = self.config.get("init") or {}
        for ident in self.config.get("devices", {}):
            # With the token, a replugged device gets its init again even
            # when it comes back under the same port name.
            outs = tuple((n, self._tokens.get(n)) for n in self._dev_out.get(ident) or ())
            msgs = init.get(ident) or []
            state = (outs, json.dumps(msgs))
            if not outs:
                self._init_sent.pop(ident, None)
                continue
            if self._init_sent.get(ident) == state:
                continue
            self._init_sent[ident] = state
            for msg in msgs:
                self._send(ident, msg)

    # ------------------------------------------------------------ messages
    def _on_msg(self, port, msg):
        devs = self.port_dev.get(port)
        if not devs:
            return
        if isinstance(msg, (list, tuple)) and len(msg) == 2 and isinstance(msg[1], float):
            msg = msg[0]
        data = list(msg)
        if not data:
            return
        now = time.monotonic()
        rewire = False
        live = []
        with self.lock:
            for dev in devs:
                self.activity[dev] = self.activity.get(dev, 0) + 1
                echo, tripped = self.guard.incoming(dev, data, dev in self._has_outputs, now)
                rewire = rewire or tripped
                if self.guard.is_muted(dev, now):
                    continue
                for t in self._direct_from.get(dev, ()):
                    self.guard.forwarded(t, data, now)
                if self._learn is not None and not echo and data[0] < 0xF8:
                    self._record_learn(dev, data, now)
                live.append((dev, echo))
        if rewire:
            threading.Thread(target=self._rewire, daemon=True).start()
        for dev, echo in live:
            self._route(dev, data)
            if not echo:
                self._translate(dev, data)

    def _route(self, dev, data):
        # Direct routes are ALSA subscriptions; only filtered routes and network
        # export pass through here.
        for t in self._filtered_from.get(dev, ()):
            held = self._held.get(dev)
            if held and any(t in targets and match(data, inp) for inp, targets in held):
                continue
            self._send(t, data)
        if data[0] >= 0xF0:
            return
        exported = self._exported_list
        if dev == NETWORK_DEV:
            for e in exported:
                self._send(e, data)
        elif dev in exported:
            self._send(NETWORK_DEV, data)

    def _translate(self, dev, data):
        s = data[0] if data[0] >= 0xF0 else data[0] & 0xF0
        entries = self._map_index.get((dev, s))
        if not entries:
            return
        for inp, outp, targets in entries:
            out = translate(data, inp, outp)
            if out is None:
                continue
            for t in targets:
                self._send(t, out)
            if dev in self._exported_list:
                self._send(NETWORK_DEV, out)

    def _send(self, dev, msg):
        outs = self.outs.get(dev)
        if not outs:
            return
        with self.lock:
            self.guard.forwarded(dev, msg)
        for m in outs:
            try:
                m.send_message(msg)
            except Exception:
                pass

    def send_to_device(self, dev, msg):
        self._send(dev, msg)

    # -------------------------------------------------------------- learn
    def learn(self, on, seconds=180):
        """Start/stop recording incoming messages for press-to-learn and the monitor."""
        with self.lock:
            if on:
                if self._learn is None:
                    self._learn = {"events": deque(maxlen=300)}
                self._learn["until"] = time.monotonic() + seconds
            else:
                self._learn = None

    def _record_learn(self, dev, data, now):
        if now > self._learn["until"]:
            self._learn = None
            return
        self._learn_seq += 1
        self._learn["events"].append({"seq": self._learn_seq, "device": dev, "bytes": data,
                                      "t": time.time()})

    def learn_events(self, after=0):
        with self.lock:
            if self._learn is None:
                return {"active": False, "seq": self._learn_seq, "events": []}
            return {"active": True, "seq": self._learn_seq,
                    "events": [e for e in self._learn["events"] if e["seq"] > after]}

    def reset_guard(self):
        with self.lock:
            self.guard.reset()
            self._manage_aconnect()

    # ------------------------------------------------------------- status
    def status(self):
        with self.lock:
            p = self.plan
            return {
                "config": self.config,
                "ports": {ident: list(names) for ident, names in self.port_names.items()},
                "activity": dict(self.activity),
                "available": self.available_ports(),
                "plan": {"direct": p["direct"], "filtered": p["filtered"],
                         "blocked": p["blocked"], "warnings": p["warnings"]},
                "guard": self.guard.snapshot(),
                "learning": self._learn is not None,
            }

    # -------------------------------------------------------------- loops
    def _rescan_loop(self):
        while True:
            time.sleep(2)
            try:
                self.connect()
            except Exception as e:
                print(f"midihub: rescan failed: {e}")

    def _reload_loop(self):
        while True:
            time.sleep(1)
            try:
                mtime = os.path.getmtime(self.config_path)
            except OSError:
                mtime = None
            if mtime != self._mtime:
                self._mtime = mtime
                if mtime is not None:
                    self.load_config()
