"""The ARGUS web app: one page, one prompt, the agent's tool calls streamed live, review, simulation.

  POST /api/runs                      {prompt, sop_text, sop_name, offline} -> {id}
  GET  /api/runs/{id}                 snapshot (state of every panel, review gate)
  GET  /api/runs/{id}/events?after=n  server-sent events from event n on
  POST /api/runs/{id}/review          {decision: approve|revise, reviewer, note}
  GET  /api/defaults                  the default SOP (PreCheck/files/input.txt) and an example prompt
  GET  /viewer/?live=1                PyLabRobot's 3D viewer on the live AHC simulation
  GET  /viewer/?run={id}&speed=s      the same viewer playing back a run's recording (&record=1: and make a GIF)
  GET  /api/runs/{id}/replay          the recording's length, and whether a GIF was made
  WS   /api/runs/{id}/replay/ws       the recording, timed, as the viewer's own messages
  GET/POST /api/runs/{id}/gif         the run's GIF (posted by the viewer page once it has rendered it)
"""

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from argus import config, viewer
from argus.agent import drive_live, drive_offline
from argus.run import Run, ToolRefused

WEB = Path(__file__).resolve().parent / "web"
EXAMPLE_PROMPT = ("Pick the top 8 LanM variants for Nd over La for our SpyCI-LAMBS assay, map their starting "
                  "concentrations, turn the attached serial dilution SOP into a Hamilton protocol, and run it in "
                  "simulation once I've reviewed it.")

@asynccontextmanager
async def lifespan(application):
    from argus.lanm_workspace import LanMWorkspace
    workspace = await asyncio.to_thread(LanMWorkspace().start)
    application.state.lanm_workspace = workspace
    try:
        yield
    finally:
        await asyncio.to_thread(workspace.close)


app = FastAPI(title="ARGUS", lifespan=lifespan)
RUNS: Dict[str, Run] = {}
TASKS: Dict[str, asyncio.Task] = {}


class NewRun(BaseModel):
    prompt: str
    sop_text: str
    sop_name: str = "input.txt"
    offline: bool = False
    project_id: Optional[str] = None


class Decision(BaseModel):
    decision: str
    reviewer: str
    note: str = ""


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/api/lanm-workspace")
def lanm_workspace(request: Request):
    workspace = getattr(request.app.state, "lanm_workspace", None)
    if workspace is None:
        raise HTTPException(503, "The LanM research workspace is not running.")
    return workspace.info()


@app.get("/api/lanm-projects")
def lanm_projects(request: Request):
    lanm_workspace(request)
    store = request.app.state.lanm_workspace.application.store
    return {"projects": [{key: row[key] for key in ("id", "name", "target", "competitor")}
                         for row in store.list_projects()]}


@app.get("/lanm")
def open_lanm_workspace(request: Request):
    return RedirectResponse(lanm_workspace(request)["url"])


# ------------------------------------------------------------------------------------------------ 3D viewer
@app.get("/viewer")
def viewer_root(request: Request):
    return RedirectResponse(f"/viewer/?{request.url.query}")


@app.get("/viewer/", response_class=HTMLResponse)
def viewer_page(run: str = "", speed: float = 1.0, live: int = 0, record: int = 0):
    if live or not run:
        from argus.tools import ahc_sim
        view = ahc_sim._VIEW
        if view is None or view.viewer is None:
            raise HTTPException(404, "No live 3D view yet: it starts with the first AHC simulation.")
        return viewer.page(view.viewer.ws_url, "ARGUS · AHC live", "live")
    info = viewer.recording_info(run)
    if info is None:
        raise HTTPException(404, "This run has no 3D recording.")
    return viewer.page(f"/api/runs/{run}/replay/ws?speed={speed:g}", f"ARGUS · replay {run}", "replay", run,
                       info["duration_s"] / max(speed, 0.1), bool(record))


@app.get("/viewer/mesh/{mesh_id}")
def viewer_mesh(mesh_id: str):
    path = viewer.mesh_file(mesh_id)
    if path is None or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="model/gltf-binary")


@app.get("/viewer/{path:path}")
def viewer_static(path: str):
    root = viewer.static_dir().resolve()
    target = (root / path).resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(404)
    return FileResponse(target, headers={"Cache-Control": "no-cache"})


@app.get("/api/runs/{run_id}/replay")
def replay_info(run_id: str):
    info = viewer.recording_info(run_id)
    if info is None:
        raise HTTPException(404, "This run has no 3D recording.")
    return info


@app.websocket("/api/runs/{run_id}/replay/ws")
async def replay_ws(ws: WebSocket, run_id: str, speed: float = 1.0):
    await ws.accept()
    if viewer.recording_info(run_id) is None:
        await ws.close(code=4404)
        return
    await viewer.replay(ws, run_id, speed)


@app.post("/api/runs/{run_id}/gif")
async def save_gif(run_id: str, request: Request):
    folder = viewer.recording_path(run_id).parent
    if not folder.exists():
        raise HTTPException(404, "No such run.")
    body = await request.body()
    if not body.startswith(b"GIF8"):
        raise HTTPException(400, "Not a GIF.")
    (folder / viewer.GIF).write_bytes(body)
    return {"saved": True, "bytes": len(body)}


@app.get("/api/runs/{run_id}/gif")
def get_gif(run_id: str):
    path = viewer.recording_path(run_id).parent / viewer.GIF
    if not path.exists():
        raise HTTPException(404, "No GIF for this run yet.")
    return FileResponse(path, media_type="image/gif", filename=f"argus-{run_id}.gif")


@app.get("/api/defaults")
def defaults():
    return {"sop_text": config.DEFAULT_SOP.read_text(), "sop_name": config.DEFAULT_SOP.name,
            "prompt": EXAMPLE_PROMPT, "model": config.AGENT_MODEL, "protocol_setup": protocol_setup()}


def protocol_setup():
    from pipeline import paths
    library = paths.labware()
    present = library.is_dir() and any(library.rglob("*.rck"))
    return {"available": present,
            "note": ("VENUS labware found; protocol-specific checks still run before review." if present else
                     "Protocol workflow unavailable: configure the VENUS labware library first. "
                     "The research workspace does not require VENUS.")}


async def _drive(run: Run) -> None:
    try:
        await (drive_offline(run) if run.offline else drive_live(run))
    except Exception as e:
        await run.emit("error", {"message": f"{type(e).__name__}: {e}"})
        await run.status("error")


@app.post("/api/runs")
async def new_run(body: NewRun):
    setup = protocol_setup()
    if not setup["available"]:
        raise HTTPException(503, setup["note"])
    if not body.sop_text.strip():
        raise HTTPException(400, "The SOP is empty.")
    try:
        run = Run(body.prompt.strip() or EXAMPLE_PROMPT, body.sop_text, body.sop_name, body.offline,
                  project_id=body.project_id)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    RUNS[run.id] = run
    TASKS[run.id] = asyncio.create_task(_drive(run))
    return {"id": run.id}


def _run(run_id: str) -> Run:
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "No such run (runs live in memory; it ended when the server restarted).")
    return run


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    return _run(run_id).snapshot()


@app.get("/api/runs/{run_id}/events")
async def events(run_id: str, after: int = -1):
    run = _run(run_id)

    async def stream():
        i = after + 1
        while True:
            async with run.cond:
                if i >= len(run.events):
                    try:
                        await asyncio.wait_for(run.cond.wait(), timeout=15)
                    except asyncio.TimeoutError:
                        pass
                batch = run.events[i:]
            if not batch:
                yield ": keep-alive\n\n"
                continue
            for e in batch:
                yield f"id: {e['i']}\ndata: {json.dumps(e, default=str)}\n\n"
            i += len(batch)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/runs/{run_id}/review")
async def review(run_id: str, body: Decision):
    run = _run(run_id)
    if body.decision not in ("approve", "revise"):
        raise HTTPException(400, "decision is approve or revise")
    if not body.reviewer.strip():
        raise HTTPException(400, "Name the reviewer.")
    if body.decision == "revise" and not body.note.strip():
        raise HTTPException(400, "Say what to change.")
    try:
        message = await run.decide(body.decision, body.reviewer.strip(), body.note.strip())
    except ToolRefused as e:
        raise HTTPException(409, str(e))
    await run.inbox.put(message)
    return run.snapshot()


def serve(host: str = "127.0.0.1", port: int = 8780, open_browser: bool = True) -> None:
    import threading
    import webbrowser

    import uvicorn

    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(f"http://{host}:{port}/")).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")


def main(argv: Optional[list] = None) -> None:  # pragma: no cover
    serve()
