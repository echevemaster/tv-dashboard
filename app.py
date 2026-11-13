# /// script
# requires-python = ">=3.12"
# dependencies = ["aiowebostv==1.1.0"]
# ///
"""Local dashboard to control LG webOS TVs on the home network.

Run with:
  uv run app.py --discover        -> list LG webOS TVs found on the LAN (JSON)
  uv run app.py --pair <tv_id>    -> pair once with a TV and record its MACs
  uv run app.py --inspect <tv_id> -> print app ids and input ids (JSON)
  uv run app.py                 -> only this machine:  http://127.0.0.1:8765
  uv run app.py --lan           -> whole LAN (phone), PIN required for non-local clients

See README.md for setup.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import os
import re
import secrets
import socket
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any

import aiohttp
from aiohttp import web
from aiowebostv import WebOsClient

BASE_DIR = Path(__file__).parent
KEYS_FILE = BASE_DIR / "keys.json"
CONFIG_FILE = BASE_DIR / "config.json"
SCENES_FILE = BASE_DIR / "scenes.json"
PORT = 8765
LAN_MODE = "--lan" in sys.argv
BIND_HOST = "0.0.0.0" if LAN_MODE else "127.0.0.1"

TVS_FILE = BASE_DIR / "tvs.json"
WOL_PORT = 9


def load_tvs() -> dict[str, dict[str, Any]]:
    """Load the TV list from tvs.json (private, see tvs.example.json)."""
    try:
        tvs = json.loads(TVS_FILE.read_text())
    except FileNotFoundError:
        if "--discover" in sys.argv:
            return {}
        sys.exit(
            "tvs.json not found. Run 'uv run app.py --discover' or copy "
            "tvs.example.json to tvs.json and edit it."
        )
    except json.JSONDecodeError as exc:
        sys.exit(f"tvs.json is not valid JSON: {exc}")
    for tv_id, tv in tvs.items():
        # ids are embedded in HTML attributes and URL paths: keep them simple
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", tv_id):
            sys.exit(f"tvs.json: id '{tv_id}' must be 1-32 chars of letters, digits, '-' or '_'")
        if not tv.get("host"):
            sys.exit(f"tvs.json: '{tv_id}' is missing 'host'")
        tv.setdefault("name", tv_id)
        tv.setdefault("macs", [])
    return tvs


TVS = load_tvs()

BUTTONS = {"UP", "DOWN", "LEFT", "RIGHT", "ENTER", "BACK", "HOME", "EXIT"}

log = logging.getLogger("tv-dashboard")


PRIVATE_FILES = ("keys.json", "config.json", "tvs.json", "scenes.json")


def write_private(path: Path, data: Any) -> None:
    """Atomically write JSON readable only by the owner (0600)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:  # mkstemp creates the file with mode 0600
            f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def tighten_permissions() -> None:
    """Make existing private files owner-only (e.g. after a manual copy)."""
    for name in PRIVATE_FILES:
        path = BASE_DIR / name
        if path.exists() and path.stat().st_mode & 0o077:
            path.chmod(0o600)


def load_keys() -> dict[str, str]:
    try:
        return json.loads(KEYS_FILE.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        # Never fall back to {}: the next save would wipe every other TV's key.
        raise RuntimeError(f"keys.json is corrupted ({exc}); fix or delete it") from exc


def save_key(tv_id: str, key: str | None) -> None:
    if not key:
        return
    keys = load_keys()
    if keys.get(tv_id) != key:
        keys[tv_id] = key
        write_private(KEYS_FILE, keys)


def load_config() -> dict[str, Any]:
    """Load config.json, generating a random 6-digit PIN on first run."""
    try:
        cfg = json.loads(CONFIG_FILE.read_text())
    except FileNotFoundError:
        cfg = {}
    except json.JSONDecodeError as exc:
        sys.exit(f"config.json is not valid JSON ({exc}); fix or delete it")
    if not cfg.get("pin"):
        cfg["pin"] = f"{secrets.randbelow(10**6):06d}"
        write_private(CONFIG_FILE, cfg)
    if not re.fullmatch(r"\d{6,}", str(cfg["pin"])):
        sys.exit("config.json: 'pin' must be at least 6 digits")
    return cfg


def magic_packet(mac: str) -> bytes:
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if len(raw) != 6:
        raise ValueError(f"invalid MAC: {mac}")
    return b"\xff" * 6 + raw * 16


def lan_ip() -> str | None:
    """Return this machine's LAN IPv4 address (UDP connect sends no packets)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


def wol_targets() -> list[str]:
    """Limited broadcast plus the subnet broadcast (config 'broadcast' or /24 guess)."""
    targets = ["255.255.255.255"]
    subnet = CONFIG.get("broadcast")
    if not subnet and (ip := lan_ip()):
        subnet = ip.rsplit(".", 1)[0] + ".255"
    if subnet:
        targets.append(subnet)
    return targets


def wake(macs: list[str]) -> None:
    """Send Wake-on-LAN magic packets (TV must have 'Turn on via Wi-Fi' enabled)."""
    if not macs:
        raise ValueError("no MAC addresses configured for this TV in tvs.json")
    targets = wol_targets()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for mac in macs:
            packet = magic_packet(mac)
            for target in targets:
                for _ in range(3):
                    sock.sendto(packet, (target, WOL_PORT))


def bounded_number(value: Any, low: float, high: float, name: str) -> float:
    """Parse a finite number within [low, high]; rejects NaN, Infinity and bools."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(f"{name} must be between {low:g} and {high:g}")
    return number


CONNECT_TIMEOUT = 8   # seconds to (re)connect before a request gives up
ACTION_TIMEOUT = 10   # seconds a single TV command may take
_BACKGROUND_TASKS: set[asyncio.Task] = set()  # strong refs so cleanup tasks aren't GC'd


def describe(exc: BaseException) -> str:
    """Human-readable error text; never empty (str(TimeoutError()) is '')."""
    if isinstance(exc, KeyError) and exc.args:
        return f"missing field: {exc.args[0]}"
    return str(exc) or type(exc).__name__


async def _dispose(client: WebOsClient) -> None:
    try:
        await client.disconnect()
    except Exception:  # noqa: BLE001
        pass


def dispose_later(client: WebOsClient) -> None:
    """Close a client without awaiting (safe to call while being cancelled)."""
    task = asyncio.ensure_future(_dispose(client))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


class TvConnection:
    """Keeps one TV client connected, reconnecting lazily on demand."""

    def __init__(self, tv_id: str, name: str, host: str, macs: list[str]) -> None:
        self.tv_id, self.name, self.host, self.macs = tv_id, name, host, macs
        self.client: WebOsClient | None = None
        self.lock = asyncio.Lock()
        self.last_error: str | None = None

    async def ensure(self) -> WebOsClient:
        async with self.lock:
            if self.client and self.client.is_connected():
                return self.client
            if self.client:
                await self._safe_disconnect()
            client = WebOsClient(
                self.host, client_key=load_keys().get(self.tv_id), connect_timeout=90
            )
            try:
                await client.connect()
                save_key(self.tv_id, client.client_key)
            except BaseException as exc:
                # Covers failures AND cancellation (wait_for timeouts): never leave a
                # half-open client behind, or each poll would leak a connection.
                dispose_later(client)
                self.last_error = (
                    "connection timed out"
                    if isinstance(exc, (asyncio.CancelledError, TimeoutError))
                    else f"{type(exc).__name__}: {describe(exc)}"
                )
                raise
            self.client, self.last_error = client, None
            return client

    async def connect(self) -> WebOsClient:
        timeout = asyncio.timeout(CONNECT_TIMEOUT)
        try:
            async with timeout:
                return await self.ensure()
        except TimeoutError:
            if not timeout.expired():  # a TimeoutError from inside the library
                raise
            raise TimeoutError(
                f"could not connect to {self.name} within {CONNECT_TIMEOUT}s"
            ) from None

    async def drop(self, client: WebOsClient) -> None:
        """Forget `client` so the next request reconnects.

        Only drops it if it is still the current client: a late timeout from an
        older request must not close a fresh connection opened in the meantime.
        """
        async with self.lock:
            if self.client is client:
                await self._safe_disconnect()

    async def _safe_disconnect(self) -> None:
        client, self.client = self.client, None
        if client:
            await _dispose(client)

    async def status(self) -> dict[str, Any]:
        base = {"id": self.tv_id, "name": self.name, "host": self.host}
        try:
            client = await self.connect()
        except Exception:  # noqa: BLE001
            return {**base, "online": False, "error": self.last_error or "unreachable"}
        state = client.tv_state
        apps = sorted(
            (
                {"id": a["id"], "title": a.get("title", a["id"]), "icon": a.get("icon")}
                for a in state.apps.values()
            ),
            key=lambda a: a["title"].lower(),
        )
        inputs = [
            {"id": i["id"], "label": i.get("label", i["id"])}
            for i in state.inputs.values()
        ]
        return {
            **base,
            "online": True,
            "is_on": state.is_on,
            "screen_on": state.is_screen_on,
            "current_app": state.current_app_id,
            "volume": state.volume,
            "muted": state.muted,
            "apps": apps,
            "inputs": inputs,
        }

    async def wait_online(self, timeout: float = 45) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                await asyncio.wait_for(self.ensure(), timeout=5)
                await asyncio.sleep(2)  # let webOS finish booting its launcher
                return
            except Exception:  # noqa: BLE001
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"{self.name} did not come online in {timeout:.0f}s "
                        "(is 'Turn on via Wi-Fi' enabled on the TV?)"
                    ) from None
                await asyncio.sleep(2)

    async def action(self, payload: dict[str, Any]) -> Any:
        act = payload.get("action")
        if act == "power_on":
            await asyncio.to_thread(wake, self.macs)
            return "magic packet sent"
        if act == "wait_online":
            await self.wait_online(bounded_number(payload.get("timeout", 45), 1, 120, "timeout"))
            return "online"
        client = await self.connect()
        try:
            return await asyncio.wait_for(self._command(client, act, payload), ACTION_TIMEOUT)
        except TimeoutError:
            # A TV that lost power can look connected until the socket times out:
            # drop it so the next request reconnects instead of hanging again.
            await self.drop(client)
            raise TimeoutError(
                f"{self.name} did not respond within {ACTION_TIMEOUT}s"
            ) from None

    async def _command(self, client: WebOsClient, act: Any, payload: dict[str, Any]) -> Any:
        simple = {
            "play": client.play,
            "pause": client.pause,
            "stop": client.stop,
            "volume_up": client.volume_up,
            "volume_down": client.volume_down,
            "power_off": client.power_off,
        }
        if act in simple:
            return await simple[act]()
        if act == "mute":
            return await client.set_mute(bool(payload.get("value", True)))
        if act == "set_volume":
            return await client.set_volume(int(bounded_number(payload.get("value"), 0, 100, "volume")))
        if act == "launch":
            return await client.launch_app(str(payload["app_id"]))
        if act == "input":
            return await client.set_input(str(payload["input_id"]))
        if act == "button":
            name = str(payload["name"]).upper()
            if name not in BUTTONS:
                raise ValueError(f"unsupported button: {name}")
            return await client.button(name)
        if act == "toast":
            message = str(payload.get("message", "")).strip()[:200]
            if not message:
                raise ValueError("empty message")
            return await client.request(
                "system.notifications/createToast", {"message": message}
            )
        raise ValueError(f"unknown action: {act}")


CONNECTIONS = {tv_id: TvConnection(tv_id, **cfg) for tv_id, cfg in TVS.items()}
tighten_permissions()
CONFIG = load_config()

# ---------------------------------------------------------------- security
# Local requests (this Mac) skip the PIN. Every mutating request must carry a
# custom header, which cross-site pages cannot send without a CORS preflight.
SESSION_COOKIE = "tvd_session"
SESSIONS: set[str] = set()
LOGIN_ATTEMPTS: dict[str, list[float]] = {}   # per client IP
GLOBAL_ATTEMPTS: list[float] = []              # all clients together
MAX_ATTEMPTS, GLOBAL_MAX_ATTEMPTS, LOCKOUT_SECONDS = 5, 20, 300
LOCAL_ADDRS = {"127.0.0.1", "::1"}
PROXY_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "X-Real-IP", "Forwarded")


def allowed_hosts() -> set[str]:
    hosts = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
    if LAN_MODE and (ip := lan_ip()):
        hosts.add(f"{ip}:{PORT}")
    return hosts


def is_local(request: web.Request) -> bool:
    # A request relayed by a local reverse proxy/tunnel also comes from loopback,
    # so it must not get the PIN exemption.
    if any(h in request.headers for h in PROXY_HEADERS):
        return False
    return request.remote in LOCAL_ADDRS


def is_authenticated(request: web.Request) -> bool:
    if is_local(request):
        return True
    token = request.cookies.get(SESSION_COOKIE, "").encode()
    return any(hmac.compare_digest(token, s.encode()) for s in SESSIONS)


def reserve_login_attempt(ip: str) -> bool:
    """Count the attempt BEFORE reading the body, so parallel requests can't race the limit."""
    now = time.monotonic()
    for key in [k for k, v in LOGIN_ATTEMPTS.items() if all(now - t >= LOCKOUT_SECONDS for t in v)]:
        del LOGIN_ATTEMPTS[key]
    GLOBAL_ATTEMPTS[:] = [t for t in GLOBAL_ATTEMPTS if now - t < LOCKOUT_SECONDS]
    recent = [t for t in LOGIN_ATTEMPTS.get(ip, []) if now - t < LOCKOUT_SECONDS]
    if len(recent) >= MAX_ATTEMPTS or len(GLOBAL_ATTEMPTS) >= GLOBAL_MAX_ATTEMPTS:
        return False
    LOGIN_ATTEMPTS[ip] = [*recent, now]
    GLOBAL_ATTEMPTS.append(now)
    return True


SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


async def add_security_headers(request: web.Request, response: web.StreamResponse) -> None:
    nonce = request.get("csp_nonce", "")
    script_src = f"'nonce-{nonce}'" if nonce else "'none'"
    response.headers.update(SECURITY_HEADERS)
    response.headers.setdefault(  # keep a stricter per-response policy (icons: sandbox)
        "Content-Security-Policy",
        f"default-src 'none'; script-src {script_src}; style-src 'unsafe-inline'; "
        "img-src 'self'; connect-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'",
    )


@web.middleware
async def security_middleware(request: web.Request, handler):
    if request.host not in allowed_hosts():  # blocks DNS rebinding
        return web.Response(status=421, text="Misdirected request")
    if request.path in ("/login",):
        return await handler(request)
    if not is_authenticated(request):
        if request.path.startswith("/api/"):
            return web.json_response({"ok": False, "error": "auth required"}, status=401)
        raise web.HTTPFound("/login")
    if request.method == "POST" and request.headers.get("X-Requested-With") != "tv-dashboard":
        return web.json_response({"ok": False, "error": "missing header"}, status=403)
    return await handler(request)


LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>TV Dashboard</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f1115;color:#e8eaed;font:16px -apple-system,sans-serif}form{display:flex;flex-direction:column;
gap:12px;padding:24px;width:min(320px,90vw)}input,button{font:inherit;padding:12px;border-radius:10px;
border:1px solid #2a2f37;background:#181b21;color:inherit;text-align:center;letter-spacing:.3em}
button{background:#2563eb;border-color:#2563eb;letter-spacing:normal}p{color:#f87171;margin:0;text-align:center}
</style></head><body><form method="post"><h1 style="margin:0;font-size:20px;text-align:center">TV Dashboard</h1>
<input name="pin" inputmode="numeric" autocomplete="one-time-code" maxlength="6" placeholder="PIN" autofocus>
<button>Enter</button>{error}</form></body></html>"""


async def login(request: web.Request) -> web.Response:
    if request.method == "GET":
        return web.Response(text=LOGIN_HTML.replace("{error}", ""), content_type="text/html")
    ip = request.remote or "?"
    if not reserve_login_attempt(ip):
        msg = "<p>Too many attempts. Try again in a few minutes.</p>"
        return web.Response(text=LOGIN_HTML.replace("{error}", msg), content_type="text/html", status=429)
    form = await request.post()
    pin = str(form.get("pin", "")).encode()
    if hmac.compare_digest(pin, str(CONFIG["pin"]).encode()):
        LOGIN_ATTEMPTS.pop(ip, None)
        token = secrets.token_urlsafe(32)
        SESSIONS.add(token)
        resp = web.HTTPFound("/")
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="Strict", max_age=30 * 86400)
        raise resp
    return web.Response(
        text=LOGIN_HTML.replace("{error}", "<p>Wrong PIN</p>"), content_type="text/html", status=401
    )


# ------------------------------------------------------------------ scenes
def load_scenes() -> list[dict[str, Any]]:
    try:
        return json.loads(SCENES_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        log.warning("cannot load scenes: %s", exc)
        return []


async def api_scenes(_: web.Request) -> web.Response:
    return web.json_response(
        [{"id": s["id"], "name": s["name"], "icon": s.get("icon", "")} for s in load_scenes()]
    )


async def api_run_scene(request: web.Request) -> web.Response:
    scene = next((s for s in load_scenes() if s["id"] == request.match_info["scene_id"]), None)
    if scene is None:
        return web.json_response({"ok": False, "error": "unknown scene"}, status=404)
    steps_log = []
    for i, step in enumerate(scene["steps"], 1):
        conn = CONNECTIONS.get(step.get("tv", ""))
        if conn is None:
            return web.json_response({"ok": False, "error": f"step {i}: unknown tv"}, status=400)
        payload = {k: v for k, v in step.items() if k not in ("do", "tv")}
        payload["action"] = step["do"]
        try:
            await conn.action(payload)
            steps_log.append(f"{step['do']} {conn.tv_id}: ok")
        except Exception as exc:  # noqa: BLE001
            log.warning("scene %s step %d failed: %s", scene["id"], i, describe(exc))
            # power_off on a TV that is already off is not a failure worth stopping for
            if step["do"] == "power_off":
                steps_log.append(f"power_off {conn.tv_id}: skipped ({describe(exc)})")
                continue
            return web.json_response(
                {"ok": False, "error": f"step {i} ({step['do']}): {describe(exc)}", "log": steps_log},
                status=400,
            )
    return web.json_response({"ok": True, "log": steps_log})


async def api_tvs(_: web.Request) -> web.Response:
    results = await asyncio.gather(*(c.status() for c in CONNECTIONS.values()))
    return web.json_response(results)


async def api_action(request: web.Request) -> web.Response:
    conn = CONNECTIONS.get(request.match_info["tv_id"])
    if conn is None:
        return web.json_response({"ok": False, "error": "unknown tv"}, status=404)
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
        result = await conn.action(payload)
        return web.json_response({"ok": True, "result": result}, dumps=_dumps)
    except Exception as exc:  # noqa: BLE001
        log.warning("action failed on %s: %s", conn.tv_id, describe(exc))
        return web.json_response({"ok": False, "error": describe(exc)}, status=400)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=str)


async def api_icon(request: web.Request) -> web.Response:
    """Proxy app icons: TVs serve them over HTTPS with a self-signed cert."""
    conn = CONNECTIONS.get(request.match_info["tv_id"])
    url = request.query.get("url", "")
    if conn is None or not url.startswith(f"https://{conn.host}:3001/resources/"):
        return web.Response(status=404)
    session: aiohttp.ClientSession = request.app["http"]
    try:
        async with session.get(
            url, ssl=False, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=5)
        ) as r:
            if r.status != 200:
                return web.Response(status=502)
            body = await r.content.read(MAX_ICON_BYTES + 1)
    except Exception:  # noqa: BLE001
        return web.Response(status=502)
    # TVs send icons as application/octet-stream, so trust the bytes, not the header.
    content_type = sniff_image(body)
    if len(body) > MAX_ICON_BYTES or content_type is None:
        return web.Response(status=502)
    return web.Response(
        body=body,
        content_type=content_type,
        headers={"Cache-Control": "max-age=86400", "Content-Security-Policy": "sandbox"},
    )


MAX_ICON_BYTES = 512 * 1024
IMAGE_SIGNATURES = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
}


def sniff_image(body: bytes) -> str | None:
    """Return a safe raster image type from magic bytes; never HTML or SVG."""
    for signature, content_type in IMAGE_SIGNATURES.items():
        if body.startswith(signature):
            return content_type
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    return None


async def index(request: web.Request) -> web.Response:
    nonce = secrets.token_urlsafe(16)
    request["csp_nonce"] = nonce
    html = INDEX_HTML.replace("<script>", f'<script nonce="{nonce}">', 1)
    return web.Response(text=html, content_type="text/html")


async def on_startup(app: web.Application) -> None:
    app["http"] = aiohttp.ClientSession()


async def on_shutdown(app: web.Application) -> None:
    await app["http"].close()
    for conn in CONNECTIONS.values():
        if conn.client:
            await conn._safe_disconnect()


def make_app() -> web.Application:
    app = web.Application(middlewares=[security_middleware])
    app.add_routes(
        [
            web.get("/", index),
            web.get("/login", login),
            web.post("/login", login),
            web.get("/api/scenes", api_scenes),
            web.post("/api/scenes/{scene_id}/run", api_run_scene),
            web.get("/api/tvs", api_tvs),
            web.post("/api/tvs/{tv_id}/action", api_action),
            web.get("/api/tvs/{tv_id}/icon", api_icon),
        ]
    )
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.on_response_prepare.append(add_security_headers)
    return app


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TV Dashboard</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1b1f24;--muted:#6b7280;--line:#e5e7eb;--accent:#2563eb;--ok:#16a34a;--bad:#dc2626;--btn:#f1f3f6}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b21;--fg:#e8eaed;--muted:#9aa0a6;--line:#2a2f37;--accent:#60a5fa;--ok:#4ade80;--bad:#f87171;--btn:#232831}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{padding:20px 16px 4px;max-width:1200px;margin:auto;display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
h1{font-size:22px;margin:0}
#updated{color:var(--muted);font-size:13px}
main{max-width:1200px;margin:auto;padding:12px 16px 40px;display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;display:flex;flex-direction:column;gap:14px}
.head{display:flex;justify-content:space-between;align-items:center;gap:8px}
.head h2{font-size:17px;margin:0}
.pill{font-size:12px;padding:3px 9px;border-radius:99px;border:1px solid var(--line);color:var(--muted)}
.pill.on{color:var(--ok);border-color:var(--ok)}.pill.off{color:var(--bad);border-color:var(--bad)}
.meta{color:var(--muted);font-size:13px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
button{background:var(--btn);color:var(--fg);border:1px solid var(--line);border-radius:10px;padding:9px 12px;font:inherit;cursor:pointer;min-width:44px}
button:hover{border-color:var(--accent)}button:active{transform:scale(.97)}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
button.danger{color:var(--bad)}
.dpad{display:grid;grid-template-columns:repeat(3,52px);gap:6px;justify-content:center}
.dpad button{height:44px;padding:0}
.section-title{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:6px}
.apps{display:grid;grid-template-columns:repeat(auto-fill,minmax(84px,1fr));gap:8px;max-height:300px;overflow:auto;padding-right:4px}
.app{display:flex;flex-direction:column;align-items:center;gap:4px;padding:8px 4px;font-size:12px;text-align:center}
.app img,.app .ph{width:40px;height:40px;border-radius:8px;object-fit:contain;background:var(--bg)}
.app .ph{display:flex;align-items:center;justify-content:center;font-weight:600}
.app.active{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.app span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%}
input[type=text],select{flex:1;min-width:0;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:10px;padding:9px 10px;font:inherit}
input[type=range]{flex:1}
#toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:var(--fg);color:var(--bg);padding:8px 14px;border-radius:10px;font-size:14px;opacity:0;transition:opacity .2s;pointer-events:none}
#toast.show{opacity:1}
.offline{color:var(--muted)}
.scenes{max-width:1200px;margin:auto;padding:8px 16px 0;display:flex;gap:8px;overflow-x:auto}
.scenes button{white-space:nowrap;padding:10px 14px;border-radius:12px}
.scenes button.running{opacity:.6;pointer-events:none}
</style>
</head>
<body>
<header><h1>TV Dashboard</h1><span id="updated">Loading…</span></header>
<nav id="scenes" class="scenes"></nav>
<main id="grid"></main>
<div id="toast"></div>
<script>
const grid = document.getElementById('grid');
let busy = new Set();

function flash(msg){const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove('show'),1800)}

async function post(url, body){
  const r = await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'tv-dashboard'},body:JSON.stringify(body||{})});
  if(r.status===401){location.href='/login';throw new Error('auth required')}
  return r.json();
}

async function act(tv, body){
  try{
    const j = await post(`/api/tvs/${tv}/action`, body);
    flash(j.ok ? (body.action==='power_on'?'Wake signal sent':'Done') : 'Error: '+j.error);
    setTimeout(refresh, body.action==='power_on'?8000:600);
  }catch(e){flash('Error: '+e.message)}
}

async function loadScenes(){
  try{
    const r = await fetch('/api/scenes'); if(r.status===401){location.href='/login';return}
    const scenes = await r.json();
    document.getElementById('scenes').innerHTML = scenes.map(s=>`<button data-scene="${esc(s.id)}">${esc(s.icon)} ${esc(s.name)}</button>`).join('');
  }catch(e){}
}

document.getElementById('scenes').addEventListener('click', async e=>{
  const b = e.target.closest('[data-scene]'); if(!b) return;
  b.classList.add('running'); flash('Running '+b.textContent.trim()+'…');
  try{
    const j = await post(`/api/scenes/${b.dataset.scene}/run`);
    flash(j.ok ? 'Scene done' : 'Error: '+j.error);
  }catch(e){flash('Error: '+e.message)}
  b.classList.remove('running'); refresh();
});

function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}

function render(tv){
  if(!tv.online){
    return `<section class="card"><div class="head"><h2>${esc(tv.name)}</h2><span class="pill off">Offline</span></div>
      <div class="meta offline">${esc(tv.host)} — ${esc(tv.error)}</div>
      <div class="meta">The TV is off or unreachable. Power on needs "Turn on via Wi-Fi" enabled on the TV.</div>
      <div class="row"><button class="primary" data-tv="${tv.id}" data-act="power_on">⏻ Power on</button></div></section>`;
  }
  const cur = tv.apps.find(a=>a.id===tv.current_app);
  const apps = tv.apps.map(a=>`<button class="app ${a.id===tv.current_app?'active':''}" data-tv="${tv.id}" data-launch="${esc(a.id)}" title="${esc(a.title)}">
      ${a.icon?`<img src="/api/tvs/${tv.id}/icon?url=${encodeURIComponent(a.icon)}" alt="" loading="lazy" data-fallback="${esc(a.title[0]||'?')}">`:`<div class="ph">${esc(a.title[0]||'?')}</div>`}
      <span>${esc(a.title)}</span></button>`).join('');
  const inputs = tv.inputs.map(i=>`<option value="${esc(i.id)}">${esc(i.label)}</option>`).join('');
  return `<section class="card" data-card="${tv.id}">
    <div class="head"><h2>${esc(tv.name)}</h2><span class="pill ${tv.is_on?'on':'off'}">${tv.is_on?(tv.screen_on?'On':'Screen off'):'Standby'}</span></div>
    <div class="meta">${esc(tv.host)} · Now: <b>${esc(cur?cur.title:(tv.current_app||'—'))}</b></div>

    <div><div class="section-title">Playback</div><div class="row">
      <button data-tv="${tv.id}" data-act="play">▶ Play</button>
      <button data-tv="${tv.id}" data-act="pause">⏸ Pause</button>
      <button data-tv="${tv.id}" data-act="stop">⏹ Stop</button>
    </div></div>

    <div><div class="section-title">Volume · ${tv.muted?'muted':(tv.volume??'—')}</div><div class="row">
      <button data-tv="${tv.id}" data-act="volume_down">−</button>
      <input type="range" min="0" max="50" value="${tv.volume??0}" data-tv="${tv.id}" data-vol>
      <button data-tv="${tv.id}" data-act="volume_up">+</button>
      <button data-tv="${tv.id}" data-mute="${tv.muted?0:1}">${tv.muted?'🔈 Unmute':'🔇 Mute'}</button>
    </div></div>

    <div><div class="section-title">Remote</div>
      <div class="dpad">
        <button data-tv="${tv.id}" data-btn="BACK">↩</button><button data-tv="${tv.id}" data-btn="UP">▲</button><button data-tv="${tv.id}" data-btn="HOME">⌂</button>
        <button data-tv="${tv.id}" data-btn="LEFT">◀</button><button class="primary" data-tv="${tv.id}" data-btn="ENTER">OK</button><button data-tv="${tv.id}" data-btn="RIGHT">▶</button>
        <span></span><button data-tv="${tv.id}" data-btn="DOWN">▼</button><span></span>
      </div></div>

    ${inputs?`<div><div class="section-title">Input</div><div class="row"><select data-input-sel="${tv.id}">${inputs}</select><button data-tv="${tv.id}" data-set-input>Switch</button></div></div>`:''}

    <div><div class="section-title">Apps (${tv.apps.length})</div><div class="apps">${apps}</div></div>

    <div><div class="section-title">On-screen message</div><div class="row">
      <input type="text" maxlength="200" placeholder="Message…" data-msg="${tv.id}">
      <button data-tv="${tv.id}" data-send>Send</button>
    </div></div>

    <div class="row"><button class="danger" data-tv="${tv.id}" data-act="power_off">⏻ Power off</button></div>
  </section>`;
}

async function refresh(){
  if(document.activeElement && document.activeElement.matches('input')) return;
  try{
    const r = await fetch('/api/tvs'); if(r.status===401){location.href='/login';return}
    const tvs = await r.json();
    grid.innerHTML = tvs.map(render).join('');
    document.getElementById('updated').textContent = 'Updated '+new Date().toLocaleTimeString();
  }catch(e){document.getElementById('updated').textContent='Server unreachable'}
}

grid.addEventListener('click', e=>{
  const b = e.target.closest('button'); if(!b) return;
  const tv = b.dataset.tv;
  if(b.dataset.act){
    if(b.dataset.act==='power_off' && !confirm('Power off this TV?')) return;
    return act(tv,{action:b.dataset.act});
  }
  if(b.dataset.btn) return act(tv,{action:'button',name:b.dataset.btn});
  if(b.dataset.launch) return act(tv,{action:'launch',app_id:b.dataset.launch});
  if(b.dataset.mute!==undefined) return act(tv,{action:'mute',value:b.dataset.mute==='1'});
  if(b.hasAttribute('data-set-input')){const s=document.querySelector(`[data-input-sel="${tv}"]`);return act(tv,{action:'input',input_id:s.value})}
  if(b.hasAttribute('data-send')){const i=document.querySelector(`[data-msg="${tv}"]`);if(!i.value.trim())return;act(tv,{action:'toast',message:i.value});i.value='';i.blur()}
});
// Broken icon -> letter placeholder. Delegated capture listener: no inline handlers (CSP).
grid.addEventListener('error', e=>{
  const img = e.target;
  if(img.tagName!=='IMG') return;
  const ph = document.createElement('div');
  ph.className = 'ph';
  ph.textContent = img.dataset.fallback || '?';
  img.replaceWith(ph);
}, true);
grid.addEventListener('change', e=>{
  if(e.target.matches('[data-vol]')){act(e.target.dataset.tv,{action:'set_volume',value:+e.target.value});e.target.blur()}
});
grid.addEventListener('keydown', e=>{
  if(e.key==='Enter' && e.target.matches('[data-msg]')){document.querySelector(`[data-tv="${e.target.dataset.msg}"][data-send]`).click()}
});

loadScenes(); refresh(); setInterval(refresh, 5000);
</script>
</body>
</html>
"""


# --------------------------------------------------------------------- CLI
def discover(duration: float = 6) -> list[dict[str, str]]:
    """Find LG webOS TVs on the LAN via SSDP and read their UPnP descriptors."""
    msg = (
        "M-SEARCH * HTTP/1.1\r\nHOST:239.255.255.250:1900\r\nMAN:\"ssdp:discover\"\r\n"
        "MX:2\r\nST:ssdp:all\r\n\r\n"
    ).encode()
    locations: dict[str, set[str]] = {}
    # SSDP is lossy UDP: listen for a fixed window and re-send the query periodically.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(0.5)
        deadline = time.monotonic() + duration
        next_send = 0.0
        while (now := time.monotonic()) < deadline:
            if now >= next_send:
                sock.sendto(msg, ("239.255.255.250", 1900))
                next_send = now + 1.5
            try:
                data, (ip, _) = sock.recvfrom(4096)
            except TimeoutError:
                continue
            text = data.decode(errors="ignore")
            server = re.search(r"(?im)^server:\s*(.*)$", text)
            if not server or "webos" not in server.group(1).lower():
                continue
            loc = re.search(r"(?im)^location:\s*(\S+)", text)
            if loc:
                locations.setdefault(ip, set()).add(loc.group(1))

    # SSDP replies are unauthenticated: only fetch plain-HTTP descriptors served by
    # the device that answered, never follow redirects, and cap the response size.
    opener = urllib.request.build_opener(_NoRedirect)
    tvs = []
    for ip, locs in sorted(locations.items(), key=lambda kv: tuple(map(int, kv[0].split(".")))):
        name = model = ""
        for loc in locs:
            if not _is_safe_location(loc, ip):
                continue
            try:
                with opener.open(loc, timeout=3) as r:  # noqa: S310 (validated above)
                    xml = r.read(MAX_DESCRIPTOR_BYTES).decode(errors="ignore")
            except (OSError, ValueError):
                continue
            name = name or (re.search(r"<friendlyName>([^<]*)", xml) or [None, ""])[1]
            m = re.search(r"<modelName>([^<]*)", xml)
            if m and m.group(1) not in ("LG TV", "LG Smart TV", "LG smart TV"):
                model = model or m.group(1)
        tvs.append({"host": ip, "name": _clean(name) or f"LG TV {ip}", "model": _clean(model)})
    return tvs


MAX_DESCRIPTOR_BYTES = 64 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


def _is_safe_location(loc: str, sender_ip: str) -> bool:
    try:
        parts = urlsplit(loc)
        return parts.scheme == "http" and parts.hostname == sender_ip and not parts.username
    except ValueError:
        return False


def _clean(text: str) -> str:
    """Strip control characters from device-supplied strings and cap their length."""
    return re.sub(r"[\x00-\x1f\x7f]", "", text).strip()[:80]


def update_tv_entry(tv_id: str, **fields: Any) -> None:
    tvs = json.loads(TVS_FILE.read_text())
    tvs[tv_id].update(fields)
    write_private(TVS_FILE, tvs)


async def pair(tv_id: str) -> None:
    """Pair once with a TV (accept the on-screen prompt) and record its MACs."""
    conn = CONNECTIONS.get(tv_id)
    if conn is None:
        sys.exit(f"Unknown TV '{tv_id}'. Known: {', '.join(CONNECTIONS)}")
    print(f"Connecting to {conn.name} ({conn.host}). Accept the prompt on the TV…")
    try:
        client = await conn.ensure()
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Pairing failed: {describe(exc)}")
    print(f"Paired. Key saved to {KEYS_FILE.name}.")
    try:
        info = await client.get_connection_info()
        macs = [
            info[k]["macAddress"].upper()
            for k in ("wiredInfo", "wifiInfo")
            if info.get(k, {}).get("macAddress")
        ]
        if macs:
            update_tv_entry(tv_id, macs=macs)
            print(f"MACs saved to {TVS_FILE.name}: {', '.join(macs)}")
    except Exception as exc:  # noqa: BLE001
        print(f"Could not read MACs ({exc}); add them to tvs.json manually.")
    await conn._safe_disconnect()


async def inspect(tv_id: str) -> None:
    """Print a TV's app ids and input ids as JSON (for writing scenes)."""
    conn = CONNECTIONS.get(tv_id)
    if conn is None:
        sys.exit(f"Unknown TV '{tv_id}'. Known: {', '.join(CONNECTIONS)}")
    status = await conn.status()
    await conn._safe_disconnect()
    if not status["online"]:
        sys.exit(f"{tv_id} is offline: {status['error']}")
    print(json.dumps({k: status[k] for k in ("id", "apps", "inputs")}, indent=2, ensure_ascii=False))


def cli_arg(flag: str) -> str:
    idx = sys.argv.index(flag)
    if idx + 1 >= len(sys.argv):
        sys.exit(f"Usage: uv run app.py {flag} <tv_id>")
    return sys.argv[idx + 1]


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    if "--discover" in sys.argv:
        print(json.dumps(discover(), indent=2, ensure_ascii=False))
        sys.exit(0)
    if "--pair" in sys.argv:
        asyncio.run(pair(cli_arg("--pair")))
        sys.exit(0)
    if "--inspect" in sys.argv:
        asyncio.run(inspect(cli_arg("--inspect")))
        sys.exit(0)
    logging.getLogger().setLevel(logging.INFO)
    if LAN_MODE:
        log.info("LAN mode: open http://%s:%d on your phone (PIN in config.json)", lan_ip(), PORT)
    web.run_app(make_app(), host=BIND_HOST, port=PORT)
