# Browser parallelism architecture

Parallel browser turns are a product requirement. FancyGPT must preserve them
without allowing one turn to hide, cancel, close, overwrite, or consume the
response of another turn.

## Invariants

1. One turn owns one execution surface for its whole lifetime.
2. A surface is identified by a lease, never by a process-global current tab or
   current window.
3. Two turns may run concurrently only when their provider conversations are
   different. Turns for the same provider conversation remain serialized.
4. Every submit, progress snapshot, capture, final response, cancel, timeout,
   and cleanup is fenced by `(job_id, generation_epoch, lease_id)`.
5. Losing visibility may reduce DOM progress quality, but must not silently
   turn a partial page into a successful answer.
6. Network decoding is authoritative only after the site's decoder proves
   completion and response ownership. Otherwise the surface must remain usable
   for page fallback.
7. Releasing one lease cannot inspect or mutate another lease's tab or window.

## Execution surface

The extension maintains a registry instead of global current-window and
keeper-tab state:

```text
SurfaceLease
  lease_id
  job_id
  generation_epoch
  site
  conversation_id
  window_id
  tab_id
  render_requirement
  state: allocating | submitting | observing | terminal | reclaiming
  created_at / deadline / last_activity_at
```

`job_id` and `lease_id` are unique. A generation epoch prevents a delayed
cancel or response from acting on a recycled surface. The registry is rebuilt
conservatively after an MV3 service-worker restart from `storage.session` and
live tab queries. A surface whose ownership cannot be proven is not reused.

## Two execution classes

Sites declare a measured render requirement; the scheduler does not infer it
from the browser name.

### Stream-owned

The captured network response provides text, completion, and response identity.
The page is needed to submit and may be needed to invoke the site's stop
control, but DOM painting is not the source of the final answer.

Several stream-owned turns may use separate tabs in one worker window. The
focus arbiter activates each tab for submission and any required page action;
after acceptance, capture continues independently. If the decoder becomes
untrustworthy, the job either obtains a render slot for page fallback or fails
explicitly as `page_fallback_unavailable`.

### DOM-bound

The page remains necessary for response text or completion. Each concurrent
DOM-bound turn receives a dedicated window so its tab remains the active tab in
that window. Windows are positioned rather than minimized. Visibility and
focus are continuously measured and included in the terminal decision.

If the platform still marks an unfocused or occluded window hidden, capacity is
not reduced globally. The scheduler may use one of these backends:

- a managed Playwright/CDP browser launched with background throttling and
  occlusion optimizations disabled;
- multiple browser worker processes/profiles, each advertising one render slot;
- visible tiled windows across available displays;
- a reliable network decoder that promotes the site to stream-owned.

Thus parallelism is preserved by adding execution surfaces or improving the
decoder, not by serializing the gateway.

## Scheduler and advertised capacity

A browser worker advertises capabilities during `hello` and heartbeat:

```json
{
  "max_turns": 4,
  "max_render_slots": 2,
  "sites": {
    "chatgpt": {"mode": "stream-owned", "decoder": "chatgpt-delta-v1"},
    "gemini": {"mode": "stream-owned", "decoder": "gemini-batchexecute-v1"},
    "grok": {"mode": "dom-bound"},
    "copilot": {"mode": "dom-bound"}
  }
}
```

The bridge routes only within advertised capacity. `max_turns` bounds all live
leases; `max_render_slots` bounds surfaces that require reliable painting.
Excess jobs stay in the gateway's bounded queue or receive its existing
backpressure response. Capacity changes are allowed on heartbeat but never
evict an already-owned turn.

Scheduling rules:

- same provider conversation: FIFO and exclusive;
- different conversations: parallel up to worker capacity;
- cancel and terminal messages: out of band and never queued behind turns;
- health probes: use a reserved control slot or an existing idle surface;
- fresh turns: prefer a clean surface;
- continuation: load the recorded conversation URL into the lease; a tab id is
  never treated as conversation identity.

## Focus and render arbiter

Focus is a scarce page action, not ownership. A short arbiter lock covers only:

1. activating the lease's window and tab;
2. verifying the expected host and adapter build;
3. writing the prompt;
4. checking that the site accepted exactly one submission.

The lock is released after acceptance. It is reacquired for a DOM-only cancel,
continue-generation action, or page fallback. Long model generation never
holds the focus lock.

A trusted user edit to the composer invalidates only that lease. It cannot
cancel or corrupt other jobs.

## Capture ownership

Capture buffers are per lease, not module-global. Each capture records bounded
metadata:

```text
lease_id, job_id, site, request path, transport kind,
start/end time, status, completion signal, truncated flag, payload
```

Selection rules are strict:

- a known site must match a measured response path/channel;
- the capture must start after the lease enters `submitting`;
- its provider identity must agree with the lease when available;
- malformed, truncated, unknown, or incomplete framing is not trustworthy;
- capture size is never sufficient evidence that it is the response;
- a long-lived WebSocket reports correlated turn snapshots instead of waiting
  for the socket to close.

Buffers are bounded by total bytes and capture count. Eviction preserves known
reply candidates and discards diagnostics/telemetry first.

## Lifecycle

```text
queued
  -> allocating
  -> loading
  -> submitting
  -> accepted
  -> observing
  -> completed | failed | cancelled | uncertain-submit
  -> reclaiming
  -> idle/closed
```

Cleanup is idempotent. It removes tab listeners, mutation observers, tick ports,
capture buffers, cancellation tombstones, and the exact tab/window owned by the
lease. A short bounded cancellation tombstone handles late messages; it is not
kept forever.

On service-worker restart, a lease in `submitting` or `accepted` is never blindly
resubmitted. The bridge/gateway receives an uncertain terminal state unless the
extension can reattach and prove the provider outcome.

## Implementation slices

### Slice 1: ownership correctness

- Introduce `SurfaceLease` and a lease registry.
- Replace global window/keeper state with per-lease ownership.
- Dispose tab-close listeners after every race.
- Bound cancellation tombstones.
- Serialize observation storage read-modify-write operations.

### Slice 2: real parallel surfaces

- Add worker capacity negotiation.
- Add the focus arbiter.
- Give DOM-bound turns dedicated windows.
- Allow stream-owned turns to share a window with one tab per lease.
- Preserve independent progress/cancel/terminal channels.

### Slice 3: capture correctness

- Scope capture buffers to leases.
- Replace largest-capture fallback with measured correlation.
- Make malformed/truncated ChatGPT and Gemini captures untrustworthy.
- Snapshot long-lived Copilot WebSocket activity and correlate SignalR turns.

### Slice 4: resilience

- Recover or quarantine leases across MV3 worker restart.
- Add reclaim deadlines for crashed/closed surfaces.
- Add managed-browser flags and multi-worker capacity as optional backends.
- Expose per-worker and per-lease diagnostics through existing inspection tools.

## Required tests

The background runtime needs an executable fake `windows`/`tabs` harness. Source
substring tests do not prove ownership or concurrency.

Minimum acceptance scenarios:

1. Four different conversations complete in parallel with four distinct leases.
2. Two turns for one conversation serialize while unrelated turns continue.
3. Allocating two surfaces concurrently never leaks or swaps windows.
4. Activating one stream-owned tab does not corrupt another turn's capture.
5. Cancelling one lease never clicks stop or returns partial text from another.
6. Closing one tab fails only its owner and disposes its listener.
7. A service-worker restart neither loses ownership nor resubmits a prompt.
8. Capture overflow retains the known response and evicts telemetry first.
9. A long-lived Copilot socket reports multiple independently correlated turns.
10. A decoder failure obtains page fallback or returns an explicit failure.
11. Generated Chromium, Edge, Firefox, and packaged assets share one build id.
12. A live soak records throughput, first-token latency, visibility transitions,
    cross-turn correlation errors, and leaked surfaces.

The release gate for parallelism is zero cross-turn response, progress,
cancellation, capture, or cleanup leakage under repeated randomized completion
order.

