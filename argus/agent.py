"""The ARGUS agent: Claude (through the Claude Agent SDK and the scientist's `claude` CLI login) with one in-process
MCP server, `argus`, whose tools are the three systems. No built-in tools (no shell, no file edits), no user
settings or plugins: the agent can only do what these tools do.

`drive_offline` runs the same tools in the same order without a model, for a deterministic demo and the tests.
"""

import json
import re
from typing import Any, Dict

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, TextBlock,
                              create_sdk_mcp_server, tool)

from argus import config
from argus.run import Run

SYSTEM_PROMPT = """You are ARGUS, the automation assistant for the LLNL team running the SpyCI-LAMBS assay \
(SpyCatcher-immobilized lanmodulin (LanM) variants, screened for rare-earth selectivity). A scientist gives you a \
request and an SOP. You turn it into a reviewed protocol that runs on a simulated Hamilton STARlet, using only the \
argus tools, in this order:

1. rank_variants: the top LanM variants from the LanM research platform (target metal over competitor; default Nd \
over La, 8 variants, one per plate row, unless the scientist says otherwise).
2. map_starting_concentrations: assay concentration mapping. Each variant gets a plate row and a starting concentration scaled by \
its published screening contrast (defaults 100 uM to 10 uM, 1:10 steps, 6 points, unless the scientist says otherwise).
3. translate_sop: PreCheck turns the uploaded SOP (plus the plate map) into a Hamilton protocol, repairs it until it \
passes the instrument's simulator, and guards the experimental intent. Then AHC checks the labware: its simulator \
loads PreCheck's deck, and anything AHC cannot load or that would run too shallow goes back to PreCheck to fix. Tell \
the scientist what that check changed (labware_check_with_ahc). It can take several minutes.
4. request_human_review: the scientist reviews the SOP side by side with the readable steps. Then END YOUR TURN and \
wait. Never approve on their behalf and never assume approval.
5. When the scientist approves: run_ahc_simulation. If they ask for changes: revise_protocol with their words, \
then request_human_review again.

Rules:
- Between tool calls, write one or two plain sentences to the scientist about what you found. No markdown tables.
- Report what PreCheck flagged as must-review, and any AHC refusal, in the tool's own terms. A refusal is a finding: \
do not try to work around it.
- Do not invent numbers: use only what the tools return. The assay concentrations are a heuristic, so say so once.
- Screening contrast is not a separation factor or a demonstrated dose requirement. Assay concentration mapping \
does not evaluate process costs. The full LanM research workspace provides separate economic models, \
Research/Build chat and registered methods; this protocol assistant uses the narrower automation tools.
- If this run names a LanM project, use its declared target/competitor pair. Never change its inputs to fit a \
contradictory request; explain the mismatch and ask for a new run or corrected request.
- Once a protocol exists, changing the ranked variants or assay map requires a new run.
- Everything here is simulation. Never suggest that anything ran on a real instrument.
- If translate_sop returns NOT_READY, explain the failure and ask the scientist what to change."""


def _text(data: Any, is_error: bool = False) -> Dict[str, Any]:
    out = {"content": [{"type": "text", "text": data if isinstance(data, str) else json.dumps(data, default=str)}]}
    if is_error:
        out["is_error"] = True
    return out


def build_server(run: Run):
    async def call(fn, **kw):
        try:
            return _text(await fn(**kw))
        except Exception as e:  # the agent sees the refusal and the UI already showed it
            return _text(f"{type(e).__name__}: {e}", is_error=True)

    @tool("rank_variants", "Rank LanM variants by published screening contrast of target over competitor metal "
          "(LanM research platform). Returns the top variants.",
          {"type": "object", "properties": {
              "target": {"type": "string", "description": "Target rare-earth element symbol, e.g. Nd"},
              "competitor": {"type": "string", "description": "Competitor element symbol, e.g. La"},
              "top_n": {"type": "integer", "description": "How many variants (1-8, one per plate row)"}},
           "required": ["target", "competitor", "top_n"]})
    async def rank_variants(args):
        return await call(run.rank_variants, target=args["target"], competitor=args["competitor"],
                          top_n=int(args["top_n"]))

    @tool("map_starting_concentrations", "Assay concentration mapping: give each ranked variant a plate row and a starting "
          "concentration scaled by its contrast (higher contrast -> lower start), log-interpolated between c_max and "
          "c_min, with a serial dilution across columns.",
          {"type": "object", "properties": {
              "c_max_uM": {"type": "number", "exclusiveMinimum": 0},
              "c_min_uM": {"type": "number", "exclusiveMinimum": 0},
              "dilution_factor": {"type": "number", "exclusiveMinimum": 1},
              "n_points": {"type": "integer", "minimum": 1, "maximum": 12}},
           "required": []})
    async def map_starting_concentrations(args):
        kw = {k: args[k] for k in ("c_max_uM", "c_min_uM", "dilution_factor") if args.get(k) is not None}
        if args.get("n_points") is not None:
            kw["n_points"] = args["n_points"]
        return await call(run.map_starting_concentrations, **kw)

    @tool("translate_sop", "PreCheck: translate the uploaded SOP (with the plate map as assay context) into a Hamilton "
          "STARlet protocol, self-heal it against the instrument's simulator with the intent guard, and return "
          "readable steps. Slow (minutes).",
          {"type": "object", "properties": {
              "extra_instructions": {"type": "string",
                                     "description": "Only protocol details the scientist stated in their request; "
                                                    "empty otherwise"}},
           "required": []})
    async def translate_sop(args):
        return await call(run.translate_sop, extra_instructions=args.get("extra_instructions") or "")

    @tool("request_human_review", "Show the scientist the SOP side by side with the readable steps, PreCheck's flags "
          "and the deck layout, for Approve or Revise. End your turn after calling it.",
          {"type": "object", "properties": {"message": {"type": "string",
                                                        "description": "One or two sentences on what to check"}},
           "required": ["message"]})
    async def request_human_review(args):
        return await call(run.request_human_review, message=args.get("message", ""))

    @tool("revise_protocol", "PreCheck: change the current protocol as the scientist asked (their words), redraft "
          "and heal it as the next version.",
          {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]})
    async def revise_protocol(args):
        return await call(run.revise_protocol, note=args["note"])

    @tool("run_ahc_simulation", "AHC: run the approved protocol on AHC's simulated Hamilton STARlet and compare the "
          "final plate volumes with PreCheck's. Refused unless the scientist approved this version.",
          {"type": "object", "properties": {}, "required": []})
    async def run_ahc_simulation(args):
        return await call(run.run_ahc_simulation)

    tools = [rank_variants, map_starting_concentrations, translate_sop, request_human_review, revise_protocol,
             run_ahc_simulation]
    return create_sdk_mcp_server("argus", "0.1.0", tools=tools), [f"mcp__argus__{t.name}" for t in tools]


def first_message(run: Run) -> str:
    project = run.lanm_project
    context = (f"Selected LanM project: {project['name']} ({project['id']}), "
               f"target {project['target']}, competitor {project['competitor']}. "
               "Use this project for ranking without changing its inputs.\n\n") if project else ""
    return (f"{context}{run.prompt.strip()}\n\n"
            f"The SOP the scientist uploaded ({run.sop_name}) is attached to this run; translate_sop uses it as is. "
            f"Its text, for your reference:\n<sop>\n{run.sop_text.strip()}\n</sop>")


async def drive_live(run: Run) -> None:
    """One agent session per run: the first message, then each review decision, in the same conversation."""
    server, allowed = build_server(run)
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"argus": server},
        strict_mcp_config=True,
        tools=[],
        allowed_tools=allowed,
        setting_sources=[],
        model=config.AGENT_MODEL,
        cwd=str(run.folder),
        max_turns=40,
        env={"MCP_TOOL_TIMEOUT": str(60 * 60 * 1000)},  # translate_sop drafts and heals: minutes, not seconds
    )
    await run.status("agent_starting")
    async with ClaudeSDKClient(options=options) as client:
        message = first_message(run)
        while True:
            run.busy = True
            await run.emit("agent_turn", {"message": message})
            await client.query(message)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            await run.emit("agent_text", {"text": block.text})
                elif isinstance(msg, ResultMessage):
                    run.state["cost_usd"] = round((run.state.get("cost_usd") or 0) + (msg.total_cost_usd or 0), 4)
                    if msg.is_error:
                        await run.emit("error", {"message": f"agent turn ended with an error: {msg.subtype}"})
            run.busy = False
            await run.emit("turn_done", {"status": run.state["status"]})
            if run.state["status"] in ("done", "simulation_failed"):
                break
            message = await run.inbox.get()


METAL = re.compile(r"\b(Y|La|Ce|Pr|Nd|Pm|Sm|Eu|Gd|Tb|Dy|Ho|Er|Tm|Yb|Lu)\b")


async def drive_offline(run: Run) -> None:
    """The same tools in the same order with no model: rank, assay concentration mapping, the fixture protocol, review, simulate."""
    metals = METAL.findall(run.prompt)
    target = metals[0] if metals else "Nd"
    competitor = metals[1] if len(metals) > 1 else ("La" if target != "La" else "Nd")
    n = re.search(r"\btop\s+(\d+)", run.prompt, re.I)
    say = lambda t: run.emit("agent_text", {"text": t})  # noqa: E731
    await run.status("offline")
    await say("Offline mode: no model is used. The same ARGUS tools run in a fixed order, and the protocol is "
              "PreCheck's passing serial-dilution fixture instead of a fresh draft.")
    r = await run.rank_variants(target, competitor, int(n.group(1)) if n else 8)
    await say(r["summary"])
    m = await run.map_starting_concentrations()
    await say(m["summary"] + ". These starting concentrations are a heuristic from LanM contrast.")
    p = await run.translate_sop()
    await say(p["summary"])
    await run.request_human_review("Check the readable steps against the SOP, and the must-review items.")
    await run.emit("turn_done", {"status": run.state["status"]})
    while True:
        message = await run.inbox.get()
        if run.review.state == "approved":
            s = await run.run_ahc_simulation()
            await say(s["summary"])
            await run.emit("turn_done", {"status": run.state["status"]})
            return
        await say("Revisions need Claude to redraft the protocol, and this run is offline. Start a live run to "
                  "revise, or approve this version to simulate it.")
        run.review.request(run.review.ir_hash)
        await run.status("awaiting_review")
        await run.emit("turn_done", {"status": run.state["status"]})
        del message
