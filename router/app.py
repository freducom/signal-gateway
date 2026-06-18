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

routes_lock = threading.Lock()
routes: dict[str, str] = {}
own_device_id: int | None = None

app = Flask(__name__)


def load_routes():
    global routes
    if ROUTES_PATH.exists():
        try:
            with ROUTES_PATH.open() as f:
                routes = json.load(f)
        except json.JSONDecodeError:
            routes = {}
    else:
        routes = {}


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
    if not prefix or not webhook:
        abort(400, "prefix and webhook required")
    with routes_lock:
        routes[prefix] = webhook
        save_routes()
    return jsonify(ok=True, prefix=prefix, webhook=webhook)


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
        return jsonify(dict(routes))


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
        webhook = routes.get(prefix)

    if not webhook:
        send_reply(f"no handler for prefix '{prefix}'")
        return

    try:
        r = requests.post(webhook, json={"message": body}, timeout=30)
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
                handle_envelope(msg)
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
