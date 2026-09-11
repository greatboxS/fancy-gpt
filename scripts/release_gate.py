from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from fancy_gpt import __version__
from fancy_gpt.catalog import load_domains, load_skills, load_workflows
from fancy_gpt.skills import packaged_skills_root, validate_skill_bundle
from fancy_gpt.tunnels import TunnelLayerInspector, TunnelRegistry

root = Path(__file__).resolve().parents[1]


def run(command: list[str], *, cwd: Path = root, pythonpath: Path | None = root / "src") -> dict[str, object]:
    env = os.environ.copy()
    if pythonpath is not None:
        env["PYTHONPATH"] = str(pythonpath)
    completed = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True)
    return {"pass": completed.returncode == 0, "command": command,
            "stdout": completed.stdout.strip(), "stderr": completed.stderr.strip()}


registry = TunnelRegistry()
inspector = TunnelLayerInspector()
tunnel_layer_errors = {
    spec.id: [item.detail for item in inspector.inspect(spec) if item.state != "healthy"]
    for spec in registry.all()
}
tunnel_layer_errors = {key: value for key, value in tunnel_layer_errors.items() if value}

source_tests = run([sys.executable, "-m", "pytest", "-ra"])
functional_review = run([sys.executable, "scripts/functional_review.py"])
tunnel_review = run([sys.executable, "scripts/tunnel_review.py"])
extension_check = run([sys.executable, "scripts/build_extension.py", "--check"], pythonpath=None)

wheel = root / "dist" / f"fancy_gpt-{__version__}-py3-none-any.whl"
wheel_errors: list[str] = []
wheel_self_test: dict[str, object] = {"pass": False, "stdout": "", "stderr": "wheel missing"}
if wheel.is_file():
    with tempfile.TemporaryDirectory(prefix="fancy-gpt-release-gate-") as tmp_name:
        extracted = Path(tmp_name)
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(extracted)
        source_root = root / "src" / "fancy_gpt"
        packaged_root = extracted / "fancy_gpt"
        source_files = {item.relative_to(source_root).as_posix(): item.read_bytes()
                        for item in source_root.rglob("*")
                        if item.is_file() and "__pycache__" not in item.parts and item.suffix != ".pyc"}
        packaged_files = {item.relative_to(packaged_root).as_posix(): item.read_bytes()
                          for item in packaged_root.rglob("*") if item.is_file()}
        for name in sorted(set(source_files) | set(packaged_files)):
            if name not in source_files:
                wheel_errors.append(f"unexpected packaged file: {name}")
            elif name not in packaged_files:
                wheel_errors.append(f"missing packaged file: {name}")
            elif source_files[name] != packaged_files[name]:
                wheel_errors.append(f"stale packaged file: {name}")
        wheel_self_test = run(
            [sys.executable, "-c", "import json; from fancy_gpt.self_test import run_self_test; print(json.dumps(run_self_test()))"],
            cwd=extracted, pythonpath=extracted)
else:
    wheel_errors.append(f"missing wheel: {wheel.relative_to(root)}")

checks = {
    "version": __version__, "skills": len(load_skills()), "workflows": len(load_workflows()),
    "domains": len(load_domains()), "tunnels": len(registry.all()),
    "tunnel_layer_errors": tunnel_layer_errors,
    "skill_bundle_errors": validate_skill_bundle(packaged_skills_root()),
    "source_tests": source_tests, "functional_review": functional_review,
    "tunnel_review": tunnel_review, "extension_build": extension_check,
    "wheel": str(wheel.relative_to(root)), "wheel_errors": wheel_errors,
    "wheel_self_test": wheel_self_test,
    "schemas_exist": all((root / "schemas" / name).exists() for name in [
        "raw-request.schema.json", "research-manifest.schema.json", "context-pack.schema.json",
        "final-report.schema.json", "request-status.schema.json", "routing-decision.schema.json"]),
    "example_files_exist": all((root / name).exists() for name in [
        "examples/requests/qos-review.yaml", "examples/planner-result.example.json",
        "examples/final-result.template.json"]),
    "tunnel_architecture_doc": (root / "docs/TUNNEL_ARCHITECTURE.md").is_file(),
}
checks["pass"] = (
    checks["version"] == "0.7.0" and checks["skills"] == 6 and checks["workflows"] == 4
    and checks["domains"] == 13 and checks["tunnels"] == 10
    and not checks["tunnel_layer_errors"] and not checks["skill_bundle_errors"]
    and not source_tests["stderr"]
    and all(result["pass"] for result in [source_tests, functional_review, tunnel_review,
                                          extension_check, wheel_self_test])
    and not wheel_errors and checks["schemas_exist"] and checks["example_files_exist"]
    and checks["tunnel_architecture_doc"]
)
output = root / "dist" / "release-gate.json"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(checks, indent=2), encoding="utf-8")
print(json.dumps(checks, indent=2))
if not checks["pass"]:
    raise SystemExit(1)
