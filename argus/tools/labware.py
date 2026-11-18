"""AHC chooses the labware, PreCheck builds the protocol with it.

PreCheck's deck builder picks from all of VENUS's library and knows nothing of AHC, so a draft can ask for labware
AHC cannot load (sample tubes on an SMP_CAR_32_EPIS carrier, say). Then the hand-off breaks: AHC refuses the layout,
or the items fall off it. ARGUS closes that loop in three places:

1. Before drafting: `install()` limits PreCheck's library to what AHC's STARlet accepts, as AHC itself states it
   (`ahc.devices.spec.load_device("hamilton.starlet").layout`), directly or through PreCheck's declared VENUS
   equivalents. A need nothing in that set fits goes back to Claude inside PreCheck's own drafting loop.
2. After healing: `check_with_ahc` loads PreCheck's AHC layout into AHC's simulator, which accepts it or refuses it
   with its own reason, and lists what fell off the layout and what containers run too shallow.
3. Fixing: a diluent that runs shallow gets a larger load (`fill_fixes`: PreCheck's own container geometry, a
   deterministic edit re-checked by PreCheck's gate). Anything else goes back to PreCheck as a revision request
   written from AHC's answer and the labware AHC can load (`revision_note`).
"""

import asyncio
import math
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from argus.tools import lanm

from ahc.devices.spec import load_device  # noqa: E402
from pipeline import deck_builder, flow  # noqa: E402
from pipeline import ahc_export  # noqa: E402
from pipeline.deck import build_deck  # noqa: E402
from pipeline.design_review import trace_mixtures  # noqa: E402

MODEL = "hamilton.starlet"
TARGET_MM = 3.0      # what ARGUS keeps in a reservoir at the end (PreCheck flags below 2.0 mm)
ROUND_UL = 500.0     # loads are rounded up to this


# ------------------------------------------------------------------------------------------- what AHC accepts

def ahc_accepts() -> Dict[str, Any]:
    """The layout types AHC's STARlet accepts, from the AHC that ARGUS runs."""
    layout = load_device(MODEL).layout
    labware = layout.get("labware", {})
    flat = [t for group in labware.values() for t in group] if isinstance(labware, dict) else list(labware)
    return {"tracks": layout.get("tracks", {"min": 1, "max": 30}), "carriers": list(layout.get("carriers", [])),
            "labware": flat, "groups": labware if isinstance(labware, dict) else {}}


def ahc_types() -> Dict[str, Any]:
    """PreCheck's `load_ahc_types` answer, with the type lists taken from ARGUS's AHC (not whichever AHC plugin is
    installed in ~/.claude) and PreCheck's declared VENUS equivalents kept."""
    base = ahc_export.load_ahc_types(ahc_export.ROOT, plugins=Path("/nonexistent"))
    acc = ahc_accepts()
    return {**base, "tracks": acc["tracks"], "carriers": acc["carriers"], "labware": acc["labware"],
            "source": f"AHC {MODEL} device description (ARGUS's ahc submodule)"}


def loadable(entry: Dict[str, Any], types: Dict[str, Any]) -> Optional[str]:
    """The AHC type a VENUS library entry reaches AHC as, or None."""
    carriers = {c.lower(): c for c in types["carriers"]}
    labware = {t.lower(): t for t in types["labware"]}
    if entry["kind"] == "carrier":
        return carriers.get(entry["venus_type"].lower()) or carriers.get((entry.get("factory") or "").lower())
    hit = labware.get((entry.get("factory") or "").lower())
    if hit:
        return hit
    same = {k.lower(): v for k, v in types.get("equivalents", {}).items()}.get(entry["venus_type"].lower())
    return labware.get(same["type"].lower()) if same else None


_original_catalog = deck_builder.catalog
_installed = False


def ahc_catalog(root: Path = deck_builder.ROOT) -> List[Dict[str, Any]]:
    types = ahc_types()
    return [e for e in _original_catalog(root) if loadable(e, types)]


def install() -> None:
    """From now on PreCheck drafts and builds decks only from labware AHC loads, and exports with ARGUS's AHC types.
    Runtime only: nothing in the PreCheck submodule changes."""
    global _installed
    if _installed:
        return
    deck_builder.catalog = ahc_catalog
    flow.load_ahc_types = lambda root=None, plugins=None: ahc_types()
    _installed = True


def options_text() -> str:
    """The labware AHC can load, in words, for a revision request."""
    types = ahc_types()
    lines = []
    for e in ahc_catalog():
        ahc = loadable(e, types)
        if e["kind"] == "carrier":
            continue
        if e["kind"] == "tip_rack":
            what = f"tip rack, {e['tip_volume_ul']:g} uL tips{' with filter' if e['filter'] else ''}"
        elif e["kind"] == "plate":
            what = f"96-well plate, {e['capacity_ul']:g} uL wells"
        else:
            what = f"reservoir, {e['capacity_ul'] / 1000:g} mL, {e.get('channels_at_once', 1)} channels at once"
        lines.append(f"- {e['venus_type']} ({what}; AHC loads it as {ahc})")
    carriers = ", ".join(types["carriers"])
    return "\n".join(lines) + f"\n- carriers AHC accepts: {carriers}\n- no tubes or tube racks: AHC has none"


# ------------------------------------------------------------------------------------------- checking

def check_with_ahc(ir: Dict[str, Any], report: Dict[str, Any], deck_config: Dict[str, Any],
                   layout_file: Path) -> Dict[str, Any]:
    """What AHC says about PreCheck's protocol: does its simulator load the deck and run every step (a dry run that
    stops at AHC's first refusal), what is not on the layout, what runs shallow. Runs in a worker thread."""
    from argus.tools import ahc_sim

    layout = yaml.safe_load(layout_file.read_text())
    dry = asyncio.run(ahc_sim.dry_run(ir, layout))
    refusal = dry["refusal"]
    loaded = refusal is None or refusal["tool"] != "load_layout"
    not_on = list(report.get("ahc", {}).get("not_on_layout", []))
    shallow = depth_report(ir, deck_config)
    roles = []
    sites = {s["name"]: (c, slot, s) for c in layout.get("carriers", []) for slot, s in c.get("sites", {}).items()}
    for lid, need in (ir.get("labware") or {}).items():
        spec = deck_config["labware"].get(lid, {})
        c, slot, s = sites.get(lid, (None, None, None))
        roles.append({"role": lid, "kind": need.get("kind"), "venus_type": spec.get("venus_type") or spec.get("factory")
                      or ("tip waste" if need.get("kind") == "waste" else None),
                      "ahc_type": s["type"] if s else ("deck tip waste" if need.get("kind") == "waste" else None),
                      "where": f"{c['type']} track {c['track']}, site {slot}" if c else None})
    clean = dry["ok"] and not not_on and not any(d["shallow"] for d in shallow)
    return {"loaded": loaded, "refusal": refusal["message"] if refusal and not loaded else None,
            "dry_run": dry, "not_on_layout": not_on, "depth": shallow, "roles": roles, "clean": clean,
            "suggestion": ahc_suggestion(ir, refusal) if refusal and loaded else None}


WELL_MIN_UL = 30.0  # PreCheck's well minimum: what a well keeps after its last draw


def ahc_suggestion(ir: Dict[str, Any], refusal: Dict[str, Any]) -> Dict[str, Any]:
    """AHC's refusal of a step, turned into the change that answers it, with AHC's own words kept.
    insufficient_liquid on a source nothing loaded: the load that covers every draw from it (per well for plate
    wells, in AHC's target form, as PreCheck's initial_volumes_ul now takes them)."""
    import re as _re

    code, msg = refusal.get("code"), refusal.get("message", "")
    step = refusal.get("ir_step")
    base = {"code": code, "ir_step": step, "ahc_said": msg.replace("Error executing tool ", "")}
    target = ((refusal.get("args") or {}).get("targets") or [""])[0]
    lid = target.split(":")[0]
    kinds = {k: v.get("kind") for k, v in (ir.get("labware") or {}).items()}
    if code == "insufficient_liquid" and lid:
        loads: Dict[str, float] = {}
        if kinds.get(lid) == "plate":
            per_well: Dict[tuple, float] = {}
            for s in ir["steps"]:
                if s["action"] == "aspirate" and s.get("labware") == lid:
                    for ch in s.get("channels", [0]):
                        per_well[(s["column"], ch)] = per_well.get((s["column"], ch), 0.0) + float(s["volume_ul"])
            for col in sorted({c for c, _ in per_well}):
                rows = sorted(ch for c, ch in per_well if c == col)
                need = max(per_well[(col, ch)] for ch in rows) + WELL_MIN_UL
                rng = (f"{lanm.ROWS[rows[0]]}{col}:{lanm.ROWS[rows[-1]]}{col}" if len(rows) > 1
                       else f"{lanm.ROWS[rows[0]]}{col}")
                loads[f"{lid}:{rng}"] = math.ceil(need / 5) * 5
        else:
            drawn = sum(float(s["volume_ul"]) * len(s.get("channels", [0])) for s in ir["steps"]
                        if s["action"] == "aspirate" and s.get("labware") == lid)
            loads[lid] = math.ceil(drawn * 1.1 / ROUND_UL) * ROUND_UL
        text = ("the source starts empty in AHC's model of the deck; load "
                + ", ".join(f"{v:g} uL in {'each of ' if ':' in k else ''}{k}" for k, v in loads.items())
                + " before the run (what the steps draw, plus what a well keeps)")
        return {**base, "kind": "load", "loads": loads, "text": text}
    hint = _re.search(r"Hint: (.*)$", msg)
    return {**base, "kind": "other", "loads": {}, "text": hint.group(1) if hint else msg}


def depth_report(ir: Dict[str, Any], deck_config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every container the steps draw from: depth at the start and end of the run (PreCheck's geometry), and the
    load that keeps TARGET_MM at the end."""
    kinds = {lid: spec.get("kind") for lid, spec in deck_config["labware"].items()}
    _, res = build_deck(deck_config)
    _, drawn = trace_mixtures(ir, kinds)
    diluent = lanm.diluent_names(ir)
    reagents = ir.get("reagents") or {}
    out = []
    for lid, loaded in (ir.get("initial_volumes_ul") or {}).items():
        r = res.get(lid)
        to_h = getattr(r, "compute_height_from_volume", None)
        used = drawn.get(lid, 0.0)
        if to_h is None or used <= 0:
            continue
        cap = float(getattr(r, "max_volume", 0) or 0)
        end = to_h(max(loaded - used, 0.0))
        need = _load_for(to_h, used, cap)
        out.append({"labware": lid, "reagent": reagents.get(lid, lid), "is_diluent": reagents.get(lid, lid) in diluent,
                    "loaded_ul": loaded, "drawn_ul": round(used, 1), "start_mm": round(to_h(loaded), 2),
                    "end_mm": round(end, 2), "shallow": end < 2.0, "capacity_ul": cap,
                    "load_for_target_ul": need})
    return out


def _load_for(to_h, used: float, cap: float) -> Optional[float]:
    """Smallest load (rounded up to ROUND_UL) leaving TARGET_MM after `used` is drawn; None if over 95% capacity."""
    lo, hi = 0.0, cap * 0.95
    if hi <= used or to_h(hi - used) < TARGET_MM:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        if to_h(mid) >= TARGET_MM:
            hi = mid
        else:
            lo = mid
    return math.ceil((used + hi) / ROUND_UL) * ROUND_UL


# ------------------------------------------------------------------------------------------- fixing

def fill_fixes(check: Dict[str, Any]) -> Dict[str, float]:
    """Diluent that runs shallow: the load that keeps TARGET_MM. Samples are left for the scientist (raising a
    variant stock's volume spends protein)."""
    return {d["labware"]: d["load_for_target_ul"] for d in check["depth"]
            if d["shallow"] and d["is_diluent"] and d["load_for_target_ul"]}


def structural_problems(check: Dict[str, Any], fixed: Dict[str, float]) -> List[str]:
    problems = []
    if not check["loaded"]:
        problems.append(f"AHC's simulator refused the deck: {check['refusal']}")
    sug = check.get("suggestion")
    if sug:
        problems.append(f"AHC's simulator refused step {sug['ir_step']}: {sug['ahc_said']} AHC's suggestion: "
                        f"{sug['text']}.")
    problems += [f"Not on the AHC layout: {p}" for p in check["not_on_layout"]]
    for d in check["depth"]:
        if d["shallow"] and d["labware"] not in fixed:
            problems.append(f"'{d['labware']}' ({d['reagent']}) holds {d['loaded_ul']:g} uL and ends {d['end_mm']:.1f} mm "
                            f"deep in its container: too shallow for a tip")
    return problems


def design_problems(ir: Dict[str, Any], plate_map: Optional[Dict[str, Any]], tol: float = 0.02) -> List[str]:
    """What PreCheck's intent guard cannot see, because it protects the draft and here the draft itself is off: the
    assay plate map's concentrations (every well, from the protocol's own steps, within `tol`), and the robot's work
    (pre-filled wells are sources the steps draw from, never wells the steps transfer into)."""
    from pipeline.deck import loaded_wells

    problems = []
    filled = {}
    for key in (ir.get("initial_volumes_ul") or {}):
        w = loaded_wells(key)
        if w:
            for col, row in w[1]:
                filled[(w[0], col, row)] = key
    received = set()
    steps = ir.get("steps", [])
    for i, s in enumerate(steps):
        if s["action"] != "dispense" or (s.get("labware"), s.get("column")) == (None, None):
            continue
        prev = steps[i - 1] if i else {}
        if prev.get("action") == "aspirate" and (prev.get("labware"), prev.get("column")) == (s.get("labware"), s.get("column")):
            continue  # a mix: back into the well it came from
        for ch in s.get("channels", [0]):
            if (s["labware"], s.get("column"), ch) in filled:
                received.add(filled[(s["labware"], s.get("column"), ch)])
    for key in sorted(received):
        problems.append(f"'{key}' is pre-filled by hand and the steps then transfer liquid into it: the robot should "
                        "pipette what goes into those wells (pre-fill only source samples, in a separate source plate)")
    if plate_map:
        conc = lanm.concentrations_from_protocol(ir, plate_map)["wells"]
        df = plate_map["params"]["dilution_factor"]
        for col in plate_map["columns"]:
            for r in plate_map["rows"]:
                want = r["nominal_uM"][col - 1]
                got = (conc.get(f"{r['row']}{col}") or {}).get("uM")
                if got is None or abs(got - want) > tol * want:
                    shown = lanm.format_uM(got) if got else "no variant"
                    problems.append(
                        f"column {col} row {r['row']} ends at {shown} of {r['name']} where the assay plate map has "
                        f"{lanm.format_uM(want)} (stock {lanm.format_uM(r['start_uM'])} diluted 1:{df:g} into column 1, "
                        f"then 1:{df:g} per column): a 1:{df:g} step is 1 part sample to {df - 1:g} parts diluent")
                    break  # one row per column is enough to say what is wrong
            if len(problems) > 6:
                break
    return problems


def revision_note(problems: List[str]) -> str:
    return ("AHC (the controller that runs this protocol on the STARlet, on its simulator) and ARGUS's check against "
            "the assay plate map and the scientist's request found:\n"
            + "\n".join(f"- {p}" for p in problems) +
            "\n\nChange the plan so AHC can run it. Use only this labware from the lab's library, which AHC loads:\n"
            + options_text() +
            "\n\nPer-row samples (one LanM variant stock per row) go in column 1, rows A-H, of a second 96-well plate, "
            "drawn with all 8 channels in one aspirate, and those wells are declared as pre-filled in "
            "initial_volumes_ul as \"<plate>:A1:H1\": <uL in each well> (what the steps draw plus 30 uL). A small "
            "sample volume belongs in plate wells, not a trough. Keep every transfer volume, dilution and mix exactly "
            "as it is: change only where liquids come from and what is loaded before the run.")
