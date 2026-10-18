#!/usr/bin/env python3
"""midihub entry point: runs the MIDI hub and (optionally) the web UI.

    python3 web.py                 run hub + web UI
    python3 web.py --no-web        run the hub only (config file is used as-is)
    python3 web.py --set-password  set or change the web UI password
    python3 web.py --clear-password remove the password (UI goes back to localhost only)

Web access rules (config "web" section):
    enabled   false = no web server at all (default true)
    host      address to listen on. Default: 0.0.0.0 when a password is set,
              127.0.0.1 (this machine only) when it isn't. Listening on the
              network without a password is refused unless
              "allow_no_password": true.
    port      default 8080
"""
import getpass
import hashlib
import json
import os
import secrets
import signal
import sys
import threading
import time
from datetime import timedelta

from flask import Flask, jsonify, redirect, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

from translator import STATUS, render, scale_value

HERE = os.path.dirname(os.path.abspath(__file__))
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _valid_config(cfg):
    if not isinstance(cfg, dict):
        return "config must be a JSON object"
    if "devices" not in cfg or not isinstance(cfg["devices"], dict):
        return "config.devices must be an object"
    for ident, d in cfg["devices"].items():
        if not isinstance(d, dict) or not d.get("match"):
            return f"device '{ident}' needs a 'match' string"
    for lst in ("routes", "mappings"):
        if lst in cfg and not isinstance(cfg[lst], list):
            return f"config.{lst} must be a list"
    for i, m in enumerate(cfg.get("mappings") or []):
        if not isinstance(m, dict):
            return f"mapping {i + 1} must be an object"
        for side in ("input", "output"):
            t = (m.get(side) or {}).get("type")
            if t is not None and t not in STATUS:
                return f"mapping {i + 1}: unknown {side} type '{t}'"
    return None


def web_settings(cfg):
    web = dict(cfg.get("web") or {})
    has_pw = bool(web.get("password_hash"))
    host = web.get("host") or ("0.0.0.0" if has_pw else "127.0.0.1")
    note = None
    if host not in LOOPBACK and not has_pw and not web.get("allow_no_password"):
        note = (f"web.host is {host} but no password is set; listening on 127.0.0.1 only. "
                f"Run 'python3 web.py --set-password' to open the web UI to your network.")
        host = "127.0.0.1"
    return {"enabled": web.get("enabled", True) is not False, "host": host,
            "port": int(web.get("port", 8080)), "password": has_pw, "note": note}


def _public_config(cfg):
    """Config as shown to the browser: no password hash."""
    out = dict(cfg)
    web = dict(out.get("web") or {})
    web.pop("password_hash", None)
    out["web"] = web
    out["devices"] = {k: v for k, v in (out.get("devices") or {}).items() if k != "qmidinet"}
    return out


def _secret_key(hub):
    """Session signing key, kept next to the config file (never in the repo)."""
    base = os.path.dirname(hub.config_path) if hub.config_path else HERE
    path = os.path.join(base, ".midihub-secret")
    try:
        with open(path) as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key)
    except OSError:
        pass  # read-only install: sessions just won't survive a restart
    return key


def create_app(hub):
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.secret_key = _secret_key(hub)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                      PERMANENT_SESSION_LIFETIME=timedelta(days=30))
    failures = {}  # ip -> (count, first_failure_time)

    def pw_hash():
        return (hub.config.get("web") or {}).get("password_hash")

    def fingerprint(h):
        # The session remembers which password it was made with, so changing
        # the password logs everybody out.
        return hashlib.sha256(h.encode()).hexdigest()[:16]

    def authed():
        h = pw_hash()
        return not h or session.get("pw") == fingerprint(h)

    @app.before_request
    def guard():
        path = request.path
        if path.startswith("/static/") or path in ("/login", "/api/login"):
            return None
        if request.method == "POST" and request.headers.get("X-Midihub") != "1":
            # Custom header: a page on another site can't send it without a
            # CORS preflight we never answer, so no cross-site form posts.
            return jsonify(error="missing X-Midihub header"), 403
        if not authed():
            if path.startswith("/api/"):
                return jsonify(error="login required"), 401
            return redirect("/login")
        return None

    @app.get("/")
    def index():
        return send_from_directory(os.path.join(HERE, "static"), "index.html")

    @app.get("/login")
    def login_page():
        if authed():
            return redirect("/")
        return send_from_directory(os.path.join(HERE, "static"), "login.html")

    @app.post("/api/login")
    def login():
        if request.headers.get("X-Midihub") != "1":
            return jsonify(error="missing X-Midihub header"), 403
        ip = request.remote_addr or "?"
        n, first = failures.get(ip, (0, 0))
        if n >= 5 and time.time() - first < 60:
            return jsonify(error="Too many attempts. Wait a minute and try again."), 429
        body = request.get_json(silent=True) or {}
        h = pw_hash()
        if not h:
            return jsonify(ok=True)
        if check_password_hash(h, str(body.get("password", ""))):
            failures.pop(ip, None)
            session.permanent = True
            session["pw"] = fingerprint(h)
            return jsonify(ok=True)
        if time.time() - first >= 60:
            n, first = 0, time.time()
        failures[ip] = (n + 1, first)
        time.sleep(1)
        return jsonify(error="Wrong password"), 401

    @app.post("/api/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/state")
    def state():
        s = hub.status()
        s["config"] = _public_config(s["config"])
        w = web_settings(hub.config)
        s["web"] = {"password": w["password"], "host": w["host"], "port": w["port"]}
        return jsonify(s)

    @app.post("/api/save")
    def save():
        cfg = request.get_json(silent=True)
        if not isinstance(cfg, dict):
            return jsonify(error="invalid JSON body"), 400
        (cfg.get("devices") or {}).pop("qmidinet", None)  # internal, managed by network export
        # Web/security settings are only changed in the config file or with
        # --set-password, never from the browser.
        cfg["web"] = hub.config.get("web") or {}
        err = _valid_config(cfg)
        if err:
            return jsonify(error=err), 400
        hub.save_config(cfg)
        return jsonify(ok=True)

    @app.post("/api/learn")
    def learn():
        body = request.get_json(silent=True) or {}
        hub.learn(bool(body.get("on")))
        return jsonify(ok=True)

    @app.get("/api/learn")
    def learn_get():
        return jsonify(hub.learn_events(int(request.args.get("after", 0) or 0)))

    @app.post("/api/test")
    def test_send():
        """Send raw bytes, or a mapping output rendered at a given value."""
        body = request.get_json(silent=True) or {}
        dev = body.get("device")
        if dev not in hub.config.get("devices", {}):
            return jsonify(error="unknown device"), 400
        msg = body.get("bytes")
        if isinstance(body.get("output"), dict):
            out = body["output"]
            value = int(body.get("value", 127))
            in_bits = 14 if body.get("input_type") == "pitchbend" else 7
            out_bits = 14 if out.get("type") == "pitchbend" else 7
            msg = render(out, scale_value(value, out.get("scale"), in_bits, out_bits))
        if not isinstance(msg, list) or not msg:
            return jsonify(error="nothing to send"), 400
        hub.send_to_device(dev, [int(b) & 0xFF for b in msg])
        return jsonify(ok=True, bytes=msg)

    @app.post("/api/guard/reset")
    def guard_reset():
        hub.reset_guard()
        return jsonify(ok=True)

    return app


# ------------------------------------------------------------------ CLI
def _edit_config(path, fn):
    try:
        with open(path) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {"devices": {}, "routes": [], "mappings": [], "init": {}, "network": {}}
    fn(cfg)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, path)


def set_password(path):
    pw = getpass.getpass("New web UI password (empty = cancel): ")
    if not pw:
        print("No password set.")
        return 1
    if getpass.getpass("Repeat password: ") != pw:
        print("Passwords don't match.")
        return 1
    if len(pw) < 6:
        print("Use at least 6 characters.")
        return 1
    h = generate_password_hash(pw)
    _edit_config(path, lambda c: c.setdefault("web", {}).__setitem__("password_hash", h))
    print(f"Password saved in {path}. Restart midihub so the web UI listens on your network.")
    return 0


def clear_password(path):
    _edit_config(path, lambda c: (c.get("web") or {}).pop("password_hash", None))
    print("Password removed. After a restart the web UI only listens on this machine (127.0.0.1).")
    return 0


def main(argv):
    from midihub import CONFIG_PATH, Hub

    if "--set-password" in argv:
        return set_password(CONFIG_PATH)
    if "--clear-password" in argv:
        return clear_password(CONFIG_PATH)

    hub = Hub()

    def stop(*_):
        hub.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    web = web_settings(hub.config)
    if "--no-web" in argv or os.environ.get("MIDIHUB_NO_WEB") == "1":
        web["enabled"] = False
    if not web["enabled"]:
        print("hub running; web UI disabled (edit the config file to change routing)")
    else:
        if web["note"]:
            print(web["note"], file=sys.stderr)
        app = create_app(hub)

        def run_web():
            # The Hub owns the MIDI ports in this process; the web server is a
            # disposable thread. If it crashes, restart it and keep routing.
            while True:
                try:
                    app.run(host=web["host"], port=web["port"], threaded=True, use_reloader=False)
                except Exception as e:
                    print(f"web server crashed: {e}", file=sys.stderr)
                time.sleep(1)

        threading.Thread(target=run_web, daemon=True).start()
        lock = "password protected" if web["password"] else "no password"
        print(f"hub running; web UI on http://{web['host']}:{web['port']} ({lock})")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
