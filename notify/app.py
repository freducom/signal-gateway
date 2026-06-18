import os

import requests
from flask import Flask, abort, jsonify, request

SIGNAL_API_URL = os.environ["SIGNAL_API_URL"]
SIGNAL_NUMBER = os.environ["SIGNAL_NUMBER"]
NOTIFY_TOKEN = os.environ["NOTIFY_TOKEN"]

app = Flask(__name__)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/notify")
def notify():
    if request.headers.get("X-Token") != NOTIFY_TOKEN:
        abort(401)

    text = request.get_data(as_text=True).strip()
    if not text:
        abort(400, "empty message")

    resp = requests.post(
        f"{SIGNAL_API_URL}/v2/send",
        json={
            "message": text,
            "number": SIGNAL_NUMBER,
            "recipients": [SIGNAL_NUMBER],
        },
        timeout=30,
    )
    if not resp.ok:
        return jsonify(error=resp.text), 502
    return jsonify(ok=True)


if __name__ == "__main__":
    from waitress import serve

    serve(app, host="0.0.0.0", port=8090, threads=4)
