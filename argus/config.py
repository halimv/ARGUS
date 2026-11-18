"""Where ARGUS finds its three systems and where it writes.

The submodules are read only. Everything a run writes goes under ARGUS/workspace/:
  workspace/lanm/        LanM's SQLite store (ranking runs, projects)
  workspace/precheck/    PreCheck's working folder (drafts/, decks/, build/, healed/, runs/, knowledge/)
  workspace/runs/<id>/   one ARGUS run: its AHC task folder and a run.json snapshot

PreCheck reads PRECHECK_WORKDIR once, when `pipeline` is first imported, so `setup()` must run before any import of
PreCheck's code. All ARGUS modules call it at import.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LANM_SRC = ROOT / "automation-capstone-fall-2026" / "platform" / "src"
PRECHECK_PLUGIN = ROOT / "PreCheck" / "plugin"
PRECHECK_FIXTURES = ROOT / "PreCheck" / "tests" / "fixtures"
DEFAULT_SOP = ROOT / "PreCheck" / "files" / "input.txt"

WORKSPACE = Path(os.environ.get("ARGUS_WORKSPACE") or ROOT / "workspace").resolve()
LANM_DATA = WORKSPACE / "lanm"
PRECHECK_WORK = WORKSPACE / "precheck"
RUNS = WORKSPACE / "runs"

# The model that drives the ARGUS agent (the Claude CLI login is used; no API key).
AGENT_MODEL = os.environ.get("ARGUS_MODEL", "claude-opus-5-5")

# The live 3D view of AHC's simulated STARlet (PyLabRobot's Viewer3D, through AHC's LiveView), embedded in the
# Simulation tab. ARGUS_3D=0 turns it off (the tests do). SIM_PACE_S: a pause after each AHC call while the view is
# on, so the scientist can watch tips and liquid move (a simulated run otherwise ends in milliseconds).
VIEW_3D = os.environ.get("ARGUS_3D", "1") != "0"
SIM_PACE_S = float(os.environ.get("ARGUS_SIM_PACE_S", "0.5"))

_done = False


def setup() -> None:
    global _done
    if _done:
        return
    for folder in (LANM_DATA, PRECHECK_WORK, RUNS):
        folder.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("PRECHECK_WORKDIR", str(PRECHECK_WORK))
    for path in (LANM_SRC, PRECHECK_PLUGIN):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    _done = True


setup()
