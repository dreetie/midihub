"""In-memory stand-in for python-rtmidi so tests run without ALSA hardware.

Tests set PORTS to the port names the "system" has, push messages into
INBOX[port] and read what midihub sent from SENT[port].
"""
import types

PORTS = []
INBOX = {}
SENT = {}


class _Base:
    def __init__(self, *args, **kwargs):
        self.port = None

    def get_ports(self):
        return list(PORTS)

    def open_port(self, idx):
        self.port = PORTS[idx]

    def close_port(self):
        self.port = None


class MidiIn(_Base):
    def ignore_types(self, *a):
        pass

    def get_message(self):
        q = INBOX.get(self.port)
        if q:
            return (q.pop(0), 0.0)
        return None


class MidiOut(_Base):
    def send_message(self, msg):
        SENT.setdefault(self.port, []).append(list(msg))


def install(sys_modules):
    mod = types.ModuleType("rtmidi")
    mod.MidiIn, mod.MidiOut, mod.API_LINUX_ALSA = MidiIn, MidiOut, 0
    sys_modules["rtmidi"] = mod


def reset(ports):
    PORTS[:] = ports
    INBOX.clear()
    SENT.clear()
