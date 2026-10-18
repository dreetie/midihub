import sys
import tempfile
import unittest
from unittest import mock

from werkzeug.security import generate_password_hash

from tests import fake_rtmidi

fake_rtmidi.install(sys.modules)
import midihub  # noqa: E402
import web  # noqa: E402

H = {"X-Midihub": "1"}


class Web(unittest.TestCase):
    def setUp(self):
        fake_rtmidi.reset(["Synth:Synth MIDI 1 24:0"])
        for p in (mock.patch("midihub.shutil.which", return_value=None), mock.patch("midihub.threading.Thread")):
            p.start()
            self.addCleanup(p.stop)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def client(self, web_cfg=None):
        path = self.dir.name + "/config.json"
        import json
        with open(path, "w") as f:
            json.dump({"web": web_cfg or {}, "devices": {"synth": {"match": "Synth"}}}, f)
        self.hub = midihub.Hub(config_path=path)
        app = web.create_app(self.hub)
        app.testing = True
        return app.test_client()

    def test_open_without_password(self):
        c = self.client()
        r = c.get("/api/state")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json["web"]["password"])

    def test_password_required(self):
        c = self.client({"password_hash": generate_password_hash("secret1")})
        self.assertEqual(c.get("/api/state").status_code, 401)
        self.assertEqual(c.get("/").status_code, 302)
        self.assertEqual(c.post("/api/login", json={"password": "nope"}, headers=H).status_code, 401)
        self.assertEqual(c.post("/api/login", json={"password": "secret1"}, headers=H).status_code, 200)
        r = c.get("/api/state")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("password_hash", r.json["config"]["web"])

    def test_post_needs_header(self):
        c = self.client()
        self.assertEqual(c.post("/api/learn", json={"on": True}).status_code, 403)
        self.assertEqual(c.post("/api/learn", json={"on": True}, headers=H).status_code, 200)

    def test_save_cannot_change_web_settings(self):
        h = generate_password_hash("secret1")
        c = self.client({"password_hash": h})
        c.post("/api/login", json={"password": "secret1"}, headers=H)
        r = c.post("/api/save", headers=H, json={"web": {}, "devices": {"synth": {"match": "Synth"}}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.hub.config["web"]["password_hash"], h)

    def test_test_send_renders_output(self):
        c = self.client()
        r = c.post("/api/test", headers=H, json={"device": "synth", "output": {"type": "cc", "cc": 7}, "value": 99})
        self.assertEqual(r.json["bytes"], [0xB0, 7, 99])
        self.assertEqual(fake_rtmidi.SENT["Synth:Synth MIDI 1 24:0"], [[0xB0, 7, 99]])

    def test_web_settings(self):
        self.assertEqual(web.web_settings({})["host"], "127.0.0.1")
        s = web.web_settings({"web": {"host": "0.0.0.0"}})
        self.assertEqual(s["host"], "127.0.0.1")
        self.assertIsNotNone(s["note"])
        self.assertEqual(web.web_settings({"web": {"password_hash": "x"}})["host"], "0.0.0.0")
        self.assertFalse(web.web_settings({"web": {"enabled": False}})["enabled"])


if __name__ == "__main__":
    unittest.main()
