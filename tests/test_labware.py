"""AHC decides which labware PreCheck may use; the check loads PreCheck's deck into AHC's simulator."""
import json

from argus import config
from argus.tools import labware
from argus.tools import precheck as pc  # noqa: F401  (installs the AHC constraint)
from pipeline import deck_builder


def test_ahc_accepts_comes_from_ahc():
    acc = labware.ahc_accepts()
    assert "PLT_CAR_L5_DWP" in acc["carriers"] and "SMP_CAR_32_EPIS_A00" not in acc["carriers"]
    assert "hamilton_1_trough_60mL_Vb" in acc["labware"]


def test_precheck_library_is_limited_to_what_ahc_loads():
    entries = deck_builder.catalog()
    kinds = {e["kind"] for e in entries}
    assert "tube_rack" not in kinds                         # AHC has no tubes
    types = labware.ahc_types()
    assert all(labware.loadable(e, types) for e in entries)
    assert any(e["venus_type"] == "Cos_96_PCR" for e in entries)


def test_ahc_refuses_a_carrier_it_does_not_know():
    import asyncio
    from argus.tools import ahc_sim
    layout = {"carriers": [{"name": "smp", "type": "SMP_CAR_32_EPIS_A00", "track": 14, "sites": {}}]}
    out = asyncio.run(ahc_sim.dry_run({"labware": {}, "steps": []}, layout))
    assert not out["ok"] and out["refusal"]["code"] == "carrier_not_allowed"


STOCK_IR = {
    "labware": {"tips": {"kind": "tip_rack"}, "stocks": {"kind": "plate"}, "dilution_plate": {"kind": "plate"},
                "diluent": {"kind": "reservoir"}, "waste": {"kind": "waste"}},
    "reagents": {"stocks": "variant stocks", "diluent": "0.9% saline"},
    "initial_volumes_ul": {"diluent": 9000},
    "steps": [
        {"step": 1, "action": "tips_on", "labware": "tips", "column": 1, "channels": list(range(8))},
        {"step": 2, "action": "aspirate", "labware": "stocks", "column": 1, "volume_ul": 10, "channels": list(range(8))},
        {"step": 3, "action": "dispense", "labware": "dilution_plate", "column": 1, "volume_ul": 10,
         "channels": list(range(8))},
        {"step": 4, "action": "tips_off", "labware": "waste", "channels": list(range(8))},
    ],
}


def test_insufficient_liquid_becomes_a_well_load():
    refusal = {"tool": "aspirate", "code": "insufficient_liquid", "ir_step": 2,
               "args": {"targets": ["stocks:A1:H1"], "volumes": 10.0},
               "message": "Error executing tool aspirate: [insufficient_liquid] stocks_well_A1 holds 0 uL; 10 uL "
                          "requested. Hint: Draw less, or add liquid to the layout."}
    sug = labware.ahc_suggestion(STOCK_IR, refusal)
    assert sug["kind"] == "load" and sug["loads"] == {"stocks:A1:H1": 40}
    assert "insufficient_liquid" in sug["ahc_said"]


def test_precheck_takes_prefilled_wells():
    from pipeline.deck import loaded_wells
    from pipeline.design_review import trace_mixtures
    assert loaded_wells("stocks:A1:H1") == ("stocks", [(1, r) for r in range(8)])
    assert loaded_wells("stocks:col2")[1][0] == (2, 0) and loaded_wells("diluent") is None
    ir = {**STOCK_IR, "initial_volumes_ul": {"diluent": 9000, "stocks:A1:H1": 40}}
    where, _ = trace_mixtures(ir, {k: v["kind"] for k, v in ir["labware"].items()})
    assert where[("dilution_plate", 1, 0)] == {"variant stocks": 10.0}
    assert where[("stocks", 1, 7)] == {"variant stocks": 30.0}


def test_load_for_target_keeps_depth():
    ir = json.loads((config.PRECHECK_FIXTURES / "serial_passing.json").read_text())
    deck = json.loads((config.PRECHECK_FIXTURES / "serial_deck.json").read_text())
    depth = {d["labware"]: d for d in labware.depth_report(ir, deck)}
    dil = depth["diluent"]
    assert dil["shallow"] and dil["is_diluent"]
    assert dil["load_for_target_ul"] > dil["loaded_ul"]
    ir["initial_volumes_ul"]["diluent"] = dil["load_for_target_ul"]
    again = {d["labware"]: d for d in labware.depth_report(ir, deck)}["diluent"]
    assert again["end_mm"] >= labware.TARGET_MM and not again["shallow"]
    assert labware.fill_fixes({"depth": list(depth.values())}) == {"diluent": dil["load_for_target_ul"]}


def test_revision_note_names_ahc_labware():
    note = labware.revision_note(["Not on the AHC layout: carrier SMP_CAR_32_EPIS_A00_14"])
    assert "Cos_96_PCR" in note and "no tubes" in note and "column 1" in note


def _plate_map():
    from argus.tools import lanm
    v = [{"candidate_id": f"o-{i}", "name": f"o-{i}", "contrast": 0.7 - i / 100} for i in range(8)]
    return lanm.map_starting_concentrations(v)


def test_design_check_passes_the_fixture():
    ir = json.loads((config.PRECHECK_FIXTURES / "serial_passing.json").read_text())
    assert labware.design_problems(ir, _plate_map()) == []


def test_design_check_catches_bench_work_and_ratio_drift():
    ir = json.loads((config.PRECHECK_FIXTURES / "serial_passing.json").read_text())
    bad = json.loads(json.dumps(ir))
    for s in bad["steps"]:                     # 10 uL into 100 uL: 1:11, not 1:10
        if s["action"] in ("aspirate", "dispense") and s["labware"] == "diluent" or (
                s["action"] == "dispense" and s.get("volume_ul") == 90):
            s["volume_ul"] = 100
    probs = labware.design_problems(bad, _plate_map())
    assert any("assay plate map" in p and "column 1" in p for p in probs)
    pre = json.loads(json.dumps(ir))
    pre["initial_volumes_ul"]["dilution_plate:A2:H2"] = 90
    assert any("pre-filled by hand" in p and "A2:H2" in p for p in labware.design_problems(pre, _plate_map()))
