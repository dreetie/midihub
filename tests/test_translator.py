import unittest

from translator import detect_value_index, match, translate

TPL = [0xF0, 0x7D, 0x01, 0x10, 0x00, 0xF7]
SYSEX_IN = {"type": "sysex", "template": TPL, "value_index": 4}


class Translate(unittest.TestCase):
    def test_cc_to_sysex(self):
        out = translate([0xB0, 20, 42], {"type": "cc", "cc": 20}, {"type": "sysex", "template": TPL, "value_index": 4})
        self.assertEqual(out, [0xF0, 0x7D, 0x01, 0x10, 42, 0xF7])

    def test_sysex_to_cc(self):
        self.assertEqual(translate([0xF0, 0x7D, 0x01, 0x10, 99, 0xF7], SYSEX_IN, {"type": "cc", "cc": 7, "channel": 2}),
                         [0xB2, 7, 99])

    def test_sysex_template_must_match_other_bytes(self):
        self.assertIsNone(translate([0xF0, 0x7D, 0x02, 0x10, 99, 0xF7], SYSEX_IN, {"type": "cc", "cc": 7}))
        self.assertIsNone(translate([0xF0, 0x7D, 0x01, 0x10, 99, 0x00, 0xF7], SYSEX_IN, {"type": "cc", "cc": 7}))

    def test_sysex_to_sysex(self):
        out = translate([0xF0, 0x7D, 0x01, 0x10, 5, 0xF7], SYSEX_IN,
                        {"type": "sysex", "template": [0xF0, 0x7D, 0x09, 0x00, 0xF7], "value_index": 3})
        self.assertEqual(out, [0xF0, 0x7D, 0x09, 5, 0xF7])

    def test_cc_to_cc_keeps_channel_when_unset(self):
        self.assertEqual(translate([0xB3, 1, 10], {"type": "cc", "cc": 1}, {"type": "cc", "cc": 74}), [0xB3, 74, 10])

    def test_cc_to_cc_channel_and_scale(self):
        out = translate([0xB0, 1, 127], {"type": "cc", "cc": 1}, {"type": "cc", "cc": 7, "channel": 9, "scale": [0, 127, 0, 100]})
        self.assertEqual(out, [0xB9, 7, 100])

    def test_inverted_scale(self):
        out = translate([0xB0, 1, 0], {"type": "cc", "cc": 1}, {"type": "cc", "cc": 1, "scale": [0, 127, 127, 0]})
        self.assertEqual(out, [0xB0, 1, 127])

    def test_note_to_note_on_and_off(self):
        inp, outp = {"type": "note", "note": 60}, {"type": "note", "note": 36}
        self.assertEqual(translate([0x90, 60, 100], inp, outp), [0x90, 36, 100])
        self.assertEqual(translate([0x80, 60, 64], inp, outp), [0x80, 36, 0])
        self.assertEqual(translate([0x90, 60, 0], inp, outp), [0x80, 36, 0])

    def test_note_to_cc(self):
        self.assertEqual(translate([0x90, 60, 100], {"type": "note", "note": 60}, {"type": "cc", "cc": 64}), [0xB0, 64, 100])
        self.assertEqual(translate([0x80, 60, 0], {"type": "note", "note": 60}, {"type": "cc", "cc": 64}), [0xB0, 64, 0])

    def test_legacy_noteon_matches_only_note_on(self):
        self.assertTrue(match([0x90, 60, 1], {"type": "noteon", "note": 60}))
        self.assertFalse(match([0x80, 60, 1], {"type": "noteon", "note": 60}))

    def test_pc(self):
        self.assertEqual(translate([0xC0, 5], {"type": "pc"}, {"type": "pc", "channel": 1}), [0xC1, 5])
        self.assertIsNone(translate([0xC0, 5], {"type": "pc", "pc": 6}, {"type": "pc"}))
        self.assertEqual(translate([0xC0, 6], {"type": "pc", "pc": 6}, {"type": "pc", "pc": 9}), [0xC0, 9])

    def test_pitchbend_resolution(self):
        self.assertEqual(translate([0xE0, 0x7F, 0x7F], {"type": "pitchbend"}, {"type": "cc", "cc": 1}), [0xB0, 1, 127])
        self.assertEqual(translate([0xB0, 1, 127], {"type": "cc", "cc": 1}, {"type": "pitchbend"}), [0xE0, 0x7F, 0x7F])
        self.assertEqual(translate([0xE0, 0x00, 0x40], {"type": "pitchbend"}, {"type": "pitchbend"}), [0xE0, 0x00, 0x40])

    def test_channel_filter(self):
        self.assertIsNone(translate([0xB1, 20, 1], {"type": "cc", "cc": 20, "channel": 0}, {"type": "cc", "cc": 1}))

    def test_sysex_trigger_without_value_index(self):
        out = translate([0xB0, 20, 42], {"type": "cc", "cc": 20}, {"type": "sysex", "template": [0xF0, 1, 2, 0xF7]})
        self.assertEqual(out, [0xF0, 1, 2, 0xF7])

    def test_detect_value_index(self):
        self.assertEqual(detect_value_index([[1, 2, 3], [1, 5, 3]]), 1)
        self.assertIsNone(detect_value_index([[1, 2, 3], [1, 5, 7]]))


if __name__ == "__main__":
    unittest.main()
