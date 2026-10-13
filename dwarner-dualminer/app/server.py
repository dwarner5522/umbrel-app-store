#!/usr/bin/env python3
"""Dualminer Control - web panel that drives every dualminer on the LAN.

Stdlib only. Talks to each board's dualminer remote API (GET /status,
POST /mode) and serves a single-page UI plus a small JSON API for it.
"""
import http.server
import ipaddress
import json
import os
import re
import secrets
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(HERE, "data"))
STATE_FILE = os.path.join(DATA_DIR, "panel.json")
PORT = int(os.environ.get("PORT", 8080))
DEFAULT_MINER_PORT = 8377
MODES = ("both", "monero", "verus", "off")
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?$")
MAX_SCAN = 1024

lock = threading.Lock()
state = {"token": "", "miners": []}  # miner: {"id", "host", "port"}
pool = ThreadPoolExecutor(max_workers=64)


def load_state():
    try:
        with open(STATE_FILE) as f:
            state.update(json.load(f))
    except (OSError, ValueError):
        pass


def save_state():
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


class BadRequest(Exception):
    pass


def call_miner(host, port, path, body=None, token="", timeout=4):
    """Returns (data, None) on success or (None, short error text)."""
    req = urllib.request.Request(f"http://{host}:{port}{path}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r), None
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return None, "wrong token"
        try:
            detail = json.load(e)
            return None, detail.get("message") or detail.get("error") or f"HTTP {e.code}"
        except ValueError:
            return None, f"HTTP {e.code}"
    except (OSError, ValueError):
        return None, "offline"


def miner_view(miner, token):
    view = dict(miner, online=False, error=None)
    if not token:
        view["error"] = "no token set"
        return view
    data, err = call_miner(miner["host"], miner["port"], "/status", token=token)
    if err:
        view["error"] = err
    else:
        view.update(online=True, name=data.get("name"), mode=data.get("mode"),
                    coins=data.get("coins", {}), temp=data.get("temp"))
    return view


def snapshot():
    with lock:
        return state["token"], [dict(m) for m in state["miners"]]


def api_state():
    token, miners = snapshot()
    return {"token_set": bool(token),
            "miners": list(pool.map(lambda m: miner_view(m, token), miners))}


def split_rate(hs):
    """1850.4 -> ("1850", "H/s"), for the umbrelOS home screen widget."""
    for unit, scale in (("MH/s", 1e6), ("kH/s", 1e3)):
        if hs >= scale * 10 or (unit == "MH/s" and hs >= scale):
            return f"{hs / scale:.1f}", unit
    return f"{hs:.0f}", "H/s"


def api_widget():
    miners = [m for m in api_state()["miners"]]
    online = [m for m in miners if m["online"]]

    def total(coin):
        return sum((m["coins"].get(coin) or {}).get("hashrate") or 0 for m in online)

    items = [{"title": "Miners", "text": f"{len(online)}/{len(miners)}", "subtext": "online"}]
    for title, coin in (("Monero", "monero"), ("Verus", "verus")):
        text, unit = split_rate(total(coin))
        items.append({"title": title, "text": text, "subtext": unit})
    return {"type": "three-stats", "refresh": "10s", "link": "", "items": items}


def parse_port(value):
    try:
        port = int(value or DEFAULT_MINER_PORT)
    except (TypeError, ValueError):
        raise BadRequest("port must be a number")
    if not 1 <= port <= 65535:
        raise BadRequest("port must be between 1 and 65535")
    return port


def add_miner(host, port):
    """Returns True if added, False if it was already in the list."""
    with lock:
        if any(m["host"] == host and m["port"] == port for m in state["miners"]):
            return False
        state["miners"].append({"id": secrets.token_hex(4), "host": host, "port": port})
        save_state()
        return True


def api_add(body):
    host = str(body.get("host", "")).strip()
    if not HOST_RE.match(host):
        raise BadRequest("enter an IP address or hostname")
    if not add_miner(host, parse_port(body.get("port"))):
        raise BadRequest(f"{host} is already in the list")
    return {"ok": True}


def api_remove(miner_id):
    with lock:
        state["miners"] = [m for m in state["miners"] if m["id"] != miner_id]
        save_state()
    return {"ok": True}


def api_token(body):
    with lock:
        state["token"] = str(body.get("token", "")).strip()
        save_state()
    return {"ok": True}


def api_mode(body):
    mode = body.get("mode")
    if mode not in MODES:
        raise BadRequest(f"mode must be one of {', '.join(MODES)}")
    token, miners = snapshot()
    ids = body.get("ids")
    if ids is not None:
        miners = [m for m in miners if m["id"] in ids]

    def switch(m):
        data, err = call_miner(m["host"], m["port"], "/mode", {"mode": mode}, token, timeout=15)
        return {"id": m["id"], "host": m["host"], "ok": err is None, "message": err}

    return {"results": list(pool.map(switch, miners))}


def api_scan(body):
    try:
        net = ipaddress.ip_network(str(body.get("cidr", "")).strip(), strict=False)
    except ValueError:
        raise BadRequest("enter a network like 192.168.1.0/24")
    if net.num_addresses > MAX_SCAN:
        raise BadRequest(f"network too large (max {MAX_SCAN} addresses, e.g. a /24)")
    port = parse_port(body.get("port"))

    def probe(ip):
        data, _ = call_miner(str(ip), port, "/ping", timeout=1.5)
        return str(ip) if data and data.get("dualminer") else None

    found = [ip for ip in pool.map(probe, net.hosts()) if ip]
    added = [ip for ip in found if add_miner(ip, port)]
    return {"found": found, "added": added}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_body(self, code, data, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, code, body):
        self.send_body(code, json.dumps(body).encode(), "application/json")

    def read_json(self):
        # JSON-only keeps other websites from driving the panel with a plain form post
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            raise BadRequest("Content-Type must be application/json")
        try:
            length = min(int(self.headers.get("Content-Length", 0)), 65536)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise BadRequest("invalid JSON")
        if not isinstance(body, dict):
            raise BadRequest("expected a JSON object")
        return body

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self.send_body(200, f.read(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            self.send_json(200, api_state())
        elif self.path == "/api/widget/stats":
            self.send_json(200, api_widget())
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        routes = {"/api/miners": api_add, "/api/token": api_token,
                  "/api/mode": api_mode, "/api/scan": api_scan}
        handler = routes.get(self.path)
        if handler is None:
            return self.send_json(404, {"error": "not found"})
        try:
            self.send_json(200, handler(self.read_json()))
        except BadRequest as e:
            self.send_json(400, {"error": str(e)})

    def do_DELETE(self):
        match = re.fullmatch(r"/api/miners/([0-9a-f]+)", self.path)
        if not match:
            return self.send_json(404, {"error": "not found"})
        self.send_json(200, api_remove(match.group(1)))


if __name__ == "__main__":
    load_state()
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print(f"Dualminer Control listening on :{PORT}", flush=True)
    server.serve_forever()
