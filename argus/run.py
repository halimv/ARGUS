"""One ARGUS run: the scientist's prompt and SOP, what each tool produced, the review gate, and an event stream.

The tool implementations live here so the live agent (agent.py) and the offline script call exactly the same code.
Every tool emits tool_start / tool_end and a `panel` event with what the UI shows; the stream is append-only so a
page that reconnects replays it.
"""

import asyncio
import hashlib
import json
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from argus import config
from argus.tools import ahc_sim, lanm
from argus.tools import precheck as pc


def ir_hash(ir: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(ir, sort_keys=True).encode()).hexdigest()[:16]


def _labware_summary(lc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not lc:
        return None
    last = lc["rounds"][-1] if lc["rounds"] else {}
    return {"ahc_loads_deck_cleanly": lc["clean"], "rounds": len(lc["rounds"]),
            "actions": [a for r in lc["rounds"] for a in r["actions"]],
            "labware": [f"{r['role']}: {r['venus_type']} -> AHC {r['ahc_type'] or 'NOT LOADABLE'}"
                        for r in last.get("roles", [])]}


class ToolRefused(Exception):
    """A tool that will not run in the run's current state (said to the agent as an error result)."""


@dataclass
class ReviewGate:
    """Nothing reaches AHC unless the scientist approved this exact protocol (by its IR hash)."""
    state: str = "none"  # none | pending | approved | changes_requested
    ir_hash: Optional[str] = None
    approved_hash: Optional[str] = None
    reviewer: Optional[str] = None
    note: str = ""
    history: List[Dict[str, Any]] = field(default_factory=list)

    def request(self, h: str) -> None:
        self.state, self.ir_hash = "pending", h

    def decide(self, decision: str, reviewer: str, note: str) -> None:
        if self.state != "pending":
            raise ToolRefused(f"no review is pending (state: {self.state})")
        self.reviewer, self.note = reviewer, note
        self.history.append({"decision": decision, "reviewer": reviewer, "note": note, "ir_hash": self.ir_hash,
                             "at": datetime.now().isoformat(timespec="seconds")})
        if decision == "approve":
            self.state, self.approved_hash = "approved", self.ir_hash
        else:
            self.state = "changes_requested"

    def public(self) -> Dict[str, Any]:
        return {"state": self.state, "ir_hash": self.ir_hash, "approved": self.approved_hash is not None
                and self.approved_hash == self.ir_hash, "reviewer": self.reviewer, "note": self.note,
                "history": self.history}


class Run:
    def __init__(self, prompt: str, sop_text: str, sop_name: str = "input.txt", offline: bool = False):
        self.id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.prompt, self.sop_text, self.sop_name, self.offline = prompt, sop_text, sop_name, offline
        self.folder = config.RUNS / self.id
        self.folder.mkdir(parents=True, exist_ok=True)
        self.events: List[Dict[str, Any]] = []
        self.cond = asyncio.Condition()
        self.loop = asyncio.get_running_loop()
        self.state: Dict[str, Any] = {"status": "starting", "variants": None, "plate_map": None, "protocol": None,
                                      "simulation": None, "cost_usd": 0.0}
        self.review = ReviewGate()
        self.inbox: "asyncio.Queue[str]" = asyncio.Queue()
        self._pc: Optional[Dict[str, Any]] = None  # PreCheck's raw result (ir, report)
        self.busy = False

    # ------------------------------------------------------------------------------------------------ events
    async def emit(self, type_: str, data: Any = None) -> None:
        async with self.cond:
            self.events.append({"i": len(self.events), "t": time.time(), "type": type_, "data": data})
            self.cond.notify_all()

    def emit_threadsafe(self, type_: str, data: Any = None) -> None:
        asyncio.run_coroutine_threadsafe(self.emit(type_, data), self.loop)

    async def panel(self, key: str, value: Any) -> None:
        self.state[key] = value
        await self.emit("panel", {"key": key, "value": value})
        self.save()

    async def status(self, status: str) -> None:
        self.state["status"] = status
        await self.emit("status", {"status": status, "review": self.review.public()})
        self.save()

    def snapshot(self) -> Dict[str, Any]:
        return {"id": self.id, "prompt": self.prompt, "sop_text": self.sop_text, "sop_name": self.sop_name,
                "offline": self.offline, "state": self.state, "review": self.review.public(),
                "n_events": len(self.events)}

    def save(self) -> None:
        try:
            (self.folder / "run.json").write_text(json.dumps(self.snapshot(), indent=2, default=str))
        except OSError:
            pass

    async def _tool(self, name: str, args: Dict[str, Any], fn):
        tid = uuid.uuid4().hex[:8]
        started = time.time()
        await self.emit("tool_start", {"id": tid, "name": name, "input": args})
        try:
            out = await fn()
        except Exception as e:  # reported to the agent and the UI; the run goes on
            await self.emit("tool_end", {"id": tid, "name": name, "ok": False, "error": f"{type(e).__name__}: {e}",
                                         "seconds": round(time.time() - started, 1)})
            if not isinstance(e, (ToolRefused, ValueError)):
                await self.emit("progress", {"line": traceback.format_exc(limit=4)})
            raise
        await self.emit("tool_end", {"id": tid, "name": name, "ok": True, "seconds": round(time.time() - started, 1),
                                     "summary": out.get("summary") if isinstance(out, dict) else None})
        return out

    # ------------------------------------------------------------------------------------------------ tools
    async def rank_variants(self, target: str = "Nd", competitor: str = "La", top_n: int = 8) -> Dict[str, Any]:
        async def go():
            r = await asyncio.to_thread(lanm.rank_variants, target, competitor, top_n)
            await self.panel("variants", r)
            names = ", ".join(f"{v['rank']}. {v['name']} ({v['contrast']:.3f})" for v in r["variants"])
            return {**r, "summary": f"Top {len(r['variants'])} for {r['target']} over {r['competitor']}: {names}"}
        return await self._tool("rank_variants", {"target": target, "competitor": competitor, "top_n": top_n}, go)

    async def map_starting_concentrations(self, c_max_uM: float = 100.0, c_min_uM: float = 10.0,
                                          dilution_factor: float = 10.0, n_points: int = 6) -> Dict[str, Any]:
        args = {"c_max_uM": c_max_uM, "c_min_uM": c_min_uM, "dilution_factor": dilution_factor, "n_points": n_points}

        async def go():
            if not self.state.get("variants"):
                raise ToolRefused("rank_variants first: the TEA gate maps the ranked variants")
            pm = lanm.map_starting_concentrations(self.state["variants"]["variants"], **args)
            await self.panel("plate_map", pm)
            rows = "; ".join(f"{r['row']}: {r['name']} {r['start_uM']:.3g} µM" for r in pm["rows"])
            return {**pm, "summary": f"Starting concentrations — {rows}"}
        return await self._tool("map_starting_concentrations", args, go)

    async def translate_sop(self, extra_instructions: str = "") -> Dict[str, Any]:
        async def go():
            if self.review.state == "approved":
                raise ToolRefused("this run's protocol is already approved; use revise_protocol for a change")
            sop = self.sop_text + (f"\n\nAdditional instructions from the scientist: {extra_instructions}"
                                   if extra_instructions.strip() else "")
            stem = f"argus_{self.id.replace('-', '_')}"
            emit = lambda line: self.emit_threadsafe("progress", {"line": line})  # noqa: E731
            await self.status("translating")
            if self.offline:
                result = await asyncio.to_thread(pc.load_fixture_protocol, sop, stem, emit,
                                                 self.state.get("plate_map"))
            else:
                result = await asyncio.to_thread(pc.translate_sop, sop, self.state.get("plate_map"), stem, emit)
            return await self._take_protocol(result)
        return await self._tool("translate_sop", {"extra_instructions": extra_instructions}, go)

    async def revise_protocol(self, note: str) -> Dict[str, Any]:
        async def go():
            if not self._pc or self._pc.get("ir") is None:
                raise ToolRefused("there is no passing protocol to revise yet: translate_sop first")
            if self.offline:
                raise ToolRefused("revisions need Claude to redraft the protocol; this run is offline")
            emit = lambda line: self.emit_threadsafe("progress", {"line": line})  # noqa: E731
            await self.status("revising")
            result = await asyncio.to_thread(pc.revise_protocol, self._pc["ir"], self._pc["stem"], note, emit,
                                           self.state.get("plate_map"))
            return await self._take_protocol(result)
        return await self._tool("revise_protocol", {"note": note}, go)

    async def _take_protocol(self, result: Dict[str, Any]) -> Dict[str, Any]:
        if result.get("ir") is None:
            await self.panel("protocol", {"status": "NOT_READY", "failure": result.get("failure"),
                                          "tiers": result.get("tiers")})
            await self.status("not_ready")
            return {"status": "NOT_READY", "failure": result.get("failure"), "tiers": result.get("tiers"),
                    "summary": f"PreCheck could not make it pass the simulator: {result.get('failure')}"}
        self._pc = result
        packaged = pc.package(result, self.state.get("plate_map"), self.sop_text)
        packaged["ir_hash"] = ir_hash(result["ir"])
        if self.review.state != "none":
            self.review.state = "none"  # a new version needs its own review
        packaged["ahc_blocked"] = bool(result.get("ahc_blocked"))
        if packaged["ahc_blocked"]:
            packaged["failure"] = result.get("failure")
        await self.panel("protocol", packaged)
        await self.status("not_ready" if packaged["ahc_blocked"] else "protocol_ready")
        steps = [f"{s['n']}. {s['title']}" for s in packaged["readable_steps"]]
        must = [i for i in packaged["needs_review"] if i.get("severity") == "must"]
        return {
            "status": packaged["status"], "protocol_name": packaged["protocol_name"], "ir_steps": packaged["n_ir_steps"],
            "readable_steps": steps, "assumptions": packaged["assumptions"],
            "must_review": [f"{i['item']}: {i['detail']}" for i in must],
            "n_checks": len(packaged["needs_review"]), "repair_tiers": packaged["tiers"],
            "changes": packaged.get("changes"),
            "labware_check_with_ahc": _labware_summary(packaged.get("labware_check")),
            "deck_setup": packaged.get("deck_setup", []),
            "ahc_blocked": packaged["ahc_blocked"], "failure": packaged.get("failure"),
            "summary": ("NOT READY (AHC still refuses it): " + str(packaged.get("failure")))
                       if packaged["ahc_blocked"] else f"{packaged['status']}: {len(steps)} readable steps from {packaged['n_ir_steps']} IR steps"
                       + (f", {len(must)} must-review item(s)" if must else ""),
        }

    async def request_human_review(self, message: str = "") -> Dict[str, Any]:
        async def go():
            if not self._pc or self._pc.get("ir") is None:
                raise ToolRefused("there is no passing protocol to review: translate_sop first")
            if self._pc.get("ahc_blocked"):
                raise ToolRefused("AHC's simulator still refuses this protocol, so it is not ready for review: "
                                  + str(self._pc.get("failure")) + ". Ask the scientist what to change.")
            self.review.request(ir_hash(self._pc["ir"]))
            await self.emit("review_requested", {"message": message, "review": self.review.public()})
            await self.status("awaiting_review")
            return {"state": "pending", "summary": "Review requested. The scientist is reviewing the SOP side by side "
                                                   "with the readable steps. End your turn now and wait for their "
                                                   "decision; do not call run_ahc_simulation until they approve."}
        return await self._tool("request_human_review", {"message": message}, go)

    async def run_ahc_simulation(self) -> Dict[str, Any]:
        async def go():
            if not self._pc or self._pc.get("ir") is None:
                raise ToolRefused("there is no protocol to simulate")
            h = ir_hash(self._pc["ir"])
            if not (self.review.state == "approved" and self.review.approved_hash == h):
                raise ToolRefused("the scientist has not approved this version of the protocol; "
                                  "call request_human_review and wait for their decision")
            await self.status("simulating")

            async def on_call(kind, data):
                await self.emit(kind, data)

            view = await ahc_sim.live_view() if config.VIEW_3D else None
            sim = await ahc_sim.simulate(self._pc["ir"], pc.layout_path(self._pc), self.folder / "ahc",
                                         self.review.reviewer or "scientist", self.review.note, on_call,
                                         view=view, pace_s=config.SIM_PACE_S if view else 0.0)
            await self.panel("simulation", sim)
            await self.status("done" if sim["passed"] else "simulation_failed")
            comp = sim["comparison"]
            return {"passed": sim["passed"], "refusal": sim["refusal"], "calls": len(sim["log"]),
                    "wells_checked": comp["checked"], "well_mismatches": comp["mismatches"],
                    "volumes_match": sim.get("volumes_match"), "skipped": sim.get("skipped"),
                    "summary": ("PASSED" if sim["passed"] else "FAILED")
                               + f": {len(sim['log'])} AHC calls, {comp['checked'] - comp['mismatches']}/"
                                 f"{comp['checked']} wells match PreCheck"
                               + (f"; AHC refused {sim['refusal']['tool']}: {sim['refusal']['message'][:200]}"
                                  if sim["refusal"] else "")}
        return await self._tool("run_ahc_simulation", {}, go)

    # ------------------------------------------------------------------------------------------------ review
    async def decide(self, decision: str, reviewer: str, note: str) -> str:
        """The scientist's decision from the review panel; returns the message for the agent."""
        self.review.decide(decision, reviewer, note)
        await self.emit("review_decided", self.review.public())
        await self.status("approved" if decision == "approve" else "changes_requested")
        if decision == "approve":
            return (f"{reviewer} reviewed the protocol side by side with the SOP and APPROVED it"
                    + (f" (note: {note})" if note else "") + ". Run it on the AHC simulation now.")
        return (f"{reviewer} asked for changes before approving: \"{note}\". Call revise_protocol with this change, "
                "summarize what changed, then request_human_review again.")
