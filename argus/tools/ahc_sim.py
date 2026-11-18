"""AHC as a tool: run a healed PreCheck protocol on AHC's simulated STARlet.

AHC has no importer for PreCheck output, so each IR step becomes the AHC tool call that does the same thing:
  tips_on  -> pick_up_tips(tips=["<rack>:A<c>:H<c>"])      tips_off -> drop_tips()
  aspirate -> aspirate(targets=[...], volumes=v)            dispense -> dispense(targets=[...], volumes=v)
  empty / incubate / shake -> not liquid handling AHC does here; listed as skipped
Plate targets name the wells of the channels used (A-H of a column for 8 channels); a reservoir is one shared target.

AHC's server is created in-process (`create_server(folder, "sim", "hamilton.starlet")`) and driven through an
in-process MCP client, as AHC's own tests do. Its gates are kept: a plan first, plan_step on every call, the deck check
after load_layout. In simulation the deck check of a new layout is the person's: ARGUS records the scientist's review
approval (which showed them this layout) as that verdict. A refusal stops the run and is reported, never worked around.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import yaml
from mcp import Client

from argus.tools import lanm  # noqa: F401  (config.setup())

from ahc.server import create_server  # noqa: E402

ROWS = "ABCDEFGH"
PIPETTE = "starlet.pip"
Emit = Callable[[str, Dict[str, Any]], Awaitable[None]]


def _wells(labware: str, column: int, channels: List[int]) -> List[str]:
    """Wells of `column` for the channels used: one range when the rows are contiguous, else one well per row."""
    rows = [ROWS[c] for c in sorted(channels)]
    if len(rows) > 1 and "".join(rows) in ROWS:
        return [f"{labware}:{rows[0]}{column}:{rows[-1]}{column}"]
    return [f"{labware}:{r}{column}" for r in rows]


def ir_to_ahc_calls(ir: Dict[str, Any]) -> List[Dict[str, Any]]:
    kinds = {lid: spec.get("kind") for lid, spec in (ir.get("labware") or {}).items()}
    calls = []
    for s in ir.get("steps", []):
        action, lw, ch = s["action"], s.get("labware"), s.get("channels") or [0]
        base = {"ir_step": s["step"], "description": s.get("description", "")}
        if action == "tips_on":
            tips = _wells(lw, s["column"], ch) if s.get("column") else None
            calls.append({**base, "tool": "pick_up_tips", "args": {"device": PIPETTE, **({"tips": tips} if tips else {})}})
        elif action == "tips_off":
            calls.append({**base, "tool": "drop_tips", "args": {"device": PIPETTE}})
        elif action in ("aspirate", "dispense"):
            if kinds.get(lw) == "plate":
                targets = _wells(lw, s["column"], ch)
            else:
                targets = [lw]
            calls.append({**base, "tool": action,
                          "args": {"device": PIPETTE, "targets": targets, "volumes": float(s["volume_ul"])}})
        else:
            calls.append({**base, "tool": None, "skipped": f"{action}: not run on AHC in this simulation"})
    return calls


def expected_volumes(ir: Dict[str, Any]) -> Dict[str, float]:
    """PreCheck's arithmetic for every plate well at the end: {'plate:A1': uL}."""
    from pipeline.design_review import trace_mixtures

    kinds = {lid: spec.get("kind") for lid, spec in (ir.get("labware") or {}).items()}
    where, _ = trace_mixtures(ir, kinds)
    out = {}
    for key, mix in where.items():
        if isinstance(key, tuple) and key[1] is not None and key[2] < len(ROWS):
            out[f"{key[0]}:{ROWS[key[2]]}{key[1]}"] = round(sum(mix.values()), 3)
    return out


_VIEW = None  # one live 3D view for the ARGUS server: each simulation swaps its STARlet in, so one link stays valid


async def live_view():
    """AHC's LiveView (PyLabRobot Viewer3D), started once on free ports with no browser tab of its own: the
    Simulation tab embeds it. None when it cannot start (a missing dependency); the simulation runs without it."""
    global _VIEW
    if _VIEW is None:
        try:
            from ahc.viz.viewer import LiveView, free_port
            _VIEW = LiveView("ARGUS · AHC simulated STARlet", open_browser=False, port=free_port(),
                             ws_port=free_port())
        except Exception:  # noqa: BLE001
            return None
    return _VIEW


class Refused(Exception):
    def __init__(self, tool: str, args: Dict[str, Any], text: str):
        super().__init__(text)
        self.tool, self.call_args, self.text = tool, args, text


async def simulate(ir: Dict[str, Any], layout_file: Path, folder: Path, reviewer: str, approval_note: str,
                   emit: Optional[Emit] = None, view: Any = None, pace_s: float = 0.0) -> Dict[str, Any]:
    """`view`: a LiveView (see `live_view`) that AHC draws the deck in, live, from load_layout on; it outlives this
    simulation. `pace_s`: a pause after each liquid-handling call, so the view can be watched."""
    folder.mkdir(parents=True, exist_ok=True)
    layout = yaml.safe_load(Path(layout_file).read_text())
    calls = ir_to_ahc_calls(ir)
    expected = expected_volumes(ir)
    log: List[Dict[str, Any]] = []
    started = time.time()

    async def note(kind: str, data: Dict[str, Any]) -> None:
        if emit:
            await emit(kind, data)

    server = create_server(folder, "sim", "hamilton.starlet")
    if view is not None:
        server.lab.view = view  # AHC's load_layout puts its STARlet in this view and the twin's tracking drives it
    result: Dict[str, Any] = {"device": "hamilton.starlet (simulated)", "task_folder": str(folder),
                              "n_calls": sum(1 for c in calls if c["tool"]), "log": log, "refusal": None}
    async with Client(server) as client:
        async def call(tool: str, args: Dict[str, Any], ir_step: Optional[int] = None, desc: str = ""):
            r = await client.call_tool(tool, args)
            entry = {"tool": tool, "args": {k: v for k, v in args.items() if k != "layout"}, "ir_step": ir_step,
                     "description": desc, "ok": not r.is_error}
            if r.is_error:
                entry["error"] = r.content[0].text if r.content else "refused"
                log.append(entry)
                await note("ahc_call", entry)
                raise Refused(tool, args, entry["error"])
            log.append(entry)
            await note("ahc_call", entry)
            return r.structured_content or {}

        plan = ["Load PreCheck's deck layout and pass the deck check",
                f"Run the protocol's {result['n_calls']} liquid-handling calls",
                "Check final plate volumes against PreCheck's expected volumes"]
        try:
            await call("report_decision", {"decision": f"Run '{ir.get('protocol_name', 'protocol')}' from PreCheck in "
                                                       f"simulation, approved by {reviewer}", "plan": plan})
            await call("load_layout", {"layout": layout, "plan_step": 1})
            if view is not None and view.url:
                result["view_url"] = view.url
                await note("ahc_view", {"url": view.url})
                if pace_s > 0:  # give the page a moment to connect, so it shows the run from its first tip
                    await view.wait_for_browser(8)
            deck = await call("verify", {"check": "deck_matches_layout", "plan_step": 1})
            if deck.get("verdict") == "pending":
                result["deck_check"] = deck.get("ask_person")
                deck = await call("record_verdict", {
                    "check_id": deck["check_id"], "verdict": "pass",
                    "note": f"Simulation: {reviewer} reviewed this layout in ARGUS and approved it. {approval_note}".strip()})
            if deck.get("verdict") != "pass":
                raise Refused("verify", {"check": "deck_matches_layout"}, f"deck check: {deck.get('verdict')}")
            skipped = []
            for c in calls:
                if not c["tool"]:
                    skipped.append({"ir_step": c["ir_step"], "why": c["skipped"]})
                    continue
                await call(c["tool"], {**c["args"], "plan_step": 2}, c["ir_step"], c["description"])
                if view is not None and pace_s > 0:
                    await asyncio.sleep(pace_s)
            result["skipped"] = skipped
            state = await call("get_state", {})
            check = await call("verify", {"check": "volumes_match", "expected": expected, "tolerance_ul": 0.5,
                                          "plan_step": 3})
            result["volumes_match"] = {k: check.get(k) for k in ("verdict", "mismatches", "checked", "note", "check_id")
                                       if k in check}
            if check.get("verdict") == "pending":  # the person's to judge: say so, never answer for them
                result["volumes_match"]["ask_person"] = check.get("ask_person")
            result["final_liquids_ul"] = state.get("liquids_ul", {})
            result["tips_left"] = state.get("tips_left", {})
            await call("report_decision", {"decision": "Simulation finished", "plan_done": True})
        except Refused as e:
            result["refusal"] = {"tool": e.tool, "args": {k: v for k, v in e.call_args.items() if k != "layout"},
                                 "message": e.text}
        finally:
            server.lab.view = None  # AHC's shutdown stops its view; this one outlives the simulation (final deck stays)
    result["expected_ul"] = expected
    result["comparison"] = compare(expected, result.get("final_liquids_ul", {}))
    result["passed"] = (result["refusal"] is None and result["comparison"]["mismatches"] == 0
                        and result.get("volumes_match", {}).get("verdict") in ("pass", None))
    result["seconds"] = round(time.time() - started, 1)
    (folder / "argus_simulation.json").write_text(json.dumps(result, indent=2, default=str))
    return result


def compare(expected: Dict[str, float], liquids: Dict[str, Any], tol: float = 0.5) -> Dict[str, Any]:
    rows, bad = [], 0
    for target, exp in sorted(expected.items(), key=lambda kv: (kv[0].split(":")[0], int(kv[0].split(":")[1][1:]),
                                                               kv[0].split(":")[1][0])):
        plate, well = target.split(":")
        got = (liquids.get(plate) or {}).get(well)
        ok = got is not None and abs(float(got) - exp) <= tol
        bad += 0 if ok else 1
        rows.append({"well": target, "expected_ul": exp, "simulated_ul": got, "ok": ok})
    return {"wells": rows, "mismatches": bad, "checked": len(rows)}


async def dry_run(ir: Dict[str, Any], layout: Dict[str, Any]) -> Dict[str, Any]:
    """Run the whole protocol on AHC's simulator before anyone reviews it, in a throwaway task folder. The layout
    is saved there as a reference first, so the deck check is the simulator's own judgment (nobody's verdict is
    recorded). Stops at AHC's first refusal and returns it with the IR step it came from."""
    import re
    import tempfile

    calls = ir_to_ahc_calls(ir)
    with tempfile.TemporaryDirectory(prefix="argus-ahc-dryrun-") as folder:
        server = create_server(Path(folder), "sim", "hamilton.starlet")
        server.lab.ws.save_layout("reference", layout, "hamilton.starlet")
        async with Client(server) as client:
            async def call(tool, args):
                r = await client.call_tool(tool, args)
                if r.is_error:
                    text = r.content[0].text if r.content else "refused"
                    m = re.search(r"\[([a-z_]+)\]", text)
                    raise Refused(tool, {**args, "code": m.group(1) if m else None}, text)
                return r.structured_content or {}

            done = 0
            try:
                await call("report_decision", {"decision": "ARGUS dry run before review", "plan": [
                    "Load the deck", "Run every step"]})
                await call("load_layout", {"layout": layout, "plan_step": 1})
                deck = await call("verify", {"check": "deck_matches_layout", "plan_step": 1})
                if deck.get("verdict") != "pass":
                    return {"ok": False, "calls": done, "refusal": {
                        "tool": "verify", "code": "deck_check", "ir_step": None,
                        "message": f"AHC's deck check did not pass on its own: {deck.get('verdict')}"}}
                for c in calls:
                    if not c["tool"]:
                        continue
                    try:
                        await call(c["tool"], {**c["args"], "plan_step": 2})
                    except Refused as e:
                        return {"ok": False, "calls": done, "refusal": {
                            "tool": c["tool"], "code": e.call_args.get("code"), "ir_step": c["ir_step"],
                            "args": c["args"], "message": e.text}}
                    done += 1
            except Refused as e:
                return {"ok": False, "calls": done, "refusal": {"tool": e.tool, "code": e.call_args.get("code"),
                                                                "ir_step": None, "message": e.text}}
            return {"ok": True, "calls": done, "refusal": None}
