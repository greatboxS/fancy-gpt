from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from fancy_gpt import __version__
from fancy_gpt.catalog import load_domains, load_skills, load_workflows
from fancy_gpt.skills import packaged_skills_root, validate_skill_bundle
from fancy_gpt.tunnels import TunnelLayerInspector, TunnelRegistry

root = Path(__file__).resolve().parents[1]
registry = TunnelRegistry()
inspector = TunnelLayerInspector()
tunnel_layer_errors = {
    spec.id: [item.detail for item in inspector.inspect(spec) if item.state != "healthy"]
    for spec in registry.all()
}
tunnel_layer_errors = {key: value for key, value in tunnel_layer_errors.items() if value}

extension_check = subprocess.run(
    [sys.executable, str(root / "scripts/build_extension.py"), "--check"],
    cwd=root,
    text=True,
    capture_output=True,
)

checks = {
    "version": __version__,
    "skills": len(load_skills()),
    "workflows": len(load_workflows()),
    "domains": len(load_domains()),
    "tunnels": len(registry.all()),
    "tunnel_layer_errors": tunnel_layer_errors,
    "skill_bundle_errors": validate_skill_bundle(packaged_skills_root()),
    "extension_build_synced": extension_check.returncode == 0,
    "schemas_exist": all((root / "schemas" / name).exists() for name in [
        "raw-request.schema.json", "research-manifest.schema.json", "context-pack.schema.json",
        "final-report.schema.json", "request-status.schema.json", "routing-decision.schema.json",
    ]),
    "example_files_exist": all((root / name).exists() for name in [
        "examples/requests/qos-review.yaml", "examples/planner-result.example.json", "examples/final-result.template.json",
    ]),
    "tunnel_architecture_doc": (root / "docs/TUNNEL_ARCHITECTURE.md").is_file(),
}
checks["pass"] = (
    checks["version"] == "0.7.0"
    and checks["skills"] == 6
    and checks["workflows"] == 4
    and checks["domains"] == 13
    and checks["tunnels"] == 10
    and not checks["tunnel_layer_errors"]
    and not checks["skill_bundle_errors"]
    and checks["extension_build_synced"]
    and checks["schemas_exist"]
    and checks["example_files_exist"]
    and checks["tunnel_architecture_doc"]
)
(root / "release-gate.json").write_text(json.dumps(checks, indent=2), encoding="utf-8")
print(json.dumps(checks, indent=2))
if not checks["pass"]:
    if extension_check.stdout:
        print(extension_check.stdout)
    if extension_check.stderr:
        print(extension_check.stderr, file=sys.stderr)
    raise SystemExit(1)
