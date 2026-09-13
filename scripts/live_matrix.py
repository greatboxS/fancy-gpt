"""Run every site on every healthy tunnel at once, and report what broke.

A browser turn is slow, and a reload cycle costs one too. Finding one defect
per cycle -- which is what running turns by hand does -- turned a single day
into about fifteen of them, each ending in a reload and a guess.

This runs the whole matrix concurrently instead: every site that has an
adapter, on every tunnel with a live worker, in parallel where the runtime
allows it. One pass, one table, every failure at once.

Parallelism is real across tunnels and only across tunnels: the runtime holds
one lock per tunnel for the length of a turn, so two sites on the same browser
queue behind each other rather than interleaving in one window.

    PYTHONPATH=src python scripts/live_matrix.py
    PYTHONPATH=src python scripts/live_matrix.py --sites chatgpt --question "..."
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fancy_gpt.execution import ExecutionCoordinator, ExecutionFailed  # noqa: E402
from fancy_gpt.focused import FocusedQuestion  # noqa: E402
from fancy_gpt.tunnels.manager import TunnelManager  # noqa: E402

DEFAULT_QUESTION = "In one sentence: what is a hash collision?"


def healthy_tunnels(manager: TunnelManager) -> list[str]:
    return [probe.tunnel_id for probe in manager.health() if probe.browser_connected]


def run_one(root: Path, manager: TunnelManager, site: str, tunnel: str, question: str) -> dict:
    started = time.monotonic()
    coordinator = ExecutionCoordinator(root, manager=manager)
    try:
        answer = coordinator.run_focused(
            FocusedQuestion(question=question, site=site), tunnel_id=tunnel
        )
        return {
            "site": site, "tunnel": tunnel, "ok": True,
            "seconds": round(time.monotonic() - started, 1),
            "chars": len(answer.answer),
        }
    except ExecutionFailed as failure:
        error = failure.status.error
        return {
            "site": site, "tunnel": tunnel, "ok": False,
            "seconds": round(time.monotonic() - started, 1),
            "layer": error.layer if error else "?",
            "error": (error.message if error else "")[:160],
        }
    except Exception as exc:  # noqa: BLE001 - the point is to survive and report
        return {
            "site": site, "tunnel": tunnel, "ok": False,
            "seconds": round(time.monotonic() - started, 1),
            "layer": "runner", "error": f"{type(exc).__name__}: {exc}"[:160],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sites", default="chatgpt,gemini")
    parser.add_argument("--tunnels", default="", help="default: every tunnel with a live worker")
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--root", default=os.getenv("FANCY_GPT_WORKDIR", ".fancy-gpt"))
    args = parser.parse_args()

    manager = TunnelManager()
    tunnels = [t for t in args.tunnels.split(",") if t] or healthy_tunnels(manager)
    if not tunnels:
        print("no tunnel has a live worker; start the bridge and reload the extension")
        return 2
    sites = [s for s in args.sites.split(",") if s]
    root = Path(args.root)

    combinations = [(site, tunnel) for tunnel in tunnels for site in sites]
    print(f"running {len(combinations)} turns across {len(tunnels)} tunnel(s)\n")

    # One worker per tunnel: more would only queue on the runtime's own lock.
    with ThreadPoolExecutor(max_workers=len(tunnels)) as pool:
        results = list(pool.map(
            lambda pair: run_one(root, manager, pair[0], pair[1], args.question), combinations
        ))

    width = max(len(f"{r['site']}/{r['tunnel']}") for r in results)
    failures = 0
    for result in sorted(results, key=lambda r: (r["site"], r["tunnel"])):
        name = f"{result['site']}/{result['tunnel']}".ljust(width)
        if result["ok"]:
            print(f"  ok    {name}  {result['seconds']:>5}s  {result['chars']} chars")
        else:
            failures += 1
            print(f"  FAIL  {name}  {result['seconds']:>5}s  [{result['layer']}] {result['error']}")

    print()
    print(json.dumps({"turns": len(results), "failed": failures}, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
