import concurrent.futures
import json
import os
import threading
import time
from pathlib import Path

import requests
import websocket
from flask import Flask, abort, jsonify, request

SIGNAL_API_URL = os.environ["SIGNAL_API_URL"]
SIGNAL_API_WS_URL = os.environ["SIGNAL_API_WS_URL"]
SIGNAL_NUMBER = os.environ["SIGNAL_NUMBER"]
ROUTER_TOKEN = os.environ["ROUTER_TOKEN"]
DEVICE_NAME = os.environ.get("DEVICE_NAME", "signal-gateway")

ROUTES_PATH = Path("/data/routes.json")
ROUTES_PATH.parent.mkdir(parents=True, exist_ok=True)

DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 600

routes_lock = threading.Lock()
# Each route: {"webhook": str, "auth_header": {name, value} | None, "timeout_seconds": int}
routes: dict[str, dict] = {}
own_device_id: int | None = None

# Webhook dispatch runs in a thread pool so a slow handler doesn't block
# the websocket receive loop.
_dispatcher = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="dispatch")

app = Flask(__name__)


def load_routes():
    global routes
    if not ROUTES_PATH.exists():
        routes = {}
        return
    try:
        with ROUTES_PATH.open() as f:
            raw = json.load(f)
    except json.JSONDecodeError:
        routes = {}
        return
    out: dict[str, dict] = {}
    for prefix, value in raw.items():
        if isinstance(value, str):
            # Oldest format: bare URL string.
            out[prefix] = {"webhook": value, "auth_header": None, "timeout_seconds": DEFAULT_TIMEOUT_SECONDS}
        else:
            out[prefix] = {
                "webhook": value["webhook"],
                "auth_header": value.get("auth_header"),
                "timeout_seconds": value.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
            }
    routes = out


def save_routes():
    tmp = ROUTES_PATH.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(routes, f, indent=2)
    tmp.replace(ROUTES_PATH)


def auth_or_abort():
    if request.headers.get("X-Token") != ROUTER_TOKEN:
        abort(401)


@app.get("/health")
def health():
    with routes_lock:
        return {"ok": True, "device_id": own_device_id, "routes": len(routes)}


@app.post("/register")
def register():
    auth_or_abort()
    body = request.get_json(force=True, silent=True) or {}
    prefix = (body.get("prefix") or "").strip().lower()
    webhook = (body.get("webhook") or "").strip()
    auth_header = body.get("auth_header")
    raw_timeout = body.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if not prefix or not webhook:
        abort(400, "prefix and webhook required")
    if auth_header is not None:
        if (
            not isinstance(auth_header, dict)
            or not auth_header.get("name")
            or not auth_header.get("value")
        ):
            abort(400, "auth_header must be an object with 'name' and 'value'")
    try:
        timeout_seconds = int(raw_timeout)
    except (TypeError, ValueError):
        abort(400, "timeout_seconds must be an integer")
    if not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        abort(400, f"timeout_seconds must be between 1 and {MAX_TIMEOUT_SECONDS}")
    with routes_lock:
        routes[prefix] = {
            "webhook": webhook,
            "auth_header": auth_header,
            "timeout_seconds": timeout_seconds,
        }
        save_routes()
    return jsonify(
        ok=True,
        prefix=prefix,
        webhook=webhook,
        auth_header=bool(auth_header),
        timeout_seconds=timeout_seconds,
    )


@app.delete("/register/<prefix>")
def unregister(prefix):
    auth_or_abort()
    with routes_lock:
        existed = routes.pop(prefix, None) is not None
        save_routes()
    return jsonify(ok=True, removed=existed)


@app.get("/routes")
def list_routes():
    auth_or_abort()
    with routes_lock:
        return jsonify({
            prefix: {
                "webhook": route["webhook"],
                "has_auth_header": route.get("auth_header") is not None,
                "timeout_seconds": route.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
            }
            for prefix, route in routes.items()
        })


def discovery_loop():
    """Find our linked-device id by querying signal-api. Retries forever."""
    global own_device_id
    while own_device_id is None:
        try:
            r = requests.get(
                f"{SIGNAL_API_URL}/v1/devices/{SIGNAL_NUMBER}", timeout=5
            )
            if r.ok:
                for d in r.json():
                    if d.get("name") == DEVICE_NAME:
                        own_device_id = int(d["id"])
                        print(
                            f"[router] discovered own_device_id={own_device_id}",
                            flush=True,
                        )
                        return
        except requests.RequestException:
            pass
        time.sleep(15)


def send_reply(text: str):
    try:
        requests.post(
            f"{SIGNAL_API_URL}/v2/send",
            json={
                "message": text,
                "number": SIGNAL_NUMBER,
                "recipients": [SIGNAL_NUMBER],
            },
            timeout=30,
        )
    except requests.RequestException as e:
        print(f"[router] reply send failed: {e}", flush=True)


def handle_envelope(raw: dict):
    envelope = raw.get("envelope") or raw
    sync = envelope.get("syncMessage") or {}
    sent = sync.get("sentMessage") or {}
    if not sent:
        return

    # Only react to messages you sent to yourself (Note to Self).
    if sent.get("destination") != SIGNAL_NUMBER:
        return

    # Echo-loop filter: ignore messages this gateway itself sent.
    source_device = envelope.get("sourceDevice")
    if own_device_id is not None and source_device == own_device_id:
        return

    text = (sent.get("message") or "").strip()
    if not text:
        return

    parts = text.split(maxsplit=1)
    prefix = parts[0].lower()
    body = parts[1] if len(parts) > 1 else ""

    with routes_lock:
        route = routes.get(prefix)
        available = sorted(routes.keys())

    if not route:
        if available:
            listing = ", ".join(available)
            send_reply(f"no handler for prefix '{prefix}'. registered: {listing}")
        else:
            send_reply(f"no handler for prefix '{prefix}'. no handlers registered.")
        return

    headers = {}
    auth_header = route.get("auth_header")
    if auth_header:
        headers[auth_header["name"]] = auth_header["value"]

    timeout = route.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    try:
        r = requests.post(
            route["webhook"],
            json={"message": body},
            headers=headers,
            timeout=timeout,
        )
        reply = r.text.strip() or f"({r.status_code} no body)"
    except requests.RequestException as e:
        reply = f"webhook error: {e}"

    send_reply(reply)


def ws_loop():
    url = f"{SIGNAL_API_WS_URL}/v1/receive/{SIGNAL_NUMBER}"
    while True:
        try:
            print(f"[router] connecting to {url}", flush=True)
            ws = websocket.WebSocket()
            ws.connect(url)
            print("[router] websocket connected", flush=True)
            while True:
                raw = ws.recv()
                if not raw:
                    break
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                # Dispatch in the thread pool so a slow webhook never blocks
                # subsequent incoming messages.
                _dispatcher.submit(handle_envelope, msg)
        except Exception as e:
            print(f"[router] ws error: {e}", flush=True)
        time.sleep(5)


def start_background():
    load_routes()
    threading.Thread(target=discovery_loop, daemon=True).start()
    threading.Thread(target=ws_loop, daemon=True).start()


start_background()


if __name__ == "__main__":
    from waitress import serve

    serve(app, host="0.0.0.0", port=8091, threads=4)
