"""PreCheck as tools: SOP -> drafted IR -> healed (intent-guarded) IR -> PLR script + AHC layout, plus the protocol as
numbered steps a scientist can read next to the SOP.

The chain is the one `PreCheck/plugin/pipeline.py:main()` runs (draft -> deck -> heal ladder -> finish -> run record),
called as functions. Drafting and repair go through the `claude` CLI, as in PreCheck. `load_fixture_protocol` skips
both and uses PreCheck's passing serial-dilution fixture, so the rest of the chain runs offline.
"""

import contextlib
import copy
import io
import json
import re
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from argus import config
from argus.tools import labware, lanm

from pipeline import paths as pc_paths  # noqa: E402
from pipeline import runs as pc_runs  # noqa: E402
from pipeline.deck import deck_config_for, load_deck_config  # noqa: E402
from pipeline.flow import NotPassing, finish, next_stem  # noqa: E402
from pipeline.instrument import DEFAULT_INSTRUMENT, load_instrument  # noqa: E402
from pipeline.self_heal import CLAUDE_LADDER, SelfHealer, heal_with_ladder, ir_diff  # noqa: E402
from pipeline.stage0_drafter import draft, revise  # noqa: E402

labware.install()  # PreCheck drafts from labware AHC loads, and exports with ARGUS's AHC types

Emit = Callable[[str], None]
ROWS = lanm.ROWS
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class _Lines(io.TextIOBase):
    """A text stream that hands each finished line to `emit` (PreCheck prints its progress)."""

    def __init__(self, emit: Emit):
        self.emit, self.buf = emit, ""

    def write(self, s: str) -> int:
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            line = ANSI.sub("", line).rstrip()
            if line.strip():
                self.emit(line)
        return len(s)


def _base_deck() -> Tuple[Dict[str, Any], str]:
    root = pc_paths.PLUGIN
    deck_file = load_instrument(DEFAULT_INSTRUMENT, root)["deck_layout"]["file"]
    return load_deck_config(str(root / deck_file)), deck_file


def _heal_and_finish(protocol: Dict[str, Any], stem: str, source: str, emit: Emit,
                     max_rounds: int = 5) -> Dict[str, Any]:
    deck_config = deck_config_for(protocol, pc_paths.PLUGIN)
    work_dir = pc_paths.BUILD / stem

    def tier_done(tier):
        emit(f"[heal] {tier['model']}: {tier['status']} after {max(len(tier['rounds']) - 1, 0)} round(s)")

    result, tiers = heal_with_ladder(protocol, deck_config, str(work_dir), max_rounds, models=list(CLAUDE_LADDER),
                                     escalate_after=1, on_tier=tier_done)
    rounds = [dict(r, model=t["model"]) for t in tiers for r in t["rounds"]]
    tier_info = [{"model": t["model"], "status": t["status"], "rounds": max(len(t["rounds"]) - 1, 0)} for t in tiers]
    if result.status == "GAVE_UP":
        last = rounds[-1] if rounds else {}
        return {"status": "NOT_READY", "stem": stem, "tiers": tier_info,
                "failure": last.get("summary") or last.get("feedback") or "did not pass the simulator",
                "draft_ir": protocol}
    passing = result.protocol
    report = finish(passing, deck_config, stem, rounds, status=result.status, tiers=tiers)
    _record(stem, source, passing, rounds, tiers, report)
    out = {"status": result.status, "stem": stem, "tiers": tier_info, "ir": passing, "report": report,
           "repairs": ir_diff(protocol, passing) if result.status == "HEALED" else ""}
    return out


def _record(stem, source, ir, rounds, tiers, report) -> None:
    work = pc_paths.WORK
    # runs.record treats a one-line source as a path first (and a long line fails to stat): hand it a file
    given = pc_paths.BUILD / f"{stem}_input.txt"
    given.parent.mkdir(parents=True, exist_ok=True)
    given.write_text(source)
    pc_runs.record(stem, str(given), ir, rounds, tiers, True, (work / report["files"]["summary"]).read_text(), report,
                   script=(work / report["files"]["script"]).read_text(), layout=work / report["files"]["ahc_layout"])


def translate_sop(sop_text: str, plate_map: Optional[Dict[str, Any]], stem: str, emit: Emit) -> Dict[str, Any]:
    """SOP (+ the assay plate map as context) -> healed protocol whose deck AHC loads. Slow: drafting and
    repair call Claude."""
    source = sop_text.strip() + ("\n" + lanm.assay_context(plate_map) if plate_map else "")
    base_deck, deck_file = _base_deck()
    out = _Lines(emit)
    with contextlib.redirect_stdout(out):
        emit("[draft] Claude is drafting the protocol IR, choosing labware AHC can load from the VENUS library")
        d = draft(source, base_deck, deck_file=deck_file, out=out, stem=stem)
        emit(f"[draft] {len(d.protocol['steps'])} steps drafted; compiling, simulating and repairing")
        result = _heal_and_finish(d.protocol, d.path.stem, source, emit)
        return reconcile(result, emit, can_revise=True, plate_map=plate_map)


def _revise_raw(current: Dict[str, Any], current_stem: str, note: str, emit: Emit) -> Dict[str, Any]:
    base_deck, deck_file = _base_deck()
    out = _Lines(emit)
    stem = next_stem(current_stem)
    with contextlib.redirect_stdout(out):
        d = revise(current, note, base_deck, current_name=f"{current_stem}.json", deck_file=deck_file, out=out,
                   stem=stem)
        result = _heal_and_finish(d.protocol, d.path.stem, note, emit)
    if result.get("ir") is not None:
        result["changes"] = ir_diff(current, result["ir"]) or "(no step or volume changes)"
    return result


def revise_protocol(current: Dict[str, Any], current_stem: str, note: str, emit: Emit,
                    plate_map: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The scientist's change in words -> the next version (<stem>_r2, ...), healed and labware-checked again."""
    emit(f"[revise] {note}")
    result = _revise_raw(current, current_stem, note, emit)
    return reconcile(result, emit, can_revise=True, plate_map=plate_map)


def load_fixture_protocol(sop_text: str, stem: str, emit: Emit,
                          plate_map: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Offline: PreCheck's passing 1:10 serial-dilution fixture (drafted earlier from the same input.txt), checked
    by the gate again and written out by `finish` exactly as a healed protocol would be."""
    ir = json.loads((config.PRECHECK_FIXTURES / "serial_passing.json").read_text())
    decks = pc_paths.WORK / "decks"
    decks.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(config.PRECHECK_FIXTURES / "serial_deck.json", decks / "fixture_serial_deck.json")
    ir["deck_config"] = "decks/fixture_serial_deck.json"
    ir["source"] = {"name": "input", "text": sop_text}
    emit("[offline] using PreCheck's passing serial-dilution fixture (no drafting or repair); re-checking it on the "
         "STAR model")
    deck_config = deck_config_for(ir, pc_paths.PLUGIN)
    report = finish(ir, deck_config, stem, [], status="PASSED")
    _record(stem, sop_text, ir, [], [], report)
    result = {"status": "PASSED", "stem": stem, "tiers": [], "ir": ir, "report": report, "repairs": "",
              "offline": True}
    return reconcile(result, emit, can_revise=False, plate_map=plate_map)


def guarded(ir: Dict[str, Any], candidate: Dict[str, Any], deck_config: Dict[str, Any], stem: str) -> Dict[str, Any]:
    """PreCheck's judgment of an edit: its gate (compile + simulate on the instrument's model) and its intent guard
    (does any plate well end up holding something else than before the edit)."""
    healer = SelfHealer(deck_config=deck_config, work_dir=str(pc_paths.BUILD / "argus_guard" / stem))
    gate = healer.evaluate(candidate, 0)
    changed = healer._intent_violations(healer._transfers(ir), healer._transfers(candidate))
    return {"gate_passed": gate.passed, "gate": gate.summary, "intent_changes": changed}


def reconcile(result: Dict[str, Any], emit: Emit, can_revise: bool, max_rounds: int = 3,
              plate_map: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """AHC runs PreCheck's protocol (a dry run on its simulator) before anyone reviews it. What AHC refuses, it
    suggests a fix for, and the fix goes back through PreCheck: an edit that leaves every well as planned is applied
    when PreCheck's gate and intent guard pass it; one that changes the experiment goes to PreCheck as a revision
    (redrafted, healed under the intent guard, shown to the scientist). A protocol AHC still refuses is NOT_READY."""
    rounds: List[Dict[str, Any]] = []
    clean = False
    last_problem = None
    for n in range(1, max_rounds + 1):
        if result.get("ir") is None:
            break
        ir, report = result["ir"], result["report"]
        deck_config = deck_config_for(ir, pc_paths.PLUGIN)
        emit(f"[ahc] round {n}: dry run of {result['stem']} on AHC's simulated STARlet")
        check = labware.check_with_ahc(ir, report, deck_config, layout_path(result))
        dry = check["dry_run"]
        entry = {"round": n, "version": result["stem"], "ahc_loaded": check["loaded"], "ahc_refusal": check["refusal"],
                 "dry_run": {"ok": dry["ok"], "calls": dry["calls"], "refusal": dry["refusal"]},
                 "suggestion": check["suggestion"], "not_on_layout": check["not_on_layout"], "depth": check["depth"],
                 "roles": check["roles"], "actions": []}
        design = labware.design_problems(ir, plate_map)
        entry["design_problems"] = design
        rounds.append(entry)
        for d in design:
            emit(f"[argus] {d}")
        emit("[ahc] " + (f"AHC ran all {dry['calls']} calls" if dry["ok"] else
                         f"AHC refused after {dry['calls']} calls: {(dry['refusal'] or {}).get('message', '')}")
             + (f"; {len(check['not_on_layout'])} item(s) not on its layout" if check["not_on_layout"] else "")
             + "".join(f"; '{d['labware']}' ends {d['end_mm']:.1f} mm deep" for d in check["depth"] if d["shallow"]))
        if check["clean"] and not design:
            clean = True
            entry["actions"].append(f"AHC ran every step ({dry['calls']} calls) on this deck, and every reservoir keeps "
                                    "at least 2 mm of liquid to the end.")
            break

        # AHC's fixes, each judged by PreCheck before it is applied
        fixes: Dict[str, float] = dict(labware.fill_fixes(check))
        sug = check["suggestion"]
        if sug and sug["kind"] == "load":
            fixes.update(sug["loads"])
        applied, needs_revision = {}, []
        for key, vol in fixes.items():
            cand = copy.deepcopy(ir)
            cand["initial_volumes_ul"][key] = vol
            verdict = guarded(ir, cand, deck_config, result["stem"])
            was = ir["initial_volumes_ul"].get(key)
            what = f"load {vol:g} uL in {'each of ' if ':' in key else ''}{key}" + (f" (was {was:g})" if was else "")
            if verdict["gate_passed"] and not verdict["intent_changes"]:
                applied[key] = vol
                ir = cand
                entry["actions"].append(f"Applied AHC's fix: {what}. PreCheck's gate and intent guard pass it "
                                        "(every well ends as planned).")
                emit(f"[ahc] applied: {what} (PreCheck gate + intent guard pass)")
            else:
                why = (f"PreCheck's gate refuses it: {verdict['gate']}" if not verdict["gate_passed"] else
                       "PreCheck's intent guard says it changes the experiment: "
                       + "; ".join(verdict["intent_changes"][:3])
                       + ("; ..." if len(verdict["intent_changes"]) > 3 else ""))
                needs_revision.append(f"{what}. {why}")
                entry["actions"].append(f"Not applied as a repair: {what}. {why}.")
                emit(f"[ahc] not a silent repair: {what} ({why[:160]})")
        if applied:
            report = finish(ir, deck_config, result["stem"], [], status=result["status"])
            _record(result["stem"], json.dumps(applied), ir, [], [], report)
            result = {**result, "ir": ir, "report": report}

        problems = labware.structural_problems(check, fixes)
        problems += [f"ARGUS's assay plate-map check: {d}" for d in design]
        if sug and sug["kind"] == "load":
            problems = [p for p in problems if not p.startswith("AHC's simulator refused step")]
            if needs_revision:
                problems.append(f"AHC's simulator refused step {sug['ir_step']}: {sug['ahc_said']} AHC suggests: "
                                f"{sug['text']}. As a change to the plan: " + " ".join(needs_revision))
        last_problem = "; ".join(problems) or last_problem
        if not problems:
            continue  # AHC runs the fixed protocol again
        if not can_revise:
            entry["actions"].append("Left for the scientist (offline: no redraft): " + "; ".join(problems))
            emit("[ahc] offline, so these stay as review items: " + "; ".join(problems))
            break
        note = labware.revision_note(problems)
        entry["actions"].append("Sent back to PreCheck as a revision (redraft, heal under the intent guard): "
                                + "; ".join(problems))
        emit("[ahc] sending AHC's findings back to PreCheck for a revision")
        revised = _revise_raw(result["ir"], result["stem"], note, emit)
        if revised.get("ir") is None:
            entry["actions"].append(f"PreCheck's revision did not pass its simulator: {revised.get('failure')}")
            last_problem = revised.get("failure")
            break
        revised["tiers"] = result.get("tiers", []) + revised.get("tiers", [])
        revised["changes"] = "\n".join(filter(None, [result.get("changes"), revised.get("changes")]))
        result = {**revised, "offline": result.get("offline", False)}
    accepts = labware.ahc_accepts()
    result["labware_check"] = {"rounds": rounds, "clean": clean, "accepts": accepts}
    last = rounds[-1] if rounds else None
    if result.get("ir") is not None and last and not last["dry_run"]["ok"]:
        # PreCheck passed it, AHC does not run it: it does not go to review as ready
        result = {**result, "status": "NOT_READY", "ahc_blocked": True,
                  "failure": "AHC's simulator still refuses it: " + ((last["dry_run"]["refusal"] or {}).get("message")
                                                                     or last_problem or "")}
    return result


# ----------------------------------------------------------------------------------------------- readable steps

def _label(ir: Dict[str, Any], lid: str) -> str:
    reagent = (ir.get("reagents") or {}).get(lid)
    if reagent:
        return reagent
    return lid.replace("_", " ")


def _well_label(ir: Dict[str, Any], lid: str, column: Optional[int]) -> str:
    """What a source plate's column holds, from the reagent named for its pre-filled wells."""
    from pipeline.design_review import well_reagent
    reagents = ir.get("reagents") or {}
    for key in (ir.get("initial_volumes_ul") or {}):
        if key.split(":")[0] == lid:
            return well_reagent(reagents, key, lid, column or 1, 0)
    return reagents.get(lid, lid.replace("_", " "))


def _cols(cols: List[int]) -> str:
    cols = sorted(set(cols))
    if len(cols) == 1:
        return f"column {cols[0]}"
    if cols == list(range(cols[0], cols[-1] + 1)):
        return f"columns {cols[0]}–{cols[-1]}"
    return "columns " + ", ".join(map(str, cols))


def _rows(channels: List[int]) -> str:
    rows = [ROWS[c] for c in sorted(channels) if c < len(ROWS)]
    if not rows:
        return ""
    return f"rows {rows[0]}–{rows[-1]}" if len(rows) > 1 else f"row {rows[0]}"


def _ops(segment: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """aspirate/dispense pairs as mixes or transfers; other actions as they are."""
    ops, i = [], 0
    while i < len(segment):
        s = segment[i]
        nxt = segment[i + 1] if i + 1 < len(segment) else None
        if s["action"] == "aspirate" and nxt and nxt["action"] == "dispense":
            same = (s.get("labware"), s.get("column")) == (nxt.get("labware"), nxt.get("column"))
            ops.append({"kind": "mix" if same else "transfer", "src": s, "dst": nxt, "steps": [s["step"], nxt["step"]]})
            i += 2
            continue
        ops.append({"kind": s["action"], "src": s, "dst": None, "steps": [s["step"]]})
        i += 1
    return ops


def _segments(ir: Dict[str, Any]) -> List[List[Dict[str, Any]]]:
    segs, cur = [], []
    for s in ir.get("steps", []):
        cur.append(s)
        if s["action"] == "tips_off":
            segs.append(cur)
            cur = []
    if cur:
        segs.append(cur)
    return segs


def readable_steps(ir: Dict[str, Any], plate_map: Optional[Dict[str, Any]] = None,
                   sop_text: str = "") -> List[Dict[str, Any]]:
    """The IR as numbered plain-language steps, one per tip cycle, each linked to its IR steps, the SOP clause it
    carries out, and (with a plate map) the concentrations it produces."""
    kinds = {lid: spec.get("kind") for lid, spec in (ir.get("labware") or {}).items()}
    diluent = lanm.diluent_names(ir)
    conc = lanm.concentrations_from_protocol(ir, plate_map)["wells"] if plate_map else {}
    variants = {r["row"]: r for r in (plate_map or {}).get("rows", [])}
    clauses = _clauses(sop_text)
    steps = []
    for seg in _segments(ir):
        ops = _ops(seg)
        tips = next((o["src"] for o in ops if o["kind"] == "tips_on"), None)
        details, phrases, shorts, kinds_used = [], [], [], set()
        filled_cols: List[int] = []
        if tips:
            details.append(f"Pick up {len(tips.get('channels', []))} fresh tips"
                           + (f" (tip rack column {tips['column']})" if tips.get("column") else ""))
        j = 0
        liquid = [o for o in ops if o["kind"] not in ("tips_on", "tips_off")]
        while j < len(liquid):
            o = liquid[j]
            if o["kind"] == "transfer":
                src, dst = o["src"], o["dst"]
                group = [o]
                while (j + len(group) < len(liquid) and liquid[j + len(group)]["kind"] == "transfer"
                       and liquid[j + len(group)]["src"].get("labware") == src.get("labware")
                       and liquid[j + len(group)]["src"].get("column") == src.get("column")
                       and kinds.get(src.get("labware")) != "plate"
                       and liquid[j + len(group)]["src"]["volume_ul"] == src["volume_ul"]):
                    group.append(liquid[j + len(group)])
                dst_cols = [g["dst"].get("column") for g in group if g["dst"].get("column")]
                vol = src["volume_ul"]
                rows = _rows(dst.get("channels", []))
                if kinds.get(src.get("labware")) == "plate" and src.get("labware") != dst.get("labware"):
                    kinds_used.add("sample")       # from a pre-filled source plate: each row its own sample
                    label = re.sub(r"\s*[(:].*$", "", _well_label(ir, src["labware"], src.get("column")))
                    who = " — each row gets its own LanM variant stock" if variants else ""
                    phrase = (f"add {vol:g} µL of {label} from {src['labware'].replace('_', ' ')} column "
                              f"{src.get('column')} to {_cols(dst_cols)} ({rows}){who}")
                    shorts.append(f"add {vol:g} µL {'variant stock' if variants else label} to {_cols(dst_cols)}")
                    filled_cols += dst_cols
                elif kinds.get(src.get("labware")) == "plate":
                    kinds_used.add("serial")
                    phrase = (f"move {vol:g} µL from column {src['column']} into {_cols(dst_cols)} "
                              f"({rows}) — the next dilution step")
                    filled_cols += dst_cols
                    shorts.append(f"transfer {vol:g} µL into {_cols(dst_cols)}")
                else:
                    label = _label(ir, src["labware"])
                    short_label = re.sub(r"\s*\(.*?\)", "", label)
                    if label in diluent:
                        kinds_used.add("diluent")
                        phrase = f"add {vol:g} µL of {label} to {_cols(dst_cols)} ({rows})"
                        shorts.append(f"fill {_cols(dst_cols)} with {vol:g} µL {short_label}")
                    else:
                        kinds_used.add("sample")
                        who = " — each row gets its own LanM variant stock" if variants else ""
                        phrase = f"add {vol:g} µL of {label} to {_cols(dst_cols)} ({rows}){who}"
                        filled_cols += dst_cols
                        shorts.append(f"add {vol:g} µL {'variant stock' if variants else short_label} to {_cols(dst_cols)}")
                phrases.append(phrase)
                details.append(phrase[0].upper() + phrase[1:] + f"  [IR steps {group[0]['steps'][0]}–{group[-1]['steps'][-1]}]")
                j += len(group)
            elif o["kind"] == "mix":
                group = [o]
                while (j + len(group) < len(liquid) and liquid[j + len(group)]["kind"] == "mix"
                       and liquid[j + len(group)]["src"].get("column") == o["src"].get("column")):
                    group.append(liquid[j + len(group)])
                kinds_used.add("mix")
                n = len(group)
                phrase = (f"mix column {o['src'].get('column')} by pipetting {o['src']['volume_ul']:g} µL up and down"
                          + (f" {n}×" if n > 1 else ""))
                phrases.append(phrase)
                shorts.append(f"mix column {o['src'].get('column')}")
                details.append(phrase[0].upper() + phrase[1:] + f"  [IR steps {group[0]['steps'][0]}–{group[-1]['steps'][-1]}]")
                j += n
            else:
                s = o["src"]
                kinds_used.add(o["kind"])
                phrase = s.get("description") or f"{o['kind']} {s.get('labware', '')}"
                phrases.append(phrase[0].lower() + phrase[1:])
                shorts.append(phrase[0].lower() + phrase[1:])
                details.append(phrase + f"  [IR step {s['step']}]")
                j += 1
        if any(o["kind"] == "tips_off" for o in ops):
            details.append("Discard the tips")
        title = ", then ".join(shorts) if shorts else "Tip handling"
        title = title[0].upper() + title[1:]
        concentrations = []
        for c in sorted(set(filled_cols)):
            vals = [(r, conc.get(f"{r}{c}", {}).get("uM")) for r in ROWS if f"{r}{c}" in conc]
            vals = [(r, v) for r, v in vals if v is not None and r in variants]
            if vals:
                concentrations.append({
                    "column": c,
                    "range": f"{lanm.format_uM(min(v for _, v in vals))} – {lanm.format_uM(max(v for _, v in vals))}",
                    "by_row": {r: lanm.format_uM(v) for r, v in vals},
                })
        ir_steps = [s["step"] for s in seg]
        clause = _best_clause(clauses, kinds_used, title)
        steps.append({
            "n": len(steps) + 1, "title": title, "details": details, "kinds": sorted(kinds_used),
            "ir_steps": [ir_steps[0], ir_steps[-1]], "concentrations": concentrations, "sop": clause,
        })
    return steps


KIND_WORDS = {
    "diluent": ["saline", "solvent", "solution", "sterilized", "diluent", "buffer", "normal"],
    "sample": ["culture", "sample", "stock", "1ml", "1 ml", "obtained", "growth"],
    "serial": ["serially", "dilute", "dilution", "ratio", "1: 10", "1:10", "10ml"],
    "mix": ["mix", "dilute", "diluted"],
}


def _clauses(text: str) -> List[Dict[str, Any]]:
    out = []
    for m in re.finditer(r"[^.;()]+", text or ""):
        span = m.group(0)
        if len(span.strip()) > 3:
            lead = len(span) - len(span.lstrip())
            out.append({"start": m.start() + lead, "end": m.end(), "text": span.strip()})
    return out


def _best_clause(clauses: List[Dict[str, Any]], kinds_used: set, title: str) -> Optional[Dict[str, Any]]:
    words = [w for k in kinds_used for w in KIND_WORDS.get(k, [])]
    words += [w for w in re.findall(r"[a-z]{5,}", title.lower())]
    best, score = None, 0
    for c in clauses:
        low = c["text"].lower()
        s = sum(low.count(w) for w in words)
        if s > score:
            best, score = c, s
    return best


# ----------------------------------------------------------------------------------------------- packaging

def package(result: Dict[str, Any], plate_map: Optional[Dict[str, Any]], sop_text: str) -> Dict[str, Any]:
    """Everything the review panel and the agent need from a PreCheck result, with file contents inlined."""
    if result.get("ir") is None:
        return result
    ir, report = result["ir"], result["report"]
    work = pc_paths.WORK
    files = report["files"]
    review_items = list(report.get("needs_review", []))
    if result.get("offline") and plate_map:
        review_items.insert(0, {
            "severity": "must", "area": "drafting", "item": "Offline fixture: one shared sample reservoir",
            "detail": "The offline fixture draws the sample for all 8 rows from one reservoir. Per-row LanM variant "
                      "stocks need a per-row source (live drafting with the assay context). Concentrations shown "
                      "assume each row's stock is at its planned starting concentration."})
    setup, model_notes, flags = split_review_items(review_items)
    out = {
        "status": result["status"], "stem": result["stem"], "tiers": result.get("tiers", []),
        "repairs": result.get("repairs", ""), "changes": result.get("changes"),
        "protocol_name": ir.get("protocol_name"), "description": ir.get("description"),
        "assumptions": ir.get("assumptions", []), "n_ir_steps": len(ir.get("steps", [])),
        "needs_review": flags, "deck_setup": setup, "model_notes": model_notes,
        "labware_check": result.get("labware_check"), "suggestions": report.get("suggestions", []),
        "labware": report["review"].get("labware", []), "reagents": report["review"].get("reagents", []),
        "ahc": report.get("ahc", {}),
        "files": {k: str(work / v) for k, v in files.items()},
        "plr_script": (work / files["script"]).read_text(),
        "ahc_layout_yaml": (work / files["ahc_layout"]).read_text(),
        "readable_steps": readable_steps(ir, plate_map, sop_text),
        "offline": bool(result.get("offline")),
    }
    return out


def split_review_items(items: List[Dict[str, Any]]):
    """PreCheck's needs_review, sorted for the scientist: deck setup is a checklist (placing carriers and labware is a
    task, not a defect), AHC-vs-VENUS geometry notes are reference, drafting assumptions are already listed with the
    protocol. What is left are the flags to decide on."""
    setup, notes, flags = [], [], []
    for i in items:
        item = i.get("item", "")
        if i.get("area") == "deck" and item.startswith("Place on the deck"):
            setup.append(i["detail"])
        elif i.get("area") == "AHC" and item.startswith("AHC's model differs"):
            notes.append(i["detail"])
        elif i.get("area") == "drafting" and item.startswith("Assumption made when drafting"):
            continue
        else:
            flags.append(i)
    return setup, notes, flags


def ir_of(result: Dict[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(result["ir"])


def layout_path(result: Dict[str, Any]) -> Path:
    return pc_paths.WORK / result["report"]["files"]["ahc_layout"]
