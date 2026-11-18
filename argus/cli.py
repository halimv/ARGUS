"""argus serve                 the web app (http://127.0.0.1:8780)
argus demo --offline        the whole chain in the terminal with no model: rank, assay concentration mapping, fixture protocol,
                            auto-approval as "offline-demo", AHC simulation
"""

import argparse
import asyncio
import json
import os
from pathlib import Path


def _demo(prompt: str) -> int:
    from argus import config
    from argus.agent import drive_offline
    from argus.run import Run

    async def go():
        run = Run(prompt, config.DEFAULT_SOP.read_text(), config.DEFAULT_SOP.name, offline=True)
        task = asyncio.create_task(drive_offline(run))
        seen = 0
        while not task.done():
            await asyncio.sleep(0.2)
            for e in run.events[seen:]:
                d = e["data"] or {}
                if e["type"] == "agent_text":
                    print(f"\nARGUS: {d['text']}")
                elif e["type"] == "tool_start":
                    print(f"  -> {d['name']}({json.dumps(d['input'])})")
                elif e["type"] == "tool_end":
                    print(f"  <- {d['name']} {'ok' if d['ok'] else 'FAILED: ' + d.get('error', '')} ({d['seconds']}s)")
                elif e["type"] == "progress":
                    print(f"     {d['line']}")
                elif e["type"] == "review_requested":
                    proto = run.state["protocol"]
                    print("\n--- Readable steps for review ---")
                    for s in proto["readable_steps"]:
                        print(f"  {s['n']}. {s['title']}")
                    print("(demo: approving as 'offline-demo')")
                    await run.inbox.put(await run.decide("approve", "offline-demo", "terminal demo"))
            seen = len(run.events)
        await task
        sim = run.state.get("simulation") or {}
        print(f"\nRun folder: {run.folder}\nSimulation passed: {sim.get('passed')}")
        return 0 if sim.get("passed") else 1

    return asyncio.run(go())


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="argus")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="start the web app")
    s.add_argument("--port", type=int, default=8780)
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--lanm-data-dir", type=Path,
                   help="use an existing LanM research-data directory without restoring over it")
    s.add_argument("--lanm-port", type=int, help="full LanM workspace port (default: a free loopback port)")
    d = sub.add_parser("demo", help="run the chain in the terminal")
    d.add_argument("--offline", action="store_true", required=True, help="no model (the only demo mode)")
    d.add_argument("--prompt", default="Top 8 LanM variants for Nd over La, serial dilution, simulate it.")
    args = parser.parse_args(argv)
    if args.cmd == "serve":
        if args.lanm_data_dir is not None:
            os.environ["ARGUS_LANM_DATA_DIR"] = str(args.lanm_data_dir.expanduser().resolve())
        if args.lanm_port is not None:
            if not 0 <= args.lanm_port <= 65535:
                parser.error("--lanm-port must be between 0 and 65535")
            os.environ["ARGUS_LANM_PORT"] = str(args.lanm_port)
        from argus.server import serve
        serve(port=args.port, open_browser=not args.no_browser)
    else:
        raise SystemExit(_demo(args.prompt))


if __name__ == "__main__":
    main()
