"""
PC Remote Control - web-based remote desktop server.

Serves a live screen stream (MJPEG) and accepts mouse/keyboard input from a
browser, so any device on the network can view and control this PC without
installing a dedicated client.

Usage:
    pip install -r requirements.txt
    python server.py --host 0.0.0.0 --port 8000 --token mysecret

Then open http://<this-pc-ip>:8000/ from another device and enter the token.
"""

import argparse
import io
import os
import secrets
import time
from functools import wraps

import mss
import pyautogui
from PIL import Image
from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    request,
    send_from_directory,
)

# pyautogui's fail-safe aborts input when the cursor hits a screen corner.
# For a remote-control tool that behaviour is surprising, so we disable it.
pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0

app = Flask(__name__, static_folder="static", static_url_path="")

# Populated in main() from CLI args / environment.
CONFIG = {
    "token": "",
    "jpeg_quality": 60,
    "fps": 15,
    "max_width": 1280,
}

SCREEN_W, SCREEN_H = pyautogui.size()


def check_token() -> bool:
    """Return True when the request carries the correct access token."""
    supplied = (
        request.args.get("token")
        or request.headers.get("X-Token")
        or (request.json.get("token") if request.is_json else None)
    )
    return bool(CONFIG["token"]) and secrets.compare_digest(
        str(supplied or ""), CONFIG["token"]
    )


def require_token(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not check_token():
            abort(401)
        return fn(*args, **kwargs)

    return wrapper


def grab_frame() -> bytes:
    """Capture the primary monitor and return a JPEG-encoded frame."""
    with mss.mss() as sct:
        monitor = sct.monitors[1]  # index 0 is the virtual "all monitors"
        raw = sct.grab(monitor)
        img = Image.frombytes("RGB", raw.size, raw.rgb)

    if img.width > CONFIG["max_width"]:
        ratio = CONFIG["max_width"] / img.width
        img = img.resize(
            (CONFIG["max_width"], int(img.height * ratio)),
            Image.BILINEAR,
        )

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=CONFIG["jpeg_quality"])
    return buf.getvalue()


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/info")
@require_token
def info():
    return jsonify({"width": SCREEN_W, "height": SCREEN_H})


@app.route("/stream")
@require_token
def stream():
    """MJPEG stream of the screen, consumable by a plain <img> tag."""
    frame_interval = 1.0 / max(1, CONFIG["fps"])

    def generate():
        while True:
            start = time.time()
            try:
                frame = grab_frame()
            except Exception:
                # A transient capture failure shouldn't kill the stream.
                time.sleep(frame_interval)
                continue
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            )
            elapsed = time.time() - start
            if elapsed < frame_interval:
                time.sleep(frame_interval - elapsed)

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


def to_screen_coords(x_norm: float, y_norm: float):
    """Map normalized [0,1] client coordinates to absolute screen pixels."""
    x = min(max(x_norm, 0.0), 1.0) * SCREEN_W
    y = min(max(y_norm, 0.0), 1.0) * SCREEN_H
    return int(x), int(y)


@app.route("/input", methods=["POST"])
@require_token
def handle_input():
    """Apply a single mouse/keyboard action described by the JSON body."""
    data = request.get_json(silent=True) or {}
    action = data.get("action")

    try:
        if action == "move":
            x, y = to_screen_coords(data["x"], data["y"])
            pyautogui.moveTo(x, y)
        elif action == "click":
            x, y = to_screen_coords(data["x"], data["y"])
            button = data.get("button", "left")
            clicks = int(data.get("clicks", 1))
            pyautogui.click(x=x, y=y, button=button, clicks=clicks)
        elif action == "mousedown":
            x, y = to_screen_coords(data["x"], data["y"])
            pyautogui.mouseDown(x=x, y=y, button=data.get("button", "left"))
        elif action == "mouseup":
            x, y = to_screen_coords(data["x"], data["y"])
            pyautogui.mouseUp(x=x, y=y, button=data.get("button", "left"))
        elif action == "scroll":
            pyautogui.scroll(int(data.get("dy", 0)))
        elif action == "type":
            pyautogui.typewrite(str(data.get("text", "")), interval=0)
        elif action == "key":
            keys = data.get("keys")
            if isinstance(keys, list) and keys:
                pyautogui.hotkey(*keys)
            else:
                pyautogui.press(str(data.get("key", "")))
        else:
            return jsonify({"error": f"unknown action: {action}"}), 400
    except Exception as exc:  # noqa: BLE001 - report back to the client
        return jsonify({"error": str(exc)}), 500

    return jsonify({"ok": True})


def main():
    parser = argparse.ArgumentParser(description="Web-based PC remote control")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--token",
        default=os.environ.get("REMOTE_TOKEN", ""),
        help="Access token. If omitted, a random one is generated and printed.",
    )
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--quality", type=int, default=60, help="JPEG quality 1-95")
    parser.add_argument("--max-width", type=int, default=1280)
    args = parser.parse_args()

    CONFIG["token"] = args.token or secrets.token_urlsafe(12)
    CONFIG["fps"] = args.fps
    CONFIG["jpeg_quality"] = args.quality
    CONFIG["max_width"] = args.max_width

    print("=" * 56)
    print("  PC Remote Control server")
    print(f"  Screen      : {SCREEN_W} x {SCREEN_H}")
    print(f"  Listening   : http://{args.host}:{args.port}/")
    print(f"  Access token: {CONFIG['token']}")
    print("=" * 56)

    # threaded=True lets the MJPEG stream and input requests run concurrently.
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
