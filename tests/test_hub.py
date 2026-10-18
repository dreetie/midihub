import sys
import unittest
from unittest import mock

from tests import fake_rtmidi

fake_rtmidi.install(sys.modules)
import midihub  # noqa: E402

KB_IN, KB_OUT = "Keys:Keys MIDI 1 20:0", "Keys:Keys MIDI 1 20:0"
SY = "Synth:Synth MIDI 1 24:0"


class Hub(unittest.TestCase):
    def setUp(self):
        fake_rtmidi.reset([KB_IN, SY])
        self.calls = []

        def run(args, **kw):
            self.calls.append(args[1:])
            return mock.Mock(returncode=0, stderr="")

        patches = [mock.patch("midihub.subprocess.run", side_effect=run),
                   mock.patch("midihub.shutil.which", return_value="/usr/bin/aconnect"),
                   mock.patch("midihub.threading.Thread")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def hub(self, **cfg):
        base = {"devices": {"keys": {"match": "Keys"}, "synth": {"match": "Synth"}}}
        base.update(cfg)
        return midihub.Hub(config=base)

    def test_route_is_direct_aconnect(self):
        self.hub(routes=[{"from": "keys", "to": ["synth"]}])
        self.assertIn(["20:0", "24:0"], self.calls)

    def test_loop_route_not_connected(self):
        self.hub(routes=[{"from": "keys", "to": ["synth"]}, {"from": "synth", "to": ["keys"]}])
        self.assertIn(["20:0", "24:0"], self.calls)
        self.assertNotIn(["24:0", "20:0"], self.calls)

    def test_cc_to_sysex_mapping(self):
        h = self.hub(mappings=[{"input": {"device": "keys", "type": "cc", "cc": 20},
                                "output": {"device": "synth", "type": "sysex",
                                           "template": [0xF0, 0x7D, 0, 0xF7], "value_index": 2}}])
        h._on_msg(KB_IN, ([0xB0, 20, 64], 0.0))
        self.assertEqual(fake_rtmidi.SENT[SY], [[0xF0, 0x7D, 64, 0xF7]])

    def test_sysex_to_cc_mapping(self):
        h = self.hub(mappings=[{"input": {"device": "synth", "type": "sysex",
                                          "template": [0xF0, 0x7D, 0, 0xF7], "value_index": 2},
                                "output": {"device": "keys", "type": "cc", "cc": 7}}])
        h._on_msg(SY, ([0xF0, 0x7D, 33, 0xF7], 0.0))
        self.assertEqual(fake_rtmidi.SENT[KB_OUT], [[0xB0, 7, 33]])

    def test_replace_original_holds_back_only_mapped_message(self):
        h = self.hub(routes=[{"from": "keys", "to": ["synth"]}],
                     mappings=[{"input": {"device": "keys", "type": "cc", "cc": 1},
                                "output": {"type": "cc", "cc": 74, "block_original": True}}])
        self.assertNotIn(["20:0", "24:0"], self.calls)  # route runs through midihub instead
        h._on_msg(KB_IN, ([0xB0, 1, 50], 0.0))
        h._on_msg(KB_IN, ([0x90, 60, 100], 0.0))
        self.assertEqual(fake_rtmidi.SENT[SY], [[0xB0, 74, 50], [0x90, 60, 100]])

    def test_init_sent_once_not_every_rescan(self):
        h = self.hub(init={"synth": [[0xF0, 0x7D, 1, 0xF7]]})
        h.connect()
        h.connect()
        self.assertEqual(fake_rtmidi.SENT[SY], [[0xF0, 0x7D, 1, 0xF7]])

    def test_echoed_message_is_not_translated(self):
        h = self.hub(mappings=[{"input": {"device": "synth", "type": "cc", "cc": 7},
                                "output": {"device": "keys", "type": "cc", "cc": 7}}])
        h.send_to_device("synth", [0xB0, 7, 5])
        h._on_msg(SY, ([0xB0, 7, 5], 0.0))
        self.assertNotIn(KB_OUT, fake_rtmidi.SENT)

    def test_learn_records_messages(self):
        h = self.hub()
        h.learn(True)
        h._on_msg(KB_IN, ([0xB0, 1, 2], 0.0))
        h._on_msg(KB_IN, ([0xF8], 0.0))  # clock is ignored
        ev = h.learn_events(0)
        self.assertEqual([(e["device"], e["bytes"]) for e in ev["events"]], [("keys", [0xB0, 1, 2])])

    def test_bouncing_messages_mute_the_route(self):
        h = self.hub(routes=[{"from": "keys", "to": ["synth"]}])
        for i in range(30):
            h.send_to_device("keys", [0x90, 60, i + 1])
            h._on_msg(KB_IN, ([0x90, 60, i + 1], 0.0))
        self.assertTrue(h.guard.is_muted("keys"))
        h._rewire()
        self.assertIn(["-d", "20:0", "24:0"], self.calls)
        self.assertEqual(h.status()["guard"]["alerts"][-1]["kind"], "loop")
        h.reset_guard()
        self.assertEqual(self.calls[-1], ["20:0", "24:0"])

    def test_close_removes_connections(self):
        h = self.hub(routes=[{"from": "keys", "to": ["synth"]}])
        h.close()
        self.assertIn(["-d", "20:0", "24:0"], self.calls)


if __name__ == "__main__":
    unittest.main()
