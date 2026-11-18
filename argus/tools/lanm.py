"""LanM research platform as tools: rank variants, then the TEA gate (starting concentrations on a plate map).

Ranking is LanM's own (`Store.rank`, untested pool: published target-minus-competitor normalized profile contrast).
LanM has no concentrations, so the TEA gate here is ARGUS's: a heuristic that gives more selective variants (higher
contrast) a lower starting concentration, interpolated on a log scale between c_max and c_min.
"""

import math
from typing import Any, Dict, List, Optional

from argus import config

from lanm_platform.core import METALS, Store  # noqa: E402  (config.setup() put it on the path)

ROWS = "ABCDEFGH"
_store: Optional[Store] = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store(str(config.LANM_DATA))
    return _store


def rank_variants(target: str = "Nd", competitor: str = "La", top_n: int = 8) -> Dict[str, Any]:
    """Top `top_n` LanM variants for selectivity of `target` over `competitor`."""
    target, competitor = target.strip().capitalize(), competitor.strip().capitalize()
    if target not in METALS or competitor not in METALS or target == competitor:
        raise ValueError(f"target and competitor must be two different elements of {', '.join(METALS)}")
    top_n = max(1, min(int(top_n), len(ROWS)))
    s = store()
    project = s.create_project(f"ARGUS {target}/{competitor}", target, competitor,
                               description="Created by ARGUS for a SpyCI-LAMBS serial dilution run")
    run = s.rank(project["id"], pool="untested", limit=top_n)
    variants = []
    for row in run["rows"]:
        variants.append({
            "rank": row["rank"],
            "candidate_id": row["candidate_id"],
            "name": row["name"],
            "organism": row.get("organism"),
            "contrast": round(float(row["profile_contrast"]), 6),
            "sd": row.get("reported_profile_sd"),
            "source": row.get("source_id"),
        })
    return {
        "target": target, "competitor": competitor,
        "lanm_project_id": project["id"], "lanm_ranking_run_id": run["id"],
        "eligible_count": run.get("eligible_count"),
        "rationale": run.get("rationale"),
        "uncertainties": run.get("uncertainties"),
        "variants": variants,
    }


def starting_concentration(contrast: float, lo: float, hi: float, c_max: float, c_min: float) -> float:
    """c = c_max * (c_min / c_max) ** t, t = (contrast - lo) / (hi - lo): the highest contrast gets c_min."""
    t = 0.0 if hi - lo < 1e-12 else (contrast - lo) / (hi - lo)
    return c_max * (c_min / c_max) ** t


def map_starting_concentrations(variants: List[Dict[str, Any]], c_max_uM: float = 100.0, c_min_uM: float = 10.0,
                                dilution_factor: float = 10.0, n_points: int = 6) -> Dict[str, Any]:
    """The TEA gate: each variant gets a plate row (A-H) and a starting (stock) concentration scaled by its contrast.

    Columns 1..n_points hold the series; the nominal concentration in column k is start / dilution_factor**k (the
    stock is diluted 1:dilution_factor into column 1). The protocol's actual per-well concentrations are worked out
    from its steps later (`concentrations_from_protocol`)."""
    if not variants:
        raise ValueError("no variants to map")
    if c_min_uM <= 0 or c_max_uM <= 0 or c_min_uM > c_max_uM:
        raise ValueError("need 0 < c_min_uM <= c_max_uM")
    variants = variants[:len(ROWS)]
    contrasts = [float(v["contrast"]) for v in variants]
    lo, hi = min(contrasts), max(contrasts)
    rows = []
    for i, v in enumerate(variants):
        start = starting_concentration(float(v["contrast"]), lo, hi, c_max_uM, c_min_uM)
        rows.append({
            "row": ROWS[i], "candidate_id": v["candidate_id"], "name": v.get("name", v["candidate_id"]),
            "contrast": v["contrast"], "start_uM": round(start, 4),
            "nominal_uM": [round(start / dilution_factor ** k, 8) for k in range(1, n_points + 1)],
        })
    return {
        "unit": "uM",
        "rows": rows,
        "columns": list(range(1, n_points + 1)),
        "params": {"c_max_uM": c_max_uM, "c_min_uM": c_min_uM, "dilution_factor": dilution_factor,
                   "n_points": n_points},
        "formula": "start = c_max * (c_min/c_max) ** ((contrast - min) / (max - min)); higher contrast -> lower start",
        "note": "Heuristic TEA gate: LanM ranks by published Nd/La-style profile contrast and has no concentrations; "
                "starting concentrations are scaled from that contrast, for the scientist to review.",
    }


def assay_context(plate_map: Dict[str, Any]) -> str:
    """The plate map in words, appended to the SOP so PreCheck drafts the plate the TEA gate laid out."""
    p = plate_map["params"]
    lines = [
        "",
        "ASSAY CONTEXT (from the ARGUS TEA gate, for the LLNL SpyCI-LAMBS assay):",
        f"- One LanM variant per plate row; the sample to dilute in each row is that variant's stock. "
        f"Do a 1:{p['dilution_factor']:g} serial dilution across columns 1 to {p['n_points']} of a 96-well plate.",
        "- Row stocks (starting concentrations):",
    ]
    lines += [f"  row {r['row']}: {r['name']} at {r['start_uM']:.3g} uM" for r in plate_map["rows"]]
    lines.append("- Use 8 channels so each row gets its own variant; keep every other detail of the SOP above.")
    return "\n".join(lines)


def concentrations_from_protocol(ir: Dict[str, Any], plate_map: Dict[str, Any]) -> Dict[str, Any]:
    """What each plate well ends up at, from the protocol's own steps: the row's start concentration times the share
    of the well that is sample (not diluent). Uses PreCheck's `trace_mixtures`, so it is the protocol's arithmetic,
    not the nominal series."""
    from pipeline.design_review import trace_mixtures

    kinds = {lid: spec.get("kind") for lid, spec in (ir.get("labware") or {}).items()}
    where, _ = trace_mixtures(ir, kinds)
    diluent = diluent_names(ir)
    starts = {r["row"]: r["start_uM"] for r in plate_map["rows"]}
    wells: Dict[str, Dict[str, Any]] = {}
    for key, mix in where.items():
        if not isinstance(key, tuple) or key[1] is None:
            continue
        plate, column, ch = key
        if ch >= len(ROWS):
            continue
        row = ROWS[ch]
        total = sum(mix.values())
        sample = sum(v for k, v in mix.items() if k not in diluent)
        share = sample / total if total > 0 else 0.0
        conc = starts.get(row, 0.0) * share if row in starts else None
        wells[f"{row}{column}"] = {"plate": plate, "volume_ul": round(total, 3), "sample_share": share,
                                   "uM": None if conc is None else conc}
    return {"unit": "uM", "wells": wells}


DILUENT_WORDS = ("diluent", "saline", "buffer", "water", "solvent", "media", "medium", "pbs")


def diluent_names(ir: Dict[str, Any]) -> set:
    """The reagent names (as trace_mixtures keys them) that are diluent, by the words in their labware or name."""
    names = set()
    reagents = ir.get("reagents") or {}
    labels = {lid: reagents.get(lid, lid) for lid in (ir.get("initial_volumes_ul") or {})}
    labels.update(reagents)
    for lid, label in labels.items():
        text = f"{lid} {label}".lower()
        if any(w in text for w in DILUENT_WORDS):
            names.add(label)
    return names


def format_uM(value: Optional[float]) -> str:
    if value is None:
        return "-"
    if value == 0:
        return "0"
    if value >= 1:
        return f"{value:.3g} µM"
    if value >= 1e-3:
        return f"{value * 1e3:.3g} nM"
    if value >= 1e-6:
        return f"{value * 1e6:.3g} pM"
    exp = math.floor(math.log10(value))
    return f"{value / 10 ** exp:.2g}e{exp} µM"
