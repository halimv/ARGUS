"""The ARGUS web app: one page, one prompt, the agent's tool calls streamed live, review, simulation.

  POST /api/runs                      {prompt, sop_text, sop_name, offline} -> {id}
  GET  /api/runs/{id}                 snapshot (state of every panel, review gate)
  GET  /api/runs/{id}/events?after=n  server-sent events from event n on
  POST /api/runs/{id}/review          {decision: approve|revise, reviewer, note}
  GET  /api/defaults                  the default SOP (PreCheck/files/input.txt) and an example prompt
"""

import asyncio
import json
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from argus import config
from argus.agent import drive_live, drive_offline
from argus.run import Run, ToolRefused

WEB = Path(__file__).resolve().parent / "web"
EXAMPLE_PROMPT = ("Pick the top 8 LanM variants for Nd over La for our SpyCI-LAMBS assay, map their starting "
                  "concentrations, turn the attached serial dilution SOP into a Hamilton protocol, and run it in "
                  "simulation once I've reviewed it.")

app = FastAPI(title="ARGUS")
RUNS: Dict[str, Run] = {}
TASKS: Dict[str, asyncio.Task] = {}


class NewRun(BaseModel):
    prompt: str
    sop_text: str
    sop_name: str = "input.txt"
    offline: bool = False


class Decision(BaseModel):
    decision: str
    reviewer: str
    note: str = ""


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/api/defaults")
def defaults():
    return {"sop_text": config.DEFAULT_SOP.read_text(), "sop_name": config.DEFAULT_SOP.name,
            "prompt": EXAMPLE_PROMPT, "model": config.AGENT_MODEL}


async def _drive(run: Run) -> None:
    try:
        await (drive_offline(run) if run.offline else drive_live(run))
    except Exception as e:
        await run.emit("error", {"message": f"{type(e).__name__}: {e}"})
        await run.status("error")


@app.post("/api/runs")
async def new_run(body: NewRun):
    if not body.sop_text.strip():
        raise HTTPException(400, "The SOP is empty.")
    run = Run(body.prompt.strip() or EXAMPLE_PROMPT, body.sop_text, body.sop_name, body.offline)
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
