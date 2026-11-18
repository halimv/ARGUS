"""PyLabRobot's 3D viewer, served by ARGUS: live, and as a recording that plays back.

AHC draws its simulated STARlet in PyLabRobot's Viewer3D (through AHC's LiveView). ARGUS serves that viewer's own
page from its own origin, so it can:
  - point the page at the live viewer's websocket, or at a recording replayed by ARGUS;
  - add `web/viewer_embed.js`, which docks the head panels (96-head, channels, gripper) at the side of the deck
    instead of over it, and drives the viewer's built-in GIF recorder for a recorded run.

Recording: while a simulation runs, a websocket client listens to the live viewer like a browser would (scene, full
state, then every change) and keeps each message with its time. The recording, and the meshes its scene names, are
saved in the run's AHC folder; playback sends the same messages again, timed, at any speed.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from argus import config

WEB = Path(__file__).resolve().parent / "web"
RECORDING = "view_recording.json"
GIF = "run.gif"
MAX_GAP_S = 1.0  # longer pauses in a recording (waiting for a page to connect) play back as this long

_meshes: Dict[str, str] = {}  # mesh id -> file, for every viewer and recording seen by this server


def static_dir() -> Path:
    from pylabrobot.visualizer3D.server import STATIC_DIR
    return Path(STATIC_DIR)


def remember_meshes(view: Any) -> Dict[str, str]:
    viewer = getattr(view, "viewer", None)
    found = dict(getattr(viewer, "_mesh_files", {}) or {})
    _meshes.update(found)
    return found


def mesh_file(mesh_id: str) -> Optional[Path]:
    from argus.tools import ahc_sim

    if ahc_sim._VIEW is not None:  # the live viewer names meshes as it draws them
        remember_meshes(ahc_sim._VIEW)
    path = _meshes.get(mesh_id)
    if path is None:  # a recording from before this server started: its own map has it
        for rec in config.RUNS.glob(f"*/ahc/{RECORDING}"):
            try:
                _meshes.update(json.loads(rec.read_text()).get("meshes", {}))
            except (OSError, ValueError):
                continue
        path = _meshes.get(mesh_id)
    return Path(path) if path else None


def page(ws_url: str, title: str, mode: str, run_id: str = "", duration_s: float = 0.0, record: bool = False) -> str:
    """The viewer's index.html, pointed at `ws_url`, with ARGUS's embed script."""
    html = (static_dir() / "index.html").read_text(encoding="utf-8")
    html = html.replace("{{ ws_port }}", "0").replace("{{ source_filename }}", title)
    cfg = {"ws": ws_url, "mode": mode, "run": run_id, "duration_s": duration_s, "record": record}
    inject = (f"<script>window.ARGUS_VIEW = {json.dumps(cfg)};</script>\n"
              f'<script src="/static/viewer_embed.js"></script>\n')
    boot = '<script type="module" src="./boot.js"></script>'
    assert boot in html, "PyLabRobot's viewer page changed: no boot.js script to inject before"
    return html.replace(boot, inject + boot)


# ------------------------------------------------------------------------------------------------ recording
class Recorder:
    """Listens to a live viewer for one simulation and saves what it heard."""

    def __init__(self, view: Any, folder: Path):
        self.view, self.path = view, folder / RECORDING
        self.messages: List[List[Any]] = []
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._listen())
        try:
            await asyncio.wait_for(self._ready.wait(), 5)
        except asyncio.TimeoutError:
            pass

    async def _listen(self) -> None:
        import websockets

        viewer = self.view.viewer
        t0 = time.monotonic()
        try:
            async with websockets.connect(viewer.ws_url, max_size=None) as ws:
                self._ready.set()
                while not self._stop.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), 0.25)
                    except asyncio.TimeoutError:
                        continue
                    self.messages.append([round(time.monotonic() - t0, 3), msg])
        except Exception:  # noqa: BLE001 - a recording that failed never fails the simulation
            self._ready.set()

    async def stop(self, settle_s: float = 1.0) -> Optional[Path]:
        """Wait for the last changes to arrive, then save. Returns the file, or None with nothing recorded."""
        if self._task is None:
            return None
        await asyncio.sleep(settle_s)
        self._stop.set()
        await self._task
        if not self.messages:
            return None
        # Long idle stretches (the wait for a page) are shortened, so playback starts with the run.
        timeline, shift, last = [], 0.0, 0.0
        for t, msg in self.messages:
            gap = t - last
            if gap > MAX_GAP_S:
                shift += gap - MAX_GAP_S
            last = t
            timeline.append([round(t - shift, 3), msg])
        self.path.write_text(json.dumps({"duration_s": timeline[-1][0], "meshes": remember_meshes(self.view),
                                         "messages": timeline}))
        return self.path


def recording_path(run_id: str) -> Path:
    return config.RUNS / run_id / "ahc" / RECORDING


def recording_info(run_id: str) -> Optional[Dict[str, Any]]:
    path = recording_path(run_id)
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    gif = path.parent / GIF
    return {"duration_s": data["duration_s"], "messages": len(data["messages"]), "gif": gif.exists()}


async def replay(ws: Any, run_id: str, speed: float) -> None:
    """Send a recording to a viewer page, timed. The opening burst (the scene and its state) goes at once; the rest
    waits for the page's `argus_go` (sent once it has drawn the deck), so playback starts with the deck on screen.
    The socket stays open after the end: a page whose socket closes reconnects, which would start the replay again.
    `argus` messages mark the start and the end; the viewer itself ignores them."""
    data = json.loads(recording_path(run_id).read_text())
    _meshes.update(data.get("meshes", {}))
    speed = max(0.1, min(float(speed), 16.0))
    messages = data["messages"]
    burst = [m for m in messages if m[0] <= 0.05]
    rest = messages[len(burst):]
    go = asyncio.Event()

    async def listen():  # the page says hello, then argus_go; nothing else it says matters here
        try:
            while True:
                if "argus_go" in await ws.receive_text():
                    go.set()
        except Exception:  # noqa: BLE001
            go.set()

    reader = asyncio.create_task(listen())
    try:
        for _, msg in burst:
            await ws.send_text(msg)
        try:
            await asyncio.wait_for(go.wait(), 15)
        except asyncio.TimeoutError:
            pass
        t0 = rest[0][0] if rest else 0.0
        await ws.send_text(json.dumps({"event": "argus", "data": {
            "kind": "start", "duration_s": (data["duration_s"] - t0) / speed}}))
        start = time.monotonic()
        for t, msg in rest:
            wait = start + (t - t0) / speed - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            await ws.send_text(msg)
        await ws.send_text(json.dumps({"event": "argus", "data": {"kind": "end"}}))
        await reader
    except Exception:  # noqa: BLE001 - the page went away
        pass
    finally:
        reader.cancel()
