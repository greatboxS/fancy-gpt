"""Run the extension's own behavioural suites.

The browser adapters are the foundation the rest of the system stands on, and
they run where we cannot watch them. Asserting that their source contains
certain strings proves nothing about what they do, so these suites execute the
real adapter code against a scripted DOM. They are wired into pytest so the
release gate covers them.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUITES = ("test_site_kit.js", "test_adapters.js", "test_page_hook.js")

node = shutil.which("node")
requires_node = pytest.mark.skipif(node is None, reason="node is not installed")


@requires_node
@pytest.mark.parametrize("suite", SUITES)
def test_extension_suite_passes(suite: str) -> None:
    completed = subprocess.run(
        [node, str(ROOT / "extension" / "tests" / suite)],
        capture_output=True, text=True, timeout=180, cwd=ROOT,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, f"{suite} failed:\n{output}"
    assert "0 failed" in output, f"{suite} reported failures:\n{output}"


@requires_node
def test_every_adapter_has_behavioural_coverage() -> None:
    """A new site adapter must not ship without being exercised."""
    adapters = {
        path.name
        for path in (ROOT / "extension" / "common").glob("site_*.js")
        if path.name != "site_kit.js"
    }
    covered = (ROOT / "extension" / "tests" / "test_adapters.js").read_text(encoding="utf-8")
    missing = sorted(name for name in adapters if name not in covered)
    assert not missing, f"these adapters have no behavioural test: {missing}"


@requires_node
def test_the_suites_actually_assert_something() -> None:
    """Guard against a suite that silently runs zero tests."""
    for suite in SUITES:
        completed = subprocess.run(
            [node, str(ROOT / "extension" / "tests" / suite)],
            capture_output=True, text=True, timeout=180, cwd=ROOT,
        )
        passed = int(completed.stdout.split(" passed")[0].strip().split()[-1])
        assert passed >= 5, f"{suite} only ran {passed} assertions"
