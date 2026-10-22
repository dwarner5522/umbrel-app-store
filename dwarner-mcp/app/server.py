#!/usr/bin/env python3
"""Umbrel MCP Server - lets AI agents work with an Umbrel 0.5.4 over MCP.

Stdlib only. Serves two ports:
  * UI_PORT, behind Umbrel's app_proxy (Umbrel login): the settings page and its JSON API.
  * MCP_PORT, published on the LAN: the MCP endpoint (Streamable HTTP, JSON responses),
    which only answers requests carrying the access token as a bearer token.

Every area (Umbrel system, Verus Node, containers and logs, Bitcoin and Lightning) has
one switch for reading and one for making changes. Tools are listed and run only while
their switch is on.
"""
import base64
import hmac
import http.client
import http.server
import json
import os
import re
import secrets
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

VERSION = "1.0.0"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(HERE, "data"))
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
UI_PORT = int(os.environ.get("UI_PORT", 8080))
MCP_PORT = int(os.environ.get("MCP_PORT", 2119))
SELF_APP_ID = os.environ.get("SELF_APP_ID", "dwarner-mcp")

MANAGER_URL = "http://%s:%s" % (os.environ.get("MANAGER_IP", "10.21.21.4"), os.environ.get("MANAGER_PORT", "3006"))
SIGNALS_DIR = os.environ.get("SIGNALS_DIR", "/signals")
DOCKER_SOCKET = os.environ.get("DOCKER_SOCKET", "/docker-proxy/docker.sock")

VERUS = {k: os.environ.get("VERUS_RPC_" + k.upper(), "") for k in ("host", "port", "user", "pass")}
BITCOIN = {k: os.environ.get("BITCOIN_RPC_" + k.upper(), "") for k in ("host", "port", "user", "pass")}


def rest_url(prefix):
    host, port = os.environ.get(prefix + "_HOST"), os.environ.get(prefix + "_PORT")
    return "https://%s:%s" % (host, port) if host and port else ""


DEFAULT_CLN_URL = rest_url("CLN_REST")
DEFAULT_LND_URL = rest_url("LND_REST")

MAX_RESULT_CHARS = 100_000
APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

AREAS = {
    "system": "Umbrel system",
    "verus": "Verus Node",
    "docker": "App logs & containers",
    "bitcoin": "Bitcoin / Lightning",
}

# Self-signed certificates: the Lightning REST servers on an Umbrel use their own
INSECURE_TLS = ssl._create_unverified_context()

lock = threading.Lock()
activity = deque(maxlen=200)


# ---------------------------------------------------------------------------
# Settings

def default_settings():
    return {
        "token": secrets.token_urlsafe(32),
        "permissions": {area: {"read": False, "write": False} for area in AREAS},
        "umbrel": {"password": "", "jwt": "", "jwt_time": 0},
        "lightning": {"backend": "none", "cln_url": DEFAULT_CLN_URL, "cln_rune": "",
                      "lnd_url": DEFAULT_LND_URL, "lnd_macaroon": ""},
    }


settings = default_settings()


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        save_settings()
        return
    for key, value in saved.items():
        if isinstance(value, dict) and isinstance(settings.get(key), dict):
            for sub, subvalue in value.items():
                if isinstance(subvalue, dict) and isinstance(settings[key].get(sub), dict):
                    settings[key][sub].update(subvalue)
                else:
                    settings[key][sub] = subvalue
        else:
            settings[key] = value
    # A Lightning node installed after this app was set up
    light = settings["lightning"]
    light["cln_url"] = light["cln_url"] or DEFAULT_CLN_URL
    light["lnd_url"] = light["lnd_url"] or DEFAULT_LND_URL


def save_settings():
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp, SETTINGS_FILE)


def allowed(area, kind):
    with lock:
        return bool(settings["permissions"][area][kind])


class ToolError(Exception):
    """An error shown to the AI as the tool's result."""


# ---------------------------------------------------------------------------
# HTTP helpers

def http_json(url, method="GET", body=None, headers=None, timeout=20, context=None):
    """Returns the decoded JSON body. Raises ToolError with a short reason."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            detail = json.loads(raw)
        except ValueError:
            detail = raw.decode(errors="replace")[:300]
        raise HttpError(e.code, detail)
    except (OSError, ValueError) as e:
        raise ToolError("cannot reach %s: %s" % (urllib.parse.urlsplit(url).netloc, reason(e)))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace")


class HttpError(ToolError):
    def __init__(self, code, detail):
        self.code, self.detail = code, detail
        if isinstance(detail, dict):
            text = detail.get("message") or detail.get("error") or json.dumps(detail)[:300]
            if isinstance(text, dict):
                text = text.get("message") or json.dumps(text)[:300]
        else:
            text = detail
        super().__init__("HTTP %s: %s" % (code, text))


def reason(e):
    if isinstance(e, urllib.error.URLError):
        e = e.reason
    if isinstance(e, socket.timeout):
        return "timed out"
    return str(e) or e.__class__.__name__


def json_rpc(conf, method, params, timeout=60):
    """JSON-RPC 1.0 call to a bitcoind-style daemon (verusd, bitcoind)."""
    if not conf["host"] or not conf["port"]:
        raise ToolError("the node app is not installed, or was installed after this app started; restart this app")
    auth = base64.b64encode(("%s:%s" % (conf["user"], conf["pass"])).encode()).decode()
    url = "http://%s:%s/" % (conf["host"], conf["port"])
    payload = {"jsonrpc": "1.0", "id": "mcp", "method": method, "params": params}
    try:
        reply = http_json(url, "POST", payload, {"Authorization": "Basic " + auth}, timeout=timeout)
    except HttpError as e:
        # bitcoind-style daemons answer RPC errors with HTTP 500 and a JSON body
        if isinstance(e.detail, dict) and e.detail.get("error"):
            err = e.detail["error"]
            raise ToolError("RPC error %s: %s" % (err.get("code"), err.get("message")))
        if e.code == 401:
            raise ToolError("the node refused the RPC credentials")
        raise
    if not isinstance(reply, dict):
        raise ToolError("unexpected reply from the node")
    if reply.get("error"):
        err = reply["error"]
        raise ToolError("RPC error %s: %s" % (err.get("code"), err.get("message")))
    return reply.get("result")


# ---------------------------------------------------------------------------
# Umbrel 0.5.4 manager API

def manager_login(password, otp=""):
    body = {"password": password}
    if otp:
        body["otpToken"] = otp
    try:
        reply = http_json(MANAGER_URL + "/v1/account/login", "POST", body, timeout=15)
    except HttpError as e:
        raise ToolError("Umbrel login failed: %s" % (e.detail.get("message") if isinstance(e.detail, dict) else e))
    return reply["jwt"]


def manager(path, method="GET", body=None):
    """Calls the manager with the stored session, logging in again when it has expired."""
    with lock:
        jwt, password = settings["umbrel"]["jwt"], settings["umbrel"]["password"]
    if not jwt and not password:
        raise ToolError("not logged in to Umbrel: enter the Umbrel password on this app's page")

    def call(token):
        return http_json(MANAGER_URL + path, method, body, {"Authorization": "JWT " + token})

    if jwt:
        try:
            return call(jwt)
        except HttpError as e:
            if e.code != 401:
                raise
    if not password:
        with lock:
            settings["umbrel"]["jwt"] = ""
            save_settings()
        raise ToolError("the Umbrel session has expired (the Umbrel restarted): log in again on this app's page")
    jwt = manager_login(password)
    store_jwt(jwt)
    return call(jwt)


def store_jwt(jwt):
    with lock:
        settings["umbrel"]["jwt"] = jwt
        settings["umbrel"]["jwt_time"] = int(time.time())
        save_settings()


def refresh_session_forever():
    """The manager's sessions last a week: refresh daily so one login keeps working."""
    while True:
        time.sleep(3600)
        with lock:
            jwt, age = settings["umbrel"]["jwt"], time.time() - settings["umbrel"]["jwt_time"]
        if jwt and age > 86400:
            try:
                store_jwt(http_json(MANAGER_URL + "/v1/account/refresh", "POST", {},
                                    {"Authorization": "JWT " + jwt})["jwt"])
            except ToolError:
                pass  # expired: the next call logs in again or asks for it


APP_FIELDS = ("id", "name", "version", "tagline", "category", "developer", "port", "path",
              "dependencies", "updateAvailable", "website")


def app_summary(app):
    return {k: app[k] for k in APP_FIELDS if k in app}


def installed_app_ids():
    return {app["id"] for app in manager("/v1/apps?installed=1")}


def write_signal(name):
    """Asks Umbrel's event daemon (karen) to run `scripts/app <command> <app>`."""
    if not os.path.isdir(SIGNALS_DIR):
        raise ToolError("Umbrel's signal folder is not available; this needs Umbrel 0.5.4")
    path = os.path.join(SIGNALS_DIR, name)
    try:
        with open(path, "w") as f:
            f.write("true")
    except PermissionError:
        # A signal file written earlier by Umbrel itself belongs to root: replace it
        os.unlink(path)
        with open(path, "w") as f:
            f.write("true")


def system_status(_):
    out = {}
    for key, path in (("info", "/v1/system/info"), ("storage", "/v1/system/storage"),
                      ("memory", "/v1/system/memory"), ("temperature", "/v1/system/temperature"),
                      ("uptime_seconds", "/v1/system/uptime"), ("update", "/v1/system/get-update"),
                      ("sd_card_failing", "/v1/system/is-sd-card-failing")):
        try:
            out[key] = manager(path)
        except HttpError as e:
            out[key] = "unavailable (%s)" % e
    return out


def list_installed_apps(_):
    apps = [app_summary(a) for a in manager("/v1/apps?installed=1")]
    # The installed list has no update flag: take it from the app stores
    updates = set()
    for repo in app_store_ids():
        try:
            updates |= {a["id"] for a in manager("/v1/apps?repo=" + urllib.parse.quote(repo))
                        if a.get("updateAvailable")}
        except ToolError:
            pass
    for app in apps:
        app["updateAvailable"] = app["id"] in updates
    return apps


def app_store_ids():
    ids = ["umbrel"]
    try:
        for store in manager("/v1/account/info").get("communityAppRepos") or []:
            if isinstance(store, dict) and store.get("id"):
                ids.append(store["id"])
    except (HttpError, AttributeError):
        pass
    return ids


def list_store_apps(args):
    store = args.get("store") or "umbrel"
    installed = installed_app_ids()
    apps = []
    for app in manager("/v1/apps?repo=" + urllib.parse.quote(store)):
        summary = app_summary(app)
        summary["installed"] = app["id"] in installed
        apps.append(summary)
    return {"store": store, "stores": app_store_ids(), "apps": apps}


def check_app_id(app_id):
    if not isinstance(app_id, str) or not APP_ID_RE.match(app_id):
        raise ToolError("invalid app id")
    return app_id


def app_control(args):
    app_id, action = check_app_id(args.get("app_id")), args.get("action")
    if action not in ("start", "stop", "restart"):
        raise ToolError("action must be start, stop or restart")
    if app_id == SELF_APP_ID and action == "stop":
        raise ToolError("this app cannot stop itself: the AI would lose its connection")
    if app_id not in installed_app_ids():
        raise ToolError("%s is not installed" % app_id)
    write_signal("app-%s-%s" % (action, app_id))
    return {"app_id": app_id, "action": action,
            "result": "requested; Umbrel carries it out in the background, which can take a minute"}


def app_install(args):
    app_id = check_app_id(args.get("app_id"))
    manager("/v1/apps/%s/install" % app_id, "POST", {})
    return {"app_id": app_id, "result": "install started; it can take several minutes"}


def app_update(args):
    app_id = check_app_id(args.get("app_id"))
    manager("/v1/apps/%s/update" % app_id, "POST", {})
    return {"app_id": app_id, "result": "update started; it can take several minutes"}


# ---------------------------------------------------------------------------
# Docker (through the filtering proxy in this app, which allows only the calls below)

class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=30):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)


def docker(path, method="GET", raw=False):
    conn = UnixHTTPConnection(DOCKER_SOCKET)
    try:
        conn.request(method, path, headers={"Host": "docker"})
        resp = conn.getresponse()
        body = resp.read()
    except OSError as e:
        raise ToolError("cannot reach Docker: %s" % reason(e))
    finally:
        conn.close()
    if resp.status >= 400:
        try:
            message = json.loads(body).get("message")
        except (ValueError, AttributeError):
            message = body.decode(errors="replace").strip()[:200]
        raise ToolError("Docker: %s" % (message or "HTTP %s" % resp.status))
    if raw:
        return body
    return json.loads(body) if body else None


def check_container(name):
    if not isinstance(name, str) or not CONTAINER_RE.match(name):
        raise ToolError("invalid container name")
    return name


def list_containers(args):
    app_id = args.get("app_id")
    rows = []
    for c in docker("/containers/json?all=1"):
        labels = c.get("Labels") or {}
        project = labels.get("com.docker.compose.project", "")
        if app_id and project != app_id:
            continue
        rows.append({"name": (c.get("Names") or ["?"])[0].lstrip("/"), "app": project,
                     "service": labels.get("com.docker.compose.service", ""),
                     "state": c.get("State"), "status": c.get("Status"), "image": c.get("Image")})
    rows.sort(key=lambda r: (r["app"], r["name"]))
    return rows


def container_logs(args):
    name = check_container(args.get("container"))
    tail = max(1, min(int(args.get("lines") or 200), 2000))
    query = {"stdout": 1, "stderr": 1, "tail": tail, "timestamps": 1}
    if args.get("since_minutes"):
        query["since"] = int(time.time() - float(args["since_minutes"]) * 60)
    info = docker("/containers/%s/json" % name)
    body = docker("/containers/%s/logs?%s" % (name, urllib.parse.urlencode(query)), raw=True)
    if not info.get("Config", {}).get("Tty"):
        body = demux_logs(body)
    return body.decode(errors="replace")


def demux_logs(body):
    """Strips Docker's 8-byte frame headers from a non-TTY log stream."""
    out, i = bytearray(), 0
    while i + 8 <= len(body):
        size = int.from_bytes(body[i + 4:i + 8], "big")
        out += body[i + 8:i + 8 + size]
        i += 8 + size
    return bytes(out)


def container_stats(args):
    name = check_container(args.get("container"))
    info = docker("/containers/%s/json" % name)
    state = info.get("State", {})
    out = {"name": name, "state": state.get("Status"), "started_at": state.get("StartedAt"),
           "restart_count": info.get("RestartCount"), "exit_code": state.get("ExitCode"),
           "oom_killed": state.get("OOMKilled"), "image": info.get("Config", {}).get("Image"),
           "health": (state.get("Health") or {}).get("Status")}
    if state.get("Running"):
        s = docker("/containers/%s/stats?stream=false" % name)
        cpu = s.get("cpu_stats", {})
        pre = s.get("precpu_stats", {})
        cpu_delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - pre.get("cpu_usage", {}).get("total_usage", 0)
        sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        cpus = cpu.get("online_cpus") or 1
        mem = s.get("memory_stats", {})
        out["cpu_percent"] = round(cpu_delta / sys_delta * cpus * 100, 1) if sys_delta > 0 else 0
        out["memory_mb"] = round((mem.get("usage", 0) - mem.get("stats", {}).get("cache", 0)) / 2**20, 1)
        out["memory_limit_mb"] = round(mem.get("limit", 0) / 2**20, 1)
    return out


def container_control(args):
    name, action = check_container(args.get("container")), args.get("action")
    if action not in ("start", "stop", "restart"):
        raise ToolError("action must be start, stop or restart")
    if name.startswith(SELF_APP_ID + "_") and action == "stop":
        raise ToolError("this app cannot stop its own containers")
    docker("/containers/%s/%s" % (name, action), "POST")
    return {"container": name, "action": action, "result": "done"}


# ---------------------------------------------------------------------------
# Verus Node

VERUS_READ = {
    # chain and network
    "getinfo", "getblockchaininfo", "getblockcount", "getbestblockhash", "getblock", "getblockhash",
    "getblockheader", "getchaintips", "getdifficulty", "getmempoolinfo", "getrawmempool",
    "getrawtransaction", "decoderawtransaction", "decodescript", "gettxout", "gettxoutsetinfo",
    "getnetworkinfo", "getpeerinfo", "getconnectioncount", "getnettotals", "getmininginfo",
    "getnetworkhashps", "getnetworksolps", "getlocalsolps", "getblocksubsidy", "getgenerate",
    "coinsupply", "getspentinfo", "help",
    # address index
    "getaddressbalance", "getaddressutxos", "getaddresstxids", "getaddressdeltas", "getaddressmempool",
    # identities, currencies, PBaaS and DeFi
    "getidentity", "getidentitycontent", "getidentitieswithaddress", "getidentitieswithrevocation",
    "getidentitieswithrecovery", "getidentityhistory", "listidentities", "getcurrency",
    "listcurrencies", "getcurrencystate", "getcurrencyconverters", "estimateconversion",
    "getexports", "getimports", "getlastimportfrom", "getpendingtransfers", "getreservedeposits",
    "getnotarizationdata", "getbestproofroot", "getoffers", "listopenoffers", "getvdxfid",
    "getcurrencytrust", "getidentitytrust", "getmergedblocktemplate",
    # wallet, read only
    "getwalletinfo", "getbalance", "getunconfirmedbalance", "getcurrencybalance", "listtransactions",
    "listunspent", "listreceivedbyaddress", "gettransaction", "z_getbalance", "z_gettotalbalance",
    "z_listunspent", "z_listaddresses", "z_listreceivedbyaddress", "getaddressesbyaccount",
    "z_getoperationstatus", "validateaddress", "z_validateaddress", "verifymessage",
    "verifyhash", "verifysignature", "verifyfile",
}

VERUS_WRITE = {
    "sendcurrency", "sendtoaddress", "sendmany", "z_sendmany", "sendrawtransaction",
    "getnewaddress", "z_getnewaddress", "z_getoperationresult",
    "registernamecommitment", "registeridentity", "updateidentity", "revokeidentity",
    "recoveridentity", "setidentitytimelock", "makeoffer", "takeoffer", "closeoffers",
    "signmessage", "signdata", "signhash", "signfile", "setgenerate", "setcurrencytrust",
    "setidentitytrust", "lockunspent",
}
# Never offered, whatever the switches say: they export keys, change the wallet file or stop the node.
# dumpprivkey, z_exportkey, z_exportviewingkey, dumpwallet, z_exportwallet, backupwallet,
# importprivkey, importwallet, z_importkey, encryptwallet, walletpassphrase, stop, ...


def verus_methods():
    methods = set()
    if allowed("verus", "read"):
        methods |= VERUS_READ
    if allowed("verus", "write"):
        methods |= VERUS_WRITE
    return methods


def verus_rpc(args):
    method = args.get("method", "")
    params = args.get("params") or []
    if method not in verus_methods():
        if method in VERUS_READ or method in VERUS_WRITE:
            raise ToolError("%s is switched off on this app's page" % method)
        raise ToolError("%s is not available through this server" % method)
    if not isinstance(params, list):
        raise ToolError("params must be a list")
    return json_rpc(VERUS, method, params)


def verus_status(_):
    info = json_rpc(VERUS, "getinfo", [])
    chain = json_rpc(VERUS, "getblockchaininfo", [])
    mining = json_rpc(VERUS, "getmininginfo", [])
    return {"version": info.get("VRSCversion") or info.get("version"), "blocks": info.get("blocks"),
            "longest_chain": info.get("longestchain"), "connections": info.get("connections"),
            "sync_progress": chain.get("verificationprogress"), "headers": chain.get("headers"),
            "size_on_disk": chain.get("size_on_disk"), "difficulty": chain.get("difficulty"),
            "network_hashps": mining.get("networkhashps"), "generate": mining.get("generate"),
            "wallet_balance": info.get("balance")}


# ---------------------------------------------------------------------------
# Bitcoin Node

BITCOIN_READ = {
    "getblockchaininfo", "getblockcount", "getbestblockhash", "getblock", "getblockhash",
    "getblockheader", "getblockstats", "getchaintips", "getchaintxstats", "getdifficulty",
    "getmempoolinfo", "getmempoolentry", "getmempoolancestors", "getmempooldescendants",
    "getrawmempool", "getrawtransaction", "decoderawtransaction", "decodescript", "decodepsbt",
    "gettxout", "gettxoutsetinfo", "getnetworkinfo", "getpeerinfo", "getconnectioncount",
    "getnettotals", "getmininginfo", "getnetworkhashps", "estimatesmartfee", "getindexinfo",
    "getdeploymentinfo", "uptime", "validateaddress", "testmempoolaccept", "help",
}
BITCOIN_WRITE = {"sendrawtransaction"}


def bitcoin_rpc(args):
    method = args.get("method", "")
    params = args.get("params") or []
    methods = (BITCOIN_READ if allowed("bitcoin", "read") else set()) | \
              (BITCOIN_WRITE if allowed("bitcoin", "write") else set())
    if method not in methods:
        if method in BITCOIN_READ or method in BITCOIN_WRITE:
            raise ToolError("%s is switched off on this app's page" % method)
        raise ToolError("%s is not available through this server" % method)
    if not isinstance(params, list):
        raise ToolError("params must be a list")
    return json_rpc(BITCOIN, method, params)


def bitcoin_status(_):
    chain = json_rpc(BITCOIN, "getblockchaininfo", [])
    net = json_rpc(BITCOIN, "getnetworkinfo", [])
    mempool = json_rpc(BITCOIN, "getmempoolinfo", [])
    return {"chain": chain.get("chain"), "blocks": chain.get("blocks"), "headers": chain.get("headers"),
            "sync_progress": chain.get("verificationprogress"), "pruned": chain.get("pruned"),
            "size_on_disk": chain.get("size_on_disk"), "version": net.get("subversion"),
            "connections": net.get("connections"), "mempool_txs": mempool.get("size"),
            "mempool_bytes": mempool.get("bytes")}


# ---------------------------------------------------------------------------
# Lightning: Core Lightning over CLNRest (rune) or LND over REST (macaroon)

def lightning_conf():
    with lock:
        conf = dict(settings["lightning"])
    if conf["backend"] == "cln":
        if not conf["cln_url"] or not conf["cln_rune"]:
            raise ToolError("Core Lightning is selected but its REST address or rune is missing on this app's page")
    elif conf["backend"] == "lnd":
        if not conf["lnd_url"] or not conf["lnd_macaroon"]:
            raise ToolError("LND is selected but its REST address or macaroon is missing on this app's page")
    else:
        raise ToolError("no Lightning node is set up on this app's page")
    return conf


def cln(conf, method, params=None):
    return http_json(conf["cln_url"].rstrip("/") + "/v1/" + method, "POST", params or {},
                     {"Rune": conf["cln_rune"]}, timeout=120, context=INSECURE_TLS)


def lnd(conf, path, method="GET", body=None):
    return http_json(conf["lnd_url"].rstrip("/") + path, method, body,
                     {"Grpc-Metadata-macaroon": conf["lnd_macaroon"]}, timeout=120, context=INSECURE_TLS)


def lightning_info(_):
    conf = lightning_conf()
    if conf["backend"] == "cln":
        info = cln(conf, "getinfo")
        funds = cln(conf, "listfunds")
        onchain = sum(o.get("amount_msat", 0) for o in funds.get("outputs", [])
                      if o.get("status") == "confirmed") // 1000
        channels = sum(c.get("our_amount_msat", 0) for c in funds.get("channels", [])) // 1000
        return {"implementation": "Core Lightning", "id": info.get("id"), "alias": info.get("alias"),
                "version": info.get("version"), "network": info.get("network"),
                "block_height": info.get("blockheight"), "peers": info.get("num_peers"),
                "active_channels": info.get("num_active_channels"),
                "pending_channels": info.get("num_pending_channels"),
                "onchain_confirmed_sat": onchain, "channel_balance_sat": channels}
    info = lnd(conf, "/v1/getinfo")
    chain = lnd(conf, "/v1/balance/blockchain")
    channels = lnd(conf, "/v1/balance/channels")
    return {"implementation": "LND", "id": info.get("identity_pubkey"), "alias": info.get("alias"),
            "version": info.get("version"), "block_height": info.get("block_height"),
            "synced_to_chain": info.get("synced_to_chain"), "peers": info.get("num_peers"),
            "active_channels": info.get("num_active_channels"),
            "pending_channels": info.get("num_pending_channels"),
            "onchain_confirmed_sat": chain.get("confirmed_balance"),
            "onchain_unconfirmed_sat": chain.get("unconfirmed_balance"),
            "channel_balance_sat": channels.get("balance")}


LIGHTNING_LISTS = ("channels", "peers", "invoices", "payments", "forwards", "funds")


def lightning_list(args):
    kind, limit = args.get("kind"), max(1, min(int(args.get("limit") or 50), 500))
    if kind not in LIGHTNING_LISTS:
        raise ToolError("kind must be one of " + ", ".join(LIGHTNING_LISTS))
    conf = lightning_conf()
    if conf["backend"] == "cln":
        method, key = {"channels": ("listpeerchannels", "channels"), "peers": ("listpeers", "peers"),
                       "invoices": ("listinvoices", "invoices"), "payments": ("listpays", "pays"),
                       "forwards": ("listforwards", "forwards"), "funds": ("listfunds", None)}[kind]
        reply = cln(conf, method)
        return reply if key is None else reply.get(key, [])[-limit:]
    if kind == "forwards":
        return lnd(conf, "/v1/switch", "POST", {"num_max_events": limit}).get("forwarding_events", [])
    path, key = {"channels": ("/v1/channels", "channels"), "peers": ("/v1/peers", "peers"),
                 "invoices": ("/v1/invoices?reversed=true&num_max_invoices=%d" % limit, "invoices"),
                 "payments": ("/v1/payments?reversed=true&max_payments=%d" % limit, "payments"),
                 "funds": ("/v1/utxos?min_confs=0&max_confs=999999999", "utxos")}[kind]
    return lnd(conf, path).get(key, [])[-limit:]


def lightning_create_invoice(args):
    conf = lightning_conf()
    amount = int(args.get("amount_sat") or 0)
    if amount < 0:
        raise ToolError("amount_sat must not be negative")
    description = str(args.get("description") or "")
    expiry = int(args.get("expiry_seconds") or 3600)
    if conf["backend"] == "cln":
        reply = cln(conf, "invoice", {"amount_msat": amount * 1000 if amount else "any",
                                      "label": "mcp-%d-%s" % (time.time(), secrets.token_hex(3)),
                                      "description": description, "expiry": expiry})
        return {"bolt11": reply.get("bolt11"), "payment_hash": reply.get("payment_hash"),
                "expires_at": reply.get("expires_at")}
    reply = lnd(conf, "/v1/invoices", "POST", {"value": str(amount), "memo": description, "expiry": str(expiry)})
    return {"bolt11": reply.get("payment_request"), "add_index": reply.get("add_index")}


def lightning_pay_invoice(args):
    conf = lightning_conf()
    bolt11 = str(args.get("bolt11") or "").strip()
    if not bolt11.lower().startswith("ln"):
        raise ToolError("bolt11 must be a Lightning invoice")
    max_fee = args.get("max_fee_sat")
    if conf["backend"] == "cln":
        params = {"bolt11": bolt11}
        if max_fee is not None:
            params["maxfee"] = int(max_fee) * 1000
        return cln(conf, "pay", params)
    body = {"payment_request": bolt11}
    if max_fee is not None:
        body["fee_limit"] = {"fixed": str(int(max_fee))}
    reply = lnd(conf, "/v1/channels/transactions", "POST", body)
    if reply.get("payment_error"):
        raise ToolError("payment failed: %s" % reply["payment_error"])
    return reply


# ---------------------------------------------------------------------------
# MCP tools

def schema(properties=None, required=()):
    return {"type": "object", "properties": properties or {}, "required": list(required),
            "additionalProperties": False}


RPC_PARAMS = {"type": "array", "description": "Positional RPC parameters, e.g. [\"Verus Coin Foundation@\"]",
              "items": {}}
APP_ID = {"type": "string", "description": "Umbrel app id, e.g. bitcoin or dwarner-verus"}
CONTAINER = {"type": "string", "description": "Container name from docker_list_containers, e.g. dwarner-verus_app_1"}

# name, area, kind, description, input schema, handler
TOOLS = [
    ("umbrel_system_status", "system", "read",
     "Umbrel version, available update, storage, memory, CPU temperature and uptime.",
     schema(), system_status),
    ("umbrel_list_installed_apps", "system", "read",
     "Apps installed on this Umbrel, with versions and whether an update is available. "
     "Use docker_list_containers to see whether they are running.",
     schema(), list_installed_apps),
    ("umbrel_list_store_apps", "system", "read",
     "Apps in an app store (default: Umbrel's own; community stores by id) and whether each is installed.",
     schema({"store": {"type": "string", "description": "App store id, default umbrel"}}), list_store_apps),
    ("umbrel_app_control", "system", "write",
     "Start, stop or restart an installed Umbrel app, as its menu on the Umbrel does.",
     schema({"app_id": APP_ID, "action": {"type": "string", "enum": ["start", "stop", "restart"]}},
            ("app_id", "action")), app_control),
    ("umbrel_install_app", "system", "write",
     "Install an app from one of the Umbrel's app stores.",
     schema({"app_id": APP_ID}, ("app_id",)), app_install),
    ("umbrel_update_app", "system", "write",
     "Update an installed app to the version in its app store.",
     schema({"app_id": APP_ID}, ("app_id",)), app_update),

    ("docker_list_containers", "docker", "read",
     "Every container on the Umbrel with its app, state and status.",
     schema({"app_id": {"type": "string", "description": "Only this app's containers"}}), list_containers),
    ("docker_container_logs", "docker", "read",
     "The latest log lines of a container, with timestamps.",
     schema({"container": CONTAINER,
             "lines": {"type": "integer", "description": "How many lines, 1-2000 (default 200)"},
             "since_minutes": {"type": "number", "description": "Only lines from the last N minutes"}},
            ("container",)), container_logs),
    ("docker_container_stats", "docker", "read",
     "State, restart count, CPU and memory use of a container.",
     schema({"container": CONTAINER}, ("container",)), container_stats),
    ("docker_container_control", "docker", "write",
     "Start, stop or restart one container.",
     schema({"container": CONTAINER, "action": {"type": "string", "enum": ["start", "stop", "restart"]}},
            ("container", "action")), container_control),

    ("verus_status", "verus", "read",
     "Verus Node summary: version, block height, sync progress, peers, difficulty and hashrate.",
     schema(), verus_status),
    ("verus_rpc", "verus", "any", None,  # description depends on the switches
     schema({"method": {"type": "string"}, "params": RPC_PARAMS}, ("method",)), verus_rpc),

    ("bitcoin_status", "bitcoin", "read",
     "Bitcoin Node summary: chain, block height, sync progress, peers and mempool.",
     schema(), bitcoin_status),
    ("bitcoin_rpc", "bitcoin", "any", None,
     schema({"method": {"type": "string"}, "params": RPC_PARAMS}, ("method",)), bitcoin_rpc),
    ("lightning_info", "bitcoin", "read",
     "Lightning node summary: alias, peers, channels and balances.",
     schema(), lightning_info),
    ("lightning_list", "bitcoin", "read",
     "List the Lightning node's channels, peers, invoices, payments, forwards or on-chain funds.",
     schema({"kind": {"type": "string", "enum": list(LIGHTNING_LISTS)},
             "limit": {"type": "integer", "description": "Most recent N entries, default 50"}},
            ("kind",)), lightning_list),
    ("lightning_create_invoice", "bitcoin", "write",
     "Create a Lightning invoice to receive a payment.",
     schema({"amount_sat": {"type": "integer", "description": "Amount in sats; 0 for any amount"},
             "description": {"type": "string"},
             "expiry_seconds": {"type": "integer", "description": "Default 3600"}},
            ("amount_sat",)), lightning_create_invoice),
    ("lightning_pay_invoice", "bitcoin", "write",
     "Pay a Lightning invoice from this node. This spends funds.",
     schema({"bolt11": {"type": "string"},
             "max_fee_sat": {"type": "integer", "description": "Highest routing fee to accept"}},
            ("bolt11",)), lightning_pay_invoice),
]
TOOLS_BY_NAME = {t[0]: t for t in TOOLS}


def tool_enabled(tool):
    _, area, kind = tool[:3]
    if kind == "any":
        return allowed(area, "read") or allowed(area, "write")
    return allowed(area, kind)


def rpc_description(name, read_set, write_set, area):
    parts = []
    if allowed(area, "read"):
        parts.append("Read methods: " + ", ".join(sorted(read_set)) + ".")
    if allowed(area, "write"):
        parts.append("Methods that change state (they can spend funds): " + ", ".join(sorted(write_set)) + ".")
    node = "Verus Node (verusd)" if area == "verus" else "Bitcoin Node (bitcoind)"
    return "Call %s over JSON-RPC. Only these methods are allowed. %s" % (node, " ".join(parts))


def tool_list():
    tools = []
    for tool in TOOLS:
        if not tool_enabled(tool):
            continue
        name, area, kind, description, input_schema, _ = tool
        if name == "verus_rpc":
            description = rpc_description(name, VERUS_READ, VERUS_WRITE, area)
        elif name == "bitcoin_rpc":
            description = rpc_description(name, BITCOIN_READ, BITCOIN_WRITE, area)
        read_only = kind == "read" or (kind == "any" and not allowed(area, "write"))
        tools.append({"name": name, "description": description, "inputSchema": input_schema,
                      "annotations": {"readOnlyHint": read_only, "destructiveHint": not read_only,
                                      "openWorldHint": False}})
    return tools


def call_tool(name, args):
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return None
    started = time.time()
    try:
        if not tool_enabled(tool):
            raise ToolError("%s is switched off on this app's page" % AREAS[tool[1]])
        result = tool[5](args or {})
        text = result if isinstance(result, str) else json.dumps(result, indent=1, default=str)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + "\n... (cut off at %d characters)" % MAX_RESULT_CHARS
        ok = True
    except ToolError as e:
        text, ok = str(e), False
    except (ValueError, TypeError, KeyError) as e:
        text, ok = "bad arguments or reply: %s" % e, False
    log_activity(name, args, ok, text if not ok else "", time.time() - started)
    return {"content": [{"type": "text", "text": text}], "isError": not ok}


def log_activity(tool, args, ok, error, seconds):
    summary = json.dumps(args or {}, default=str)
    activity.appendleft({"time": int(time.time()), "tool": tool,
                         "args": summary if len(summary) <= 200 else summary[:200] + "...",
                         "ok": ok, "error": error[:300], "ms": int(seconds * 1000)})


INSTRUCTIONS = (
    "Tools for an Umbrel 0.5.4 home server: its system and apps, the Verus Node app, container logs, "
    "and the Bitcoin and Lightning nodes. Which tools exist depends on switches the owner sets in the "
    "Umbrel MCP Server app; if a tool you need is missing, ask the owner to switch it on and reconnect."
)


def handle_rpc(message):
    """Handles one JSON-RPC message. Returns the reply, or None for a notification."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
        return rpc_error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request")
    method, msg_id, params = message["method"], message.get("id"), message.get("params") or {}
    if "id" not in message:
        return None  # notifications/initialized and friends need no answer
    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return rpc_result(msg_id, {"protocolVersion": version,
                                   "capabilities": {"tools": {"listChanged": False}},
                                   "serverInfo": {"name": "umbrel-mcp", "title": "Umbrel MCP Server",
                                                  "version": VERSION},
                                   "instructions": INSTRUCTIONS})
    if method == "ping":
        return rpc_result(msg_id, {})
    if method == "tools/list":
        return rpc_result(msg_id, {"tools": tool_list()})
    if method == "tools/call":
        result = call_tool(params.get("name"), params.get("arguments"))
        if result is None:
            return rpc_error(msg_id, -32602, "Unknown tool: %s" % params.get("name"))
        return rpc_result(msg_id, result)
    if method in ("resources/list", "prompts/list"):
        return rpc_result(msg_id, {method.split("/")[0]: []})
    return rpc_error(msg_id, -32601, "Method not found: %s" % method)


def rpc_result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def rpc_error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# HTTP servers

class BaseHandler(http.server.BaseHTTPRequestHandler):
    server_version = "UmbrelMCP/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, code, obj, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_empty(self, code, headers=None):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            raise ValueError("request too large")
        return json.loads(self.rfile.read(length) or b"null")


class McpHandler(BaseHandler):
    def authorized(self):
        header = self.headers.get("Authorization", "")
        with lock:
            token = settings["token"]
        return header.startswith("Bearer ") and hmac.compare_digest(header[7:].strip().encode(), token.encode())

    def deny(self):
        self.send_json(401, {"error": "missing or wrong access token"},
                       {"WWW-Authenticate": 'Bearer realm="umbrel-mcp"'})

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path.rstrip("/") != "/mcp":
            return self.send_json(404, {"error": "the MCP endpoint is /mcp"})
        if not self.authorized():
            return self.deny()
        try:
            message = self.read_json()
        except ValueError:
            return self.send_json(400, rpc_error(None, -32700, "Parse error"))
        if isinstance(message, list):
            replies = [r for r in (handle_rpc(m) for m in message) if r is not None]
            reply = replies or None
        else:
            reply = handle_rpc(message)
        if reply is None:
            return self.send_empty(202)
        headers = {}
        if isinstance(message, dict) and message.get("method") == "initialize":
            headers["Mcp-Session-Id"] = secrets.token_hex(16)
        self.send_json(200, reply, headers)

    def do_GET(self):
        if urllib.parse.urlsplit(self.path).path.rstrip("/") != "/mcp":
            return self.send_json(404, {"error": "the MCP endpoint is /mcp"})
        if not self.authorized():
            return self.deny()
        # No server-to-client stream: every reply comes back on its POST
        self.send_empty(405, {"Allow": "POST, DELETE"})

    def do_DELETE(self):
        if not self.authorized():
            return self.deny()
        self.send_empty(204)


def status_of(area):
    """A short line for the page: can this area be reached right now?"""
    try:
        if area == "system":
            with lock:
                logged_in = bool(settings["umbrel"]["jwt"] or settings["umbrel"]["password"])
            if not logged_in:
                return {"ok": False, "text": "Log in below with your Umbrel password"}
            info = manager("/v1/system/info")
            return {"ok": True, "text": "Connected to %s" % (info.get("name") or "Umbrel")}
        if area == "verus":
            if not VERUS["port"]:
                return {"ok": False, "text": "Verus Node is not installed"}
            blocks = json_rpc(VERUS, "getblockcount", [], timeout=5)
            return {"ok": True, "text": "Connected, block %s" % blocks}
        if area == "docker":
            docker("/_ping", raw=True)
            return {"ok": True, "text": "Connected to Docker"}
        if area == "bitcoin":
            parts = []
            ok = False
            if BITCOIN["host"]:
                try:
                    parts.append("Bitcoin block %s" % json_rpc(BITCOIN, "getblockcount", [], timeout=5))
                    ok = True
                except ToolError as e:
                    parts.append("Bitcoin: %s" % e)
            else:
                parts.append("Bitcoin Node is not installed")
            with lock:
                backend = settings["lightning"]["backend"]
            if backend != "none":
                try:
                    alias = lightning_info({}).get("alias")
                    parts.append("Lightning: %s" % (alias or "connected"))
                    ok = True
                except ToolError as e:
                    parts.append("Lightning: %s" % e)
            return {"ok": ok, "text": " · ".join(parts)}
    except ToolError as e:
        return {"ok": False, "text": str(e)}
    return {"ok": False, "text": "unknown"}


def public_settings():
    with lock:
        light = settings["lightning"]
        return {
            "version": VERSION,
            "token": settings["token"],
            "mcp_port": MCP_PORT,
            "permissions": settings["permissions"],
            "areas": AREAS,
            "umbrel": {"logged_in": bool(settings["umbrel"]["jwt"]),
                       "remembered": bool(settings["umbrel"]["password"])},
            "lightning": {"backend": light["backend"], "cln_url": light["cln_url"],
                          "lnd_url": light["lnd_url"], "cln_rune_set": bool(light["cln_rune"]),
                          "lnd_macaroon_set": bool(light["lnd_macaroon"])},
        }


class UiHandler(BaseHandler):
    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return self.wfile.write(body)
        if path == "/api/settings":
            return self.send_json(200, public_settings())
        if path == "/api/status":
            return self.send_json(200, {area: status_of(area) for area in AREAS})
        if path == "/api/activity":
            return self.send_json(200, list(activity))
        if path == "/api/tools":
            return self.send_json(200, [t["name"] for t in tool_list()])
        self.send_json(404, {"error": "not found"})

    def do_POST(self):
        # A JSON body can't be sent cross-site without a CORS preflight, which this server never allows
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.send_json(415, {"error": "expected JSON"})
        try:
            body = self.read_json() or {}
            result = self.route(urllib.parse.urlsplit(self.path).path, body)
        except ToolError as e:
            return self.send_json(400, {"error": str(e)})
        except (ValueError, TypeError, KeyError) as e:
            return self.send_json(400, {"error": "bad request: %s" % e})
        if result is None:
            return self.send_json(404, {"error": "not found"})
        self.send_json(200, result)

    def route(self, path, body):
        if path == "/api/permissions":
            area, kind = body["area"], body["kind"]
            if area not in AREAS or kind not in ("read", "write"):
                raise ToolError("unknown switch")
            with lock:
                settings["permissions"][area][kind] = bool(body["on"])
                save_settings()
            return public_settings()
        if path == "/api/token":
            with lock:
                settings["token"] = secrets.token_urlsafe(32)
                save_settings()
            return public_settings()
        if path == "/api/umbrel/login":
            password = str(body.get("password") or "")
            if not password:
                raise ToolError("enter your Umbrel password")
            jwt = manager_login(password, str(body.get("otp") or "").strip())
            with lock:
                settings["umbrel"]["password"] = password if body.get("remember") else ""
            store_jwt(jwt)
            return public_settings()
        if path == "/api/umbrel/logout":
            with lock:
                settings["umbrel"] = {"password": "", "jwt": "", "jwt_time": 0}
                save_settings()
            return public_settings()
        if path == "/api/lightning":
            backend = body.get("backend")
            if backend not in ("none", "cln", "lnd"):
                raise ToolError("unknown Lightning node")
            with lock:
                light = settings["lightning"]
                light["backend"] = backend
                for key in ("cln_url", "lnd_url"):
                    if key in body:
                        url = str(body[key]).strip()
                        if url and not re.match(r"^https?://", url):
                            raise ToolError("the REST address must start with https:// or http://")
                        light[key] = url
                if body.get("cln_rune"):
                    light["cln_rune"] = str(body["cln_rune"]).strip()
                if body.get("lnd_macaroon"):
                    macaroon = re.sub(r"\s+", "", str(body["lnd_macaroon"]))
                    if not re.match(r"^[0-9a-fA-F]+$", macaroon):
                        raise ToolError("paste the macaroon as hex (xxd -ps -c 1000 readonly.macaroon)")
                    light["lnd_macaroon"] = macaroon
                save_settings()
            return public_settings()
        return None


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    load_settings()
    threading.Thread(target=refresh_session_forever, daemon=True).start()
    mcp = Server(("0.0.0.0", MCP_PORT), McpHandler)
    threading.Thread(target=mcp.serve_forever, daemon=True).start()
    print("Umbrel MCP Server %s: page on :%d, MCP on :%d/mcp" % (VERSION, UI_PORT, MCP_PORT), flush=True)
    Server(("0.0.0.0", UI_PORT), UiHandler).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
