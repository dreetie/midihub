import unittest

from guard import LoopGuard, plan

DEVS = {"a": {"match": "A"}, "b": {"match": "B"}, "c": {"match": "C"}}


class Plan(unittest.TestCase):
    def test_two_way_route_is_blocked(self):
        p = plan({"devices": DEVS, "routes": [{"from": "a", "to": ["b"]}, {"from": "b", "to": ["a"]}]})
        self.assertEqual(p["direct"], [("a", "b")])
        self.assertEqual(p["blocked"][0]["cycle"], ["b", "a", "b"])
        self.assertEqual(p["warnings"][0]["level"], "err")

    def test_long_cycle_is_blocked(self):
        routes = [{"from": "a", "to": ["b"]}, {"from": "b", "to": ["c"]}, {"from": "c", "to": ["a"]}]
        p = plan({"devices": DEVS, "routes": routes})
        self.assertEqual(sorted(p["direct"]), [("a", "b"), ("b", "c")])
        self.assertEqual(p["blocked"][0]["cycle"], ["c", "a", "b", "c"])

    def test_cycles_allowed_when_disabled(self):
        p = plan({"devices": DEVS, "loop_guard": {"block_cycles": False},
                  "routes": [{"from": "a", "to": ["b"]}, {"from": "b", "to": ["a"]}]})
        self.assertEqual(len(p["direct"]), 2)
        self.assertEqual(p["warnings"][0]["level"], "warn")

    def test_replace_original_makes_route_filtered(self):
        p = plan({"devices": DEVS, "routes": [{"from": "a", "to": ["b", "c"]}],
                  "mappings": [{"input": {"device": "a", "type": "cc", "cc": 1},
                                "output": {"device": "b", "type": "cc", "cc": 2, "block_original": True}}]})
        self.assertEqual(p["direct"], [("a", "c")])
        self.assertEqual(p["filtered"], [("a", "b")])

    def test_mapping_loop_is_a_warning(self):
        p = plan({"devices": DEVS, "routes": [{"from": "b", "to": ["a"]}],
                  "mappings": [{"input": {"device": "a", "type": "cc"}, "output": {"device": "b", "type": "cc"}}]})
        self.assertEqual(p["direct"], [("b", "a")])
        self.assertEqual(p["warnings"][0]["kind"], "mapping-loop")


class Live(unittest.TestCase):
    def setUp(self):
        self.t = 100.0
        self.g = LoopGuard({}, clock=lambda: self.t)

    def test_echo_burst_mutes_source(self):
        tripped = False
        for i in range(30):
            self.g.forwarded("b", [0x90, 60, i + 1])
            echo, tr = self.g.incoming("b", [0x90, 60, i + 1])
            self.assertTrue(echo)
            tripped = tripped or tr
            self.t += 0.001
        self.assertTrue(tripped)
        self.assertTrue(self.g.is_muted("b"))
        self.t += 6
        self.assertFalse(self.g.is_muted("b"))

    def test_late_reply_is_not_an_echo(self):
        self.g.forwarded("b", [0xB0, 1, 1])
        self.t += 0.5
        self.assertEqual(self.g.incoming("b", [0xB0, 1, 1]), (False, False))

    def test_no_trip_without_outputs(self):
        for i in range(40):
            self.g.forwarded("b", [0x90, 60, i + 1])
            self.g.incoming("b", [0x90, 60, i + 1], has_outputs=False)
        self.assertFalse(self.g.is_muted("b"))

    def test_clock_is_never_an_echo(self):
        self.g.forwarded("b", [0xF8])
        self.assertEqual(self.g.incoming("b", [0xF8]), (False, False))

    def test_repeated_trips_latch(self):
        for _ in range(3):
            for i in range(25):
                self.g.forwarded("b", [0x90, 1, i + 1])
                self.g.incoming("b", [0x90, 1, i + 1])
            self.t += 6
        self.assertTrue(self.g.is_muted("b"))
        self.t += 1000
        self.assertTrue(self.g.is_muted("b"))
        self.g.reset()
        self.assertFalse(self.g.is_muted("b"))

    def test_flood_warns(self):
        for _ in range(2001):
            self.g.incoming("a", [0xB0, 1, 1])
        self.assertEqual(self.g.alerts[-1]["kind"], "flood")
        self.assertFalse(self.g.is_muted("a"))


if __name__ == "__main__":
    unittest.main()
