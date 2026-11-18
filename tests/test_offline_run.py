"""The whole chain offline: rank -> TEA gate -> fixture protocol -> review gate -> AHC simulation."""
import asyncio

import pytest

from argus import config
from argus.agent import drive_offline
from argus.run import Run, ToolRefused

pytestmark = pytest.mark.anyio


async def wait_for(cond, timeout=240):
    for _ in range(int(timeout / 0.1)):
        if cond():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("timed out")


async def test_offline_chain_needs_approval_then_passes():
    run = Run("Top 8 LanM variants for Nd over La", config.DEFAULT_SOP.read_text(), offline=True)
    task = asyncio.create_task(drive_offline(run))
    await wait_for(lambda: run.state["status"] == "awaiting_review")

    assert len(run.state["variants"]["variants"]) == 8
    assert len(run.state["plate_map"]["rows"]) == 8
    proto = run.state["protocol"]
    assert proto["status"] == "PASSED" and len(proto["readable_steps"]) == 7
    assert any(i["severity"] == "must" and "Offline fixture" in i["item"] for i in proto["needs_review"])
    lc = proto["labware_check"]                          # AHC loaded the deck; the diluent load was raised
    assert lc["rounds"][0]["ahc_loaded"]
    assert any(a.startswith("Applied AHC's fix: load") and " diluent " in a and "intent guard pass" in a
               for a in lc["rounds"][0]["actions"])
    assert lc["rounds"][-1]["dry_run"]["ok"]             # AHC ran every step before the review
    assert not any("diluent" in i["item"] for i in proto["needs_review"])
    assert proto["model_notes"]

    with pytest.raises(ToolRefused):                     # the gate is in code, not only in the prompt
        await run.run_ahc_simulation()

    await run.inbox.put(await run.decide("approve", "tester", "looks right"))
    await asyncio.wait_for(task, 240)
    sim = run.state["simulation"]
    assert sim["passed"], sim.get("refusal")
    assert sim["comparison"] == {**sim["comparison"], "mismatches": 0, "checked": 48}
    assert run.state["status"] == "done"
    assert (run.folder / "ahc" / "argus_simulation.json").exists()


async def test_decision_without_pending_review_is_refused():
    run = Run("x", "serially dilute 1:10", offline=True)
    with pytest.raises(ToolRefused):
        await run.decide("approve", "tester", "")
