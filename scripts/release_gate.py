from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from fancy_gpt import __version__
from fancy_gpt.catalog import load_domains, load_skills, load_workflows
from fancy_gpt.skills import packaged_skills_root, validate_skill_bundle
from fancy_gpt.tunnels import TunnelLayerInspector, TunnelRegistry

ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str], *, env: dict[str, str] | None = None, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=merged,
        timeout=timeout,
        check=False,
    )


def source_package_files() -> set[str]:
    base = ROOT / "src" / "fancy_gpt"
    result: set[str] = set()
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(base).as_posix()
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if rel.startswith("extension_assets/") or rel.startswith("agent_skills/") or rel.startswith("catalog/") or path.suffix == ".py":
            result.add(rel)
    return result


def wheel_package_files(wheel: Path) -> set[str]:
    prefix = "fancy_gpt/"
    with zipfile.ZipFile(wheel) as zf:
        return {
            name[len(prefix):]
            for name in zf.namelist()
            if name.startswith(prefix) and not name.endswith("/")
        }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    registry = TunnelRegistry()
    inspector = TunnelLayerInspector()
    tunnel_layer_errors = {
        spec.id: [item.detail for item in inspector.inspect(spec) if item.state != "healthy"]
        for spec in registry.all()
    }
    tunnel_layer_errors = {key: value for key, value in tunnel_layer_errors.items() if value}

    pytest_result = run([sys.executable, "-m", "pytest", "-q"], timeout=180)
    functional = run([sys.executable, "scripts/functional_review.py"], env={"PYTHONPATH": str(ROOT / "src")})
    tunnel = run([sys.executable, "scripts/tunnel_review.py"], env={"PYTHONPATH": str(ROOT / "src")})
    extension = run([sys.executable, "scripts/build_extension.py", "--check"])

    wheel_path: Path | None = None
    wheel_build_ok = False
    wheel_parity_ok = False
    wheel_self_test_ok = False
    wheel_hash: str | None = None
    wheel_error = ""
    missing_in_wheel: list[str] = []
    extra_in_wheel: list[str] = []

    with tempfile.TemporaryDirectory(prefix="fancy-gpt-release-gate-") as td:
        temp = Path(td)
        wheel_dir = temp / "wheel"
        target = temp / "site"
        wheel_dir.mkdir()
        target.mkdir()
        build = run([
            sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--no-build-isolation", "-w", str(wheel_dir)
        ], timeout=180)
        wheels = sorted(wheel_dir.glob("fancy_gpt-*.whl"))
        if build.returncode == 0 and len(wheels) == 1:
            wheel_build_ok = True
            wheel_path = wheels[0]
            wheel_hash = sha256(wheel_path)
            source_files = source_package_files()
            wheel_files = wheel_package_files(wheel_path)
            missing_in_wheel = sorted(source_files - wheel_files)
            # dist-info is outside package prefix; inside-package extras should be intentional only.
            extra_in_wheel = sorted(wheel_files - source_files)
            wheel_parity_ok = not missing_in_wheel and not extra_in_wheel

            install = run([
                sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(target), str(wheel_path)
            ], timeout=120)
            if install.returncode == 0:
                code = (
                    "import json; "
                    "from fancy_gpt import __version__; "
                    "from fancy_gpt.self_test import run_self_test; "
                    "r=run_self_test(); "
                    "assert __version__=='0.8.0'; "
                    "assert r['ok'] and r['persistent_team_state']=='complete' and r['semantic_relevance_policy']=='enabled'; "
                    "print(json.dumps(r, sort_keys=True))"
                )
                env = {"PYTHONPATH": str(target)}
                smoke = run([sys.executable, "-c", code], env=env, timeout=120)
                wheel_self_test_ok = smoke.returncode == 0
                if not wheel_self_test_ok:
                    wheel_error = smoke.stderr or smoke.stdout
            else:
                wheel_error = install.stderr or install.stdout
        else:
            wheel_error = build.stderr or build.stdout

    # setuptools may leave local build metadata even when wheel output itself is
    # directed to a temporary directory. The release gate must not dirty the
    # repository merely by verifying it.
    shutil.rmtree(ROOT / "build", ignore_errors=True)
    shutil.rmtree(ROOT / "src" / "fancy_gpt.egg-info", ignore_errors=True)

    checks = {
        "version": __version__,
        "skills": len(load_skills()),
        "workflows": len(load_workflows()),
        "domains": len(load_domains()),
        "tunnels": len(registry.all()),
        "tunnel_layer_errors": tunnel_layer_errors,
        "skill_bundle_errors": validate_skill_bundle(packaged_skills_root()),
        "source_tests_pass": pytest_result.returncode == 0,
        "functional_review_pass": functional.returncode == 0,
        "tunnel_review_pass": tunnel.returncode == 0,
        "extension_build_synced": extension.returncode == 0,
        "wheel_build_pass": wheel_build_ok,
        "wheel_source_parity_pass": wheel_parity_ok,
        "wheel_self_test_pass": wheel_self_test_ok,
        "wheel_sha256": wheel_hash,
        "wheel_missing_files": missing_in_wheel,
        "wheel_extra_files": extra_in_wheel,
        "schemas_exist": all((ROOT / "schemas" / name).exists() for name in [
            "raw-request.schema.json", "research-manifest.schema.json", "context-pack.schema.json",
            "final-report.schema.json", "request-status.schema.json", "routing-decision.schema.json",
        ]),
        "example_files_exist": all((ROOT / name).exists() for name in [
            "examples/requests/qos-review.yaml", "examples/planner-result.example.json", "examples/final-result.template.json",
        ]),
        "team_runtime_doc": (ROOT / "docs/TEAM_RUNTIME.md").is_file(),
        "tunnel_architecture_doc": (ROOT / "docs/TUNNEL_ARCHITECTURE.md").is_file(),
    }
    checks["pass"] = all([
        checks["version"] == "0.8.0",
        checks["skills"] == 6,
        checks["workflows"] == 4,
        checks["domains"] == 13,
        checks["tunnels"] == 10,
        not checks["tunnel_layer_errors"],
        not checks["skill_bundle_errors"],
        checks["source_tests_pass"],
        checks["functional_review_pass"],
        checks["tunnel_review_pass"],
        checks["extension_build_synced"],
        checks["wheel_build_pass"],
        checks["wheel_source_parity_pass"],
        checks["wheel_self_test_pass"],
        checks["schemas_exist"],
        checks["example_files_exist"],
        checks["team_runtime_doc"],
        checks["tunnel_architecture_doc"],
    ])

    (ROOT / "release-gate.json").write_text(json.dumps(checks, indent=2), encoding="utf-8")
    print(json.dumps(checks, indent=2))
    if not checks["pass"]:
        diagnostics = {
            "pytest": pytest_result.stderr or pytest_result.stdout,
            "functional": functional.stderr or functional.stdout,
            "tunnel": tunnel.stderr or tunnel.stdout,
            "extension": extension.stderr or extension.stdout,
            "wheel": wheel_error,
        }
        print(json.dumps(diagnostics, indent=2), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
