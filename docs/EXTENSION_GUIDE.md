# Extension guide

Prefer this order:

1. **Focus** — use for a new narrow topic (`WireGuard`, `RAUC`, `V4L2`, `CAN`, `SRT`). No code/catalog change.
2. **Domain policy** — extend/add when source authorities, artifact types and evidence semantics are genuinely different.
3. **Workflow** — add only when multiple primitive reasoning passes/cross-domain quality gates must be orchestrated differently.
4. **Skill** — last resort; add only when the reasoning/output contract cannot be represented by review/design/consult/investigate/verify/write.

This rule prevents skill proliferation.


## Testing the adapters

The adapters are the foundation the rest of the system stands on, and they run
where we cannot watch them. Asserting that their source *contains* a string
proves nothing about what it *does*, so `extension/tests/` executes the real
adapter code against a scripted DOM, with no npm dependency:

```bash
node extension/tests/test_site_kit.js
node extension/tests/test_adapters.js
```

Both run under `pytest` too (`tests/test_extension_behaviour.py`), so the
release gate covers them, and a new `site_*.js` adapter fails the suite until it
has behavioural coverage.

`extension/tests/dom_stub.js` implements only what the adapters actually touch,
and deliberately reproduces the two behaviours that matter: `querySelectorAll`
returning several matches, which the adapters must treat as ambiguous, and
mutations notifying observers.

## Progress is observed, not polled

Automation runs in a minimized window so it stays out of the user's way, and
browsers throttle `setTimeout` hard in hidden windows - measured as 24-second
gaps where the page had been streaming all along. Partial output is therefore
reported by a `MutationObserver`, which is driven by the DOM changing rather
than by a timer.

The settle loop still uses a timer, so completion detection remains slower in a
hidden window than a visible one.

## The automation window is closed when idle

The task window is reused across jobs, so it is not closed with each tab.
Without an explicit idle close it accumulates: the tab goes away and an empty
minimized window stays behind for the rest of the browser session. It is closed
only when no job is active and no tabs remain in it, so a window holding a
user's own tab is never taken away.
