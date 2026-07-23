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
import json
import os
import secrets
from concurrent.futures import ThreadPoolExecutor

import mss
import numpy as np
import pyautogui
from aiohttp import web
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
    "ice_servers": [],   # list of RTCIceServer for the server side
    "ice_json": [],      # same, as plain dicts, for the browser
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
        {"iceServers": CONFIG["ice_json"], "screen": {"w": SCREEN_W, "h": SCREEN_H}}
    )


async def offer(request):
    if not token_ok(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    params = await request.json()
    offer_desc = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    pc = RTCPeerConnection(
        configuration=RTCConfiguration(iceServers=CONFIG["ice_servers"])
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
        @channel.on("message")
        def on_message(message):
            try:
                data = json.loads(message)
            except (ValueError, TypeError):
                return
            # Run pyautogui off the event loop, in order.
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
def build_ice(args):
    servers = []
    ice_json = []

    stun = args.stun or "stun:stun.l.google.com:19302"
    if stun.lower() != "none":
        servers.append(RTCIceServer(urls=[stun]))
        ice_json.append({"urls": [stun]})

    if args.turn_url:
        servers.append(
            RTCIceServer(
                urls=[args.turn_url],
                username=args.turn_user,
                credential=args.turn_pass,
            )
        )
        ice_json.append(
            {
                "urls": [args.turn_url],
                "username": args.turn_user,
                "credential": args.turn_pass,
            }
        )

    return servers, ice_json


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
    parser.add_argument("--turn-user", default=os.environ.get("TURN_USER", ""))
    parser.add_argument("--turn-pass", default=os.environ.get("TURN_PASS", ""))
    args = parser.parse_args()

    CONFIG["token"] = args.token or secrets.token_urlsafe(12)
    CONFIG["max_width"] = args.max_width
    CONFIG["ice_servers"], CONFIG["ice_json"] = build_ice(args)

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
    turn = "yes" if args.turn_url else "no (STUN only)"
    print(f"  TURN relay  : {turn}")
    print("=" * 60)

    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
