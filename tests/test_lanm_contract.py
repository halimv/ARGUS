"""Regression checks for the LanM handoff and its protocol approval boundary."""
import copy
import importlib.util
import json
import math
import sys
import types

import pytest

from argus.tools import lanm


VARIANTS = [{"candidate_id": "synthetic", "name": "synthetic", "contrast": 0.5}]


@pytest.mark.parametrize("changes", [
    {"c_min_uM": 0}, {"c_max_uM": -1}, {"c_min_uM": 101},
    {"c_min_uM": math.nan}, {"c_max_uM": math.inf}, {"c_max_uM": True},
    {"c_min_uM": "10"}, {"dilution_factor": 0}, {"dilution_factor": 0.5},
    {"dilution_factor": 1}, {"dilution_factor": math.nan}, {"dilution_factor": math.inf},
    {"dilution_factor": True}, {"n_points": 0}, {"n_points": 13},
    {"n_points": -1}, {"n_points": 6.5}, {"n_points": True}, {"n_points": "6"},
])
def test_invalid_assay_inputs_are_refused(changes):
    with pytest.raises(ValueError):
        lanm.map_starting_concentrations(VARIANTS, **changes)


@pytest.mark.parametrize("contrast", [None, math.nan, math.inf, -math.inf, True, "0.5"])
def test_nonfinite_or_nonnumeric_contrast_is_refused(contrast):
    with pytest.raises(ValueError, match="variant contrast"):
        lanm.map_starting_concentrations([{**VARIANTS[0], "contrast": contrast}])


def test_small_concentrations_survive_mapping_and_protocol_handoff():
    result = lanm.map_starting_concentrations(VARIANTS, c_min_uM=1e-5, c_max_uM=1e-5, n_points=12)
    row = result["rows"][0]
    assert row["start_uM"] == 1e-5
    assert row["nominal_uM"][-1] > 0
    assert row["nominal_uM"][-1] == pytest.approx(1e-17, rel=1e-12, abs=0)
    assert all(a / b == pytest.approx(10) for a, b in zip(row["nominal_uM"], row["nominal_uM"][1:]))
    assert "at 1e-05 uM" in lanm.assay_context(result)


def test_extreme_finite_values_interpolate_without_ratio_overflow():
    variants = [{**VARIANTS[0], "contrast": contrast} for contrast in (-1e308, 0.0, 1e308)]
    result = lanm.map_starting_concentrations(variants, c_min_uM=1e-100, c_max_uM=1e300, n_points=1)
    assert [r["start_uM"] for r in result["rows"]] == pytest.approx([1e300, 1e100, 1e-100], rel=1e-12, abs=0)


def test_underflow_is_refused_instead_of_turning_into_zero_stock():
    with pytest.raises(ValueError, match="positive numeric range"):
        lanm.map_starting_concentrations(VARIANTS, c_min_uM=5e-324, c_max_uM=5e-324)


@pytest.fixture
def run_contract(monkeypatch):
    """Exercise the real Run guards without importing or executing robot adapters."""
    from argus import config, tools

    for name in ("ahc_sim", "precheck"):
        stub = types.ModuleType(f"argus.tools.{name}")
        monkeypatch.setitem(sys.modules, stub.__name__, stub)
        monkeypatch.setattr(tools, name, stub, raising=False)
    spec = importlib.util.spec_from_file_location("_argus_run_contract", config.ROOT / "argus" / "run.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["rank_variants", "map_starting_concentrations"])
@pytest.mark.parametrize("review_state", ["none", "pending", "approved"])
async def test_existing_protocol_blocks_upstream_plan_changes(tmp_path, monkeypatch, run_contract, operation, review_state):
    from argus import config

    monkeypatch.setattr(config, "RUNS", tmp_path)
    run = run_contract.Run("synthetic contract test", "synthetic SOP", offline=True)
    run.state["variants"] = {"variants": copy.deepcopy(VARIANTS)}
    run.state["plate_map"] = lanm.map_starting_concentrations(VARIANTS)
    run._pc = {"ir": {"steps": []}}
    if review_state != "none":
        run.review.request(run_contract.ir_hash(run._pc["ir"]))
    if review_state == "approved":
        run.review.decide("approve", "tester", "")
    before = copy.deepcopy((run.state, run._pc, run.review.public()))

    def unexpected(*args, **kwargs):
        raise AssertionError("upstream tool must not run after a protocol exists")

    monkeypatch.setattr(lanm, operation, unexpected)
    with pytest.raises(run_contract.ToolRefused, match="start a new run"):
        await getattr(run, operation)()
    assert (run.state, run._pc, run.review.public()) == before


@pytest.mark.anyio
@pytest.mark.parametrize("rank_fails", [False, True])
async def test_reranking_clears_old_assay_map_only_on_success(tmp_path, monkeypatch, run_contract, rank_fails):
    from argus import config

    monkeypatch.setattr(config, "RUNS", tmp_path)
    run = run_contract.Run("synthetic rerank test", "synthetic SOP", offline=True)
    run.state["variants"] = {"variants": copy.deepcopy(VARIANTS)}
    run.state["plate_map"] = lanm.map_starting_concentrations(VARIANTS)
    run.save()
    before = copy.deepcopy(run.state)
    replacement = {"variants": [{**VARIANTS[0], "rank": 1, "candidate_id": "replacement", "name": "replacement"}],
                   "target": "Nd", "competitor": "La", "lanm_project_id": "isolated-project"}

    def rank(*args, **kwargs):
        if rank_fails:
            raise ValueError("synthetic ranking failure")
        return replacement

    monkeypatch.setattr(lanm, "rank_variants", rank)
    if rank_fails:
        with pytest.raises(ValueError, match="synthetic ranking failure"):
            await run.rank_variants()
        assert run.state == before
        assert not any(e["type"] == "panel" for e in run.events)
    else:
        await run.rank_variants()
        assert run.state["plate_map"] is None
        assert run.state["variants"] == replacement
        assert any(e["type"] == "panel" and e["data"] == {"key": "plate_map", "value": None}
                   for e in run.events)
    assert json.loads((run.folder / "run.json").read_text())["state"] == run.state
