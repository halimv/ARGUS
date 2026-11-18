import json

from argus import config
from argus.tools import ahc_sim, lanm
from argus.tools import precheck as pc


def fixture_ir():
    return json.loads((config.PRECHECK_FIXTURES / "serial_passing.json").read_text())


VARIANTS = [{"candidate_id": f"o-{i}", "name": f"o-{i}", "contrast": c}
            for i, c in enumerate([0.70, 0.69, 0.66, 0.60])]


def test_assay_mapping_scales_by_contrast():
    pm = lanm.map_starting_concentrations(VARIANTS, c_max_uM=100, c_min_uM=10)
    starts = [r["start_uM"] for r in pm["rows"]]
    assert starts[0] == 10 and starts[-1] == 100            # highest contrast -> c_min, lowest -> c_max
    assert starts == sorted(starts)                         # monotonic in contrast
    assert [r["row"] for r in pm["rows"]] == list("ABCD")
    assert pm["rows"][0]["nominal_uM"][:2] == [1.0, 0.1]    # 1:10 from the stock


def test_assay_mapping_equal_contrasts_and_limits():
    pm = lanm.map_starting_concentrations([{**v, "contrast": 0.5} for v in VARIANTS])
    assert {r["start_uM"] for r in pm["rows"]} == {100.0}
    try:
        lanm.map_starting_concentrations(VARIANTS, c_max_uM=1, c_min_uM=10)
    except ValueError:
        pass
    else:
        raise AssertionError("c_min above c_max accepted")


def test_rank_variants_from_lanm():
    r = lanm.rank_variants("Nd", "La", 8)
    assert len(r["variants"]) == 8 and r["eligible_count"] > 100
    contrasts = [v["contrast"] for v in r["variants"]]
    assert contrasts == sorted(contrasts, reverse=True)


def test_readable_steps_follow_the_protocol():
    ir = fixture_ir()
    pm = lanm.map_starting_concentrations(VARIANTS + VARIANTS)
    steps = pc.readable_steps(ir, pm, ir["source"]["text"])
    assert len(steps) == 7                                   # one per tip cycle
    assert steps[0]["title"].startswith("Fill columns 1–6 with 90 µL")
    assert steps[1]["title"].startswith("Add 10 µL variant stock to column 1, then mix column 1")
    assert steps[-1]["title"] == "Mix column 6"
    assert steps[0]["ir_steps"] == [1, 14] and steps[-1]["ir_steps"] == [47, 50]
    col1 = next(c for c in steps[1]["concentrations"] if c["column"] == 1)
    assert col1["by_row"]["A"] == lanm.format_uM(pm["rows"][0]["start_uM"] / 10)
    assert steps[0]["sop"] and "saline" in steps[0]["sop"]["text"]


def test_concentrations_from_protocol_are_tenfold():
    ir = fixture_ir()
    pm = lanm.map_starting_concentrations(VARIANTS + VARIANTS)
    wells = lanm.concentrations_from_protocol(ir, pm)["wells"]
    a = pm["rows"][0]["start_uM"]
    for k in range(1, 7):
        assert abs(wells[f"A{k}"]["uM"] - a / 10 ** k) < 1e-9 * a


def test_ir_to_ahc_calls():
    calls = ahc_sim.ir_to_ahc_calls(fixture_ir())
    assert len(calls) == 50 and all(c["tool"] for c in calls)
    assert calls[0] == {"ir_step": 1, "description": calls[0]["description"], "tool": "pick_up_tips",
                        "args": {"device": "starlet.pip", "tips": ["tips:A1:H1"]}}
    assert calls[1]["args"]["targets"] == ["diluent"]
    assert calls[2]["args"]["targets"] == ["dilution_plate:A1:H1"] and calls[2]["args"]["volumes"] == 90.0
    assert ahc_sim._wells("p", 3, [0, 2]) == ["p:A3", "p:C3"]


def test_expected_volumes():
    exp = ahc_sim.expected_volumes(fixture_ir())
    assert len(exp) == 48
    assert exp["dilution_plate:A1"] == 90.0 and exp["dilution_plate:H6"] == 100.0
