"""
PC Remote Control - WebRTC remote desktop server.

Streams the screen as a WebRTC video track and receives mouse/keyboard input
over a WebRTC data channel, so a browser on another network can view and
control this PC with low latency. WebRTC handles NAT traversal via STUN/TURN,
which makes "connect from anywhere" practical (see README for reachability of
the signaling endpoint).

Usage:
    pip install -r requirements.txt
    python server.py --host 0.0.0.0 --port 8000 --token mysecret

Signaling is plain HTTP (POST /offer). To reach it across the internet, expose
this port with a tunnel (cloudflared/ngrok) or port-forwarding; the media/input
themselves travel P2P (or via TURN), not through that tunnel.
"""

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from concurrent.futures import ThreadPoolExecutor

import mss
import numpy as np
import pyautogui
from aiohttp import web

try:
    import pyperclip

    # Probe once: on Linux, pyperclip needs a backend (xclip/xsel/wl-clipboard).
    pyperclip.paste()
    HAVE_CLIPBOARD = True
except Exception as _clip_err:  # noqa: BLE001
    pyperclip = None
    HAVE_CLIPBOARD = False
    print(f"[clipboard] disabled: {_clip_err}")
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
    VideoStreamTrack,
)
from av import VideoFrame

# For a remote-control tool, pyautogui's corner fail-safe is surprising.
pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0

SCREEN_W, SCREEN_H = pyautogui.size()

CONFIG = {
    "token": "",
    "max_width": 0,
    "stun": "",
    "turn_url": "",
    "turn_user": "",       # static credential mode
    "turn_pass": "",
    "turn_secret": "",     # time-limited credential mode (coturn use-auth-secret)
    "turn_ttl": 3600,
    "files_dir": "",       # shared folder: uploads land here, downloads served from here
}

# Single worker keeps input events strictly ordered (down -> move -> up)
# while running off the asyncio event loop so the video stream stays smooth.
INPUT_EXECUTOR = ThreadPoolExecutor(max_workers=1)

pcs = set()


# --------------------------------------------------------------------------- #
# Screen capture as a WebRTC video track
# --------------------------------------------------------------------------- #
class ScreenTrack(VideoStreamTrack):
    """A WebRTC video track that emits frames captured from the primary screen."""

    def __init__(self, max_width: int = 0):
        super().__init__()
        self._sct = mss.mss()
        self._monitor = self._sct.monitors[1]  # 0 is the "all monitors" virtual
        self._max_width = max_width

    async def recv(self) -> VideoFrame:
        # next_timestamp() paces the track to WebRTC's video clock (~30 fps)
        # and yields the pts / time_base the encoder expects.
        pts, time_base = await self.next_timestamp()

        raw = self._sct.grab(self._monitor)
        arr = np.asarray(raw)  # BGRA, shape (h, w, 4)

        frame = VideoFrame.from_ndarray(arr, format="bgra")
        if self._max_width and frame.width > self._max_width:
            new_h = int(frame.height * self._max_width / frame.width)
            frame = frame.reformat(width=self._max_width, height=new_h)

        frame.pts = pts
        frame.time_base = time_base
        return frame

    def stop(self):
        super().stop()
        try:
            self._sct.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Input handling (runs on INPUT_EXECUTOR)
# --------------------------------------------------------------------------- #
def to_screen_coords(x_norm: float, y_norm: float):
    x = min(max(float(x_norm), 0.0), 1.0) * SCREEN_W
    y = min(max(float(y_norm), 0.0), 1.0) * SCREEN_H
    return int(x), int(y)


def apply_input(data: dict):
    action = data.get("action")
    try:
        if action == "move":
            pyautogui.moveTo(*to_screen_coords(data["x"], data["y"]))
        elif action == "click":
            x, y = to_screen_coords(data["x"], data["y"])
            pyautogui.click(
                x=x, y=y,
                button=data.get("button", "left"),
                clicks=int(data.get("clicks", 1)),
            )
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
    except Exception as exc:  # noqa: BLE001 - never let bad input kill the loop
        print(f"[input] error handling {action}: {exc}")


def get_clipboard() -> str:
    """Return the host clipboard text (empty string if unavailable)."""
    if not HAVE_CLIPBOARD:
        return ""
    try:
        return pyperclip.paste() or ""
    except Exception as exc:  # noqa: BLE001
        print(f"[clipboard] read error: {exc}")
        return ""


def set_clipboard(text: str):
    """Write text to the host clipboard."""
    if not HAVE_CLIPBOARD:
        return
    try:
        pyperclip.copy(str(text))
    except Exception as exc:  # noqa: BLE001
        print(f"[clipboard] write error: {exc}")


# --------------------------------------------------------------------------- #
# File transfer (shared folder)
# --------------------------------------------------------------------------- #
FILE_CHUNK = 16 * 1024          # bytes per data-channel message
FILE_BUFFER_LIMIT = 8 * 1024 * 1024  # pause download when channel buffer exceeds this


def safe_name(name: str) -> str:
    """Reduce a client-supplied name to a bare, traversal-safe filename."""
    base = os.path.basename(str(name)).replace("\x00", "").strip()
    return base or "unnamed"


def is_within(directory: str, path: str) -> bool:
    """True when `path` resolves inside `directory` (defends against traversal)."""
    d = os.path.realpath(directory)
    p = os.path.realpath(path)
    return p == d or p.startswith(d + os.sep)


def unique_path(path: str) -> str:
    """Return `path`, or `name (n).ext` if it already exists, to avoid clobbering."""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{root} ({i}){ext}"):
        i += 1
    return f"{root} ({i}){ext}"


def list_files():
    """List regular files in the shared folder as [{name, size}, ...]."""
    d = CONFIG["files_dir"]
    out = []
    try:
        for n in sorted(os.listdir(d)):
            p = os.path.join(d, n)
            if os.path.isfile(p):
                out.append({"name": n, "size": os.path.getsize(p)})
    except Exception as exc:  # noqa: BLE001
        print(f"[files] list error: {exc}")
    return out


async def stream_download(channel, send_json, name: str):
    """Stream a shared-folder file to the browser as chunked binary messages."""
    path = os.path.join(CONFIG["files_dir"], safe_name(name))
    if not os.path.isfile(path) or not is_within(CONFIG["files_dir"], path):
        send_json({"type": "error", "message": "파일을 찾을 수 없습니다"})
        return

    size = os.path.getsize(path)
    send_json({"type": "download_begin", "name": os.path.basename(path), "size": size})
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(FILE_CHUNK)
                if not chunk:
                    break
                # Respect back-pressure so we don't blow up the send buffer.
                while (
                    channel.readyState == "open"
                    and channel.bufferedAmount > FILE_BUFFER_LIMIT
                ):
                    await asyncio.sleep(0.05)
                if channel.readyState != "open":
                    return
                channel.send(chunk)
        send_json({"type": "download_end", "name": os.path.basename(path)})
    except Exception as exc:  # noqa: BLE001
        send_json({"type": "error", "message": f"다운로드 오류: {exc}"})


# --------------------------------------------------------------------------- #
# ICE / TURN credentials
# --------------------------------------------------------------------------- #
def make_turn_credentials():
    """Mint a short-lived TURN username/password.

    Follows coturn's ``use-auth-secret`` (REST) convention:
        username = "<expiry-epoch>:<name>"
        password = base64( HMAC-SHA1(username, static_auth_secret) )
    The credential is valid until ``expiry`` and requires no per-user state on
    the TURN server.
    """
    expiry = int(time.time()) + int(CONFIG["turn_ttl"])
    username = f"{expiry}:webrtc"
    digest = hmac.new(
        CONFIG["turn_secret"].encode(), username.encode(), hashlib.sha1
    ).digest()
    password = base64.b64encode(digest).decode()
    return username, password


def build_ice(for_browser: bool):
    """Assemble the ICE server list, minting fresh TURN creds when a secret is set.

    Returns plain dicts for the browser (``/config``) or ``RTCIceServer``
    objects for the server-side peer connection (``/offer``).
    """
    servers = []

    def add(json_entry, **server_kwargs):
        servers.append(json_entry if for_browser else RTCIceServer(**server_kwargs))

    stun = CONFIG["stun"]
    if stun and stun.lower() != "none":
        add({"urls": [stun]}, urls=[stun])

    if CONFIG["turn_url"]:
        if CONFIG["turn_secret"]:
            user, pwd = make_turn_credentials()
        else:
            user, pwd = CONFIG["turn_user"], CONFIG["turn_pass"]
        if user and pwd:
            add(
                {"urls": [CONFIG["turn_url"]], "username": user, "credential": pwd},
                urls=[CONFIG["turn_url"]], username=user, credential=pwd,
            )

    return servers


# --------------------------------------------------------------------------- #
# HTTP: static files, config, WebRTC signaling
# --------------------------------------------------------------------------- #
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def token_ok(request) -> bool:
    supplied = request.query.get("token") or request.headers.get("X-Token")
    return bool(CONFIG["token"]) and secrets.compare_digest(
        str(supplied or ""), CONFIG["token"]
    )


async def index(request):
    return web.FileResponse(os.path.join(STATIC_DIR, "index.html"))


async def config(request):
    if not token_ok(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    return web.json_response(
        {
            "iceServers": build_ice(for_browser=True),
            "screen": {"w": SCREEN_W, "h": SCREEN_H},
            "clipboard": HAVE_CLIPBOARD,
            "files": True,
            # When TURN creds are time-limited, tell the client how long they last
            # so it can reconnect (re-fetch /config) before they expire.
            "turnTtl": CONFIG["turn_ttl"] if CONFIG["turn_secret"] else None,
        }
    )


async def offer(request):
    if not token_ok(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    params = await request.json()
    offer_desc = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    pc = RTCPeerConnection(
        configuration=RTCConfiguration(iceServers=build_ice(for_browser=False))
    )
    pcs.add(pc)
    loop = asyncio.get_event_loop()

    @pc.on("connectionstatechange")
    async def on_state():
        print(f"[webrtc] connection state: {pc.connectionState}")
        if pc.connectionState in ("failed", "closed", "disconnected"):
            await pc.close()
            pcs.discard(pc)

    @pc.on("datachannel")
    def on_datachannel(channel):
        # Per-channel state for an in-progress browser -> PC upload. Data-channel
        # messages are ordered, so a single "active upload" is sufficient: binary
        # messages between upload_begin and upload_end belong to that file.
        upload = {"f": None, "path": None, "received": 0}

        def send_json(obj):
            if channel.readyState == "open":
                channel.send(json.dumps(obj))

        def finish_upload():
            if upload["f"] is not None:
                try:
                    upload["f"].close()
                except Exception:  # noqa: BLE001
                    pass
                send_json({"type": "upload_done",
                           "name": os.path.basename(upload["path"] or ""),
                           "size": upload["received"]})
            upload["f"] = None
            upload["path"] = None
            upload["received"] = 0

        @channel.on("message")
        def on_message(message):
            # Binary payloads are file chunks for the active upload.
            if isinstance(message, (bytes, bytearray)):
                if upload["f"] is not None:
                    upload["f"].write(message)
                    upload["received"] += len(message)
                return

            try:
                data = json.loads(message)
            except (ValueError, TypeError):
                return
            action = data.get("action")

            if action == "clipboard_set":
                loop.run_in_executor(
                    INPUT_EXECUTOR, set_clipboard, data.get("text", "")
                )
            elif action == "clipboard_get":
                # Read the host clipboard off the loop, then reply on the channel.
                async def reply():
                    text = await loop.run_in_executor(INPUT_EXECUTOR, get_clipboard)
                    if channel.readyState == "open":
                        channel.send(
                            json.dumps({"type": "clipboard", "text": text})
                        )

                asyncio.ensure_future(reply())
            elif action == "upload_begin":
                finish_upload()  # close any dangling transfer first
                path = unique_path(
                    os.path.join(CONFIG["files_dir"], safe_name(data.get("name")))
                )
                try:
                    upload["f"] = open(path, "wb")
                    upload["path"] = path
                    upload["received"] = 0
                except Exception as exc:  # noqa: BLE001
                    send_json({"type": "error", "message": f"업로드 시작 실패: {exc}"})
            elif action == "upload_end":
                finish_upload()
            elif action == "file_list":
                send_json({"type": "file_list", "files": list_files()})
            elif action == "download":
                asyncio.ensure_future(
                    stream_download(channel, send_json, data.get("name", ""))
                )
            else:
                # Mouse/keyboard: run pyautogui off the event loop, in order.
                loop.run_in_executor(INPUT_EXECUTOR, apply_input, data)

    # The browser offers a recvonly video transceiver + a data channel;
    # we attach the screen track as the answer.
    pc.addTrack(ScreenTrack(max_width=CONFIG.get("max_width", 0)))

    await pc.setRemoteDescription(offer_desc)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return web.json_response(
        {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
    )


async def on_shutdown(app):
    await asyncio.gather(*[pc.close() for pc in list(pcs)], return_exceptions=True)
    pcs.clear()


# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description="WebRTC PC remote control")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--token",
        default=os.environ.get("REMOTE_TOKEN", ""),
        help="Access token. If omitted, a random one is generated and printed.",
    )
    parser.add_argument(
        "--max-width",
        type=int,
        default=0,
        help="Downscale frames wider than this many px (0 = full resolution).",
    )
    parser.add_argument(
        "--stun",
        default="",
        help="STUN URL (default Google STUN). Use 'none' to disable.",
    )
    parser.add_argument("--turn-url", default=os.environ.get("TURN_URL", ""))
    parser.add_argument(
        "--turn-user",
        default=os.environ.get("TURN_USER", ""),
        help="Static TURN username (ignored when --turn-secret is set).",
    )
    parser.add_argument(
        "--turn-pass",
        default=os.environ.get("TURN_PASS", ""),
        help="Static TURN password (ignored when --turn-secret is set).",
    )
    parser.add_argument(
        "--turn-secret",
        default=os.environ.get("TURN_SECRET", ""),
        help="coturn static-auth-secret. When set, the server mints "
        "time-limited TURN credentials automatically (REST convention).",
    )
    parser.add_argument(
        "--turn-ttl",
        type=int,
        default=int(os.environ.get("TURN_TTL", "3600")),
        help="Lifetime in seconds of minted TURN credentials (default 3600).",
    )
    parser.add_argument(
        "--files-dir",
        default=os.environ.get("FILES_DIR", "remote_files"),
        help="Shared folder: browser uploads land here and downloads are "
        "served from here (default ./remote_files).",
    )
    args = parser.parse_args()

    CONFIG["token"] = args.token or secrets.token_urlsafe(12)
    CONFIG["max_width"] = args.max_width
    CONFIG["stun"] = args.stun or "stun:stun.l.google.com:19302"
    CONFIG["turn_url"] = args.turn_url
    CONFIG["turn_user"] = args.turn_user
    CONFIG["turn_pass"] = args.turn_pass
    CONFIG["turn_secret"] = args.turn_secret
    CONFIG["turn_ttl"] = args.turn_ttl
    CONFIG["files_dir"] = os.path.abspath(args.files_dir)
    os.makedirs(CONFIG["files_dir"], exist_ok=True)

    app = web.Application()
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", index)
    app.router.add_get("/config", config)
    app.router.add_post("/offer", offer)
    app.router.add_static("/static/", STATIC_DIR)

    print("=" * 60)
    print("  PC Remote Control (WebRTC) server")
    print(f"  Screen      : {SCREEN_W} x {SCREEN_H}")
    print(f"  Listening   : http://{args.host}:{args.port}/")
    print(f"  Access token: {CONFIG['token']}")
    if args.turn_url and args.turn_secret:
        turn = f"yes (auto time-limited creds, ttl={args.turn_ttl}s)"
    elif args.turn_url:
        turn = "yes (static creds)"
    else:
        turn = "no (STUN only)"
    print(f"  TURN relay  : {turn}")
    print(f"  Clipboard   : {'enabled' if HAVE_CLIPBOARD else 'disabled'}")
    print(f"  Files dir   : {CONFIG['files_dir']}")
    print("=" * 60)

    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
