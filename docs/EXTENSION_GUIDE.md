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

Automation must not rely on a minimized or hidden window: live measurements
showed that a rendered reply can freeze there. Partial output therefore uses a
`MutationObserver` plus a background-worker tick, while network capture can be
the authoritative path for a site whose decoder proves response ownership and
completion. Concurrent turns use separate execution-surface leases as specified
in [BROWSER_PARALLELISM.md](BROWSER_PARALLELISM.md).

## The automation window is closed when idle

An idle surface may be reused only after its previous lease reaches terminal
cleanup. Stream-owned turns may use separate tabs in a shared worker window;
DOM-bound turns use dedicated windows. Idle surfaces close after a grace period,
and cleanup may remove only the exact tab/window owned by its lease. A window
holding a user's own tab is never removed.
