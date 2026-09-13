"""Run every site on every healthy tunnel at once, and report what broke.

A browser turn is slow, and a reload cycle costs one too. Finding one defect
per cycle -- which is what running turns by hand does -- turned a single day
into about fifteen of them, each ending in a reload and a guess.

This runs the whole matrix concurrently instead: every site that has an
adapter, on every tunnel with a live worker, in parallel where the runtime
allows it. One pass, one table, every failure at once.

Each matrix cell is submitted independently. Turns on the same tunnel therefore
exercise the extension's surface capacity and turns on different tunnels also
overlap; this is both a compatibility smoke test and a real concurrency gate.

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

# One reload costs a round trip, so a run should learn as much as it can.
# Each scenario exercises a different part of a turn, and they are cheap next
# to the reload they share.
SCENARIOS = {
    "short": "In one sentence: what is a hash collision?",
    "long": (
        "In about 200 words, explain how a B-tree differs from a binary search "
        "tree, covering node fan-out, height, disk locality and when each is "
        "preferred."
    ),
    "fenced": (
        "Answer normally, but put your entire JSON reply inside a ```json "
        "fenced code block. Question: in one sentence, what is a race condition?"
    ),
    "quoted": (
        'In one sentence, explain what the string "world: "MAIN"" would do to a '
        "JSON parser if the inner quotes were not escaped."
    ),
}


def healthy_tunnels(manager: TunnelManager) -> list[str]:
    return [probe.tunnel_id for probe in manager.health() if probe.browser_connected]


def diagnostics_for(root: Path, request_id: str) -> dict:
    """The shape report a turn left behind, found by the request it belongs to."""
    executions = root / "executions"
    for record in sorted(executions.glob("exec-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if record.name.endswith(".diagnostics.json"):
            continue
        try:
            if json.loads(record.read_text()).get("request_id") != request_id:
                continue
        except (OSError, ValueError):
            continue
        sidecar = record.with_suffix("").with_suffix(".diagnostics.json")
        sidecar = executions / f"{record.stem}.diagnostics.json"
        if sidecar.exists():
            try:
                return json.loads(sidecar.read_text())
            except (OSError, ValueError):
                return {}
        return {}
    return {}


def check_stream(site: str, diagnostics: dict, page_text: str) -> str:
    """Whether the stream reading agrees with the page reading.

    Compared as parsed payloads rather than byte for byte: the stream carries
    the fence the model wrote and the rendered page loses it, so the two differ
    in a way that means nothing. What matters is whether they say the same.
    """
    from fancy_gpt.response_parser import parse_json_object
    from fancy_gpt.stream_decoding import decode_stream

    captures = diagnostics.get("streams") or []
    bodies = diagnostics.get("streamBodies") or []
    if not captures and not bodies:
        return "no stream"
    decoded = (
        decode_stream(site, captures=captures) if captures else decode_stream(site, bodies=bodies)
    )
    if decoded is None:
        return "no decoder"
    if not decoded.trustworthy:
        return f"untrusted ({len(decoded.unknown_ops or [])} unknown ops, {decoded.skipped_text} lost)"
    try:
        return "agrees" if parse_json_object(decoded.text) == parse_json_object(page_text) else "DIFFERS"
    except ValueError as exc:
        return f"unparseable: {exc}"


def run_one(root: Path, manager: TunnelManager, site: str, tunnel: str,
            scenario: str, question: str) -> dict:
    started = time.monotonic()
    coordinator = ExecutionCoordinator(root, manager=manager)
    try:
        answer = coordinator.run_focused(
            FocusedQuestion(question=question, site=site), tunnel_id=tunnel
        )
        diagnostics = diagnostics_for(root, answer.request_id)
        page_text = ""
        executions = root / "executions"
        for record in sorted(executions.glob("exec-*.response.txt"), key=lambda p: p.stat().st_mtime, reverse=True)[:6]:
            text = record.read_text(encoding="utf-8")
            if answer.request_id in text:
                page_text = text
                break
        return {
            "site": site, "tunnel": tunnel, "scenario": scenario, "ok": True,
            "seconds": round(time.monotonic() - started, 1),
            "chars": len(answer.answer),
            "stream": check_stream(site, diagnostics, page_text) if page_text else "no page text",
            "others": diagnostics.get("otherSites") or [],
        }
    except ExecutionFailed as failure:
        error = failure.status.error
        return {
            "site": site, "tunnel": tunnel, "scenario": scenario, "ok": False,
            "seconds": round(time.monotonic() - started, 1),
            "layer": error.layer if error else "?",
            "error": (error.message if error else "")[:160],
            "others": [],
        }
    except Exception as exc:  # noqa: BLE001 - the point is to survive and report
        return {
            "site": site, "tunnel": tunnel, "scenario": scenario, "ok": False,
            "seconds": round(time.monotonic() - started, 1),
            "layer": "runner", "error": f"{type(exc).__name__}: {exc}"[:160],
            "others": [],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sites", default="chatgpt,gemini")
    parser.add_argument("--tunnels", default="", help="default: every tunnel with a live worker")
    parser.add_argument("--scenarios", default=",".join(SCENARIOS))
    parser.add_argument("--root", default=os.getenv("FANCY_GPT_WORKDIR", ".fancy-gpt"))
    parser.add_argument("--max-workers", type=int, default=0, help="default: one worker per matrix cell")
    args = parser.parse_args()

    manager = TunnelManager()
    tunnels = [t for t in args.tunnels.split(",") if t] or healthy_tunnels(manager)
    if not tunnels:
        print("no tunnel has a live worker; start the bridge and reload the extension")
        return 2
    sites = [s for s in args.sites.split(",") if s]
    scenarios = [s for s in args.scenarios.split(",") if s in SCENARIOS]
    root = Path(args.root)

    work = [(site, tunnel, name) for tunnel in tunnels for site in sites for name in scenarios]
    total = len(work)
    print(f"{total} turns: {len(sites)} site(s) x {len(scenarios)} scenario(s) x {len(tunnels)} tunnel(s)\n")

    max_workers = args.max_workers or total
    if max_workers < 1:
        parser.error("--max-workers must be positive")
    with ThreadPoolExecutor(max_workers=min(max_workers, total)) as pool:
        results = list(pool.map(
            lambda item: run_one(root, manager, item[0], item[1], item[2], SCENARIOS[item[2]]),
            work,
        ))

    width = max(len(f"{r['site']}/{r['tunnel']}/{r['scenario']}") for r in results)
    failures = 0
    for result in sorted(results, key=lambda r: (r["site"], r["scenario"], r["tunnel"])):
        name = f"{result['site']}/{result['tunnel']}/{result['scenario']}".ljust(width)
        if result["ok"]:
            flag = " " if result["stream"] in ("agrees", "no decoder", "no stream") else "!"
            print(f" {flag}ok   {name}  {result['seconds']:>5}s  {result['chars']:>5} chars  stream: {result['stream']}")
        else:
            failures += 1
            print(f"  FAIL {name}  {result['seconds']:>5}s  [{result['layer']}] {result['error']}")

    # Sites we watch but do not drive report through whichever turn ran next.
    seen: dict[tuple, dict] = {}
    for result in results:
        for observation in result["others"]:
            seen[(observation.get("origin"), observation.get("kind"), observation.get("path"))] = observation
    if seen:
        print("\nwatched elsewhere:")
        for observation in seen.values():
            detail = " ".join(
                f"{key}={observation[key]}"
                for key in ("chars", "chunks", "frames", "progressive", "sawDone")
                if observation.get(key) is not None
            )
            print(f"  {observation.get('origin')}  {observation.get('kind'):6} {str(observation.get('path'))[:52]:52} {detail}")
    else:
        print("\nwatched elsewhere: nothing seen")

    disagreements = [r for r in results if r.get("ok") and r["stream"] not in ("agrees", "no decoder", "no stream")]
    print()
    print(json.dumps({"turns": len(results), "failed": failures, "stream_disagreements": len(disagreements)}, indent=2))
    return 1 if failures or disagreements else 0


if __name__ == "__main__":
    raise SystemExit(main())
