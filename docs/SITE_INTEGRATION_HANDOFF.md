# Site integration handoff

This document is the working contract for joining browser work to the FancyGPT
core. It deliberately tracks readiness per layer: a domain being observable by
the extension does not yet mean that the gateway can route a client turn to it.

## Current ownership

The browser track currently owns:

- extension capture and page observation;
- site DOM adapters and their browser fixtures;
- response transport measurement;
- runtime decoding and decoder fixtures.

The core-integration track owns:

- site registration and conversation URL policy;
- gateway model aliases and capability resolution;
- routing a normalized turn to the selected site;
- CLI, MCP and health/discovery surfaces;
- cross-layer and real-client conformance tests;
- user-facing setup and compatibility documentation.

Files already modified by the browser track must not be reformatted or folded
into unrelated changes. In particular, coordinate changes to
`extension/common/content.js`, generated extension assets,
`src/fancy_gpt/stream_decoding.py`, `scripts/live_matrix.py`, and their tests.

## Readiness matrix

Use only these states: `observed`, `adapter`, `decoded`, `core-routable`, and
`verified`. A site advances one column at a time.

| Site | Extension observes traffic | DOM adapter drives turns | Runtime decoder | Core/gateway route | Live verified |
| --- | --- | --- | --- | --- | --- |
| ChatGPT | yes | yes | yes | yes | required before release |
| Gemini | yes | yes | yes | yes | required before release |
| Grok | yes | browser track | browser track | pending | pending |
| Microsoft Copilot | yes | browser track | browser track | pending | pending |

Update this table only with evidence from a test or a recorded live run. The
extension manifests already admit Grok and Copilot origins; that fact alone is
the `observed` state.

### Copilot protocol evidence to preserve

The independent [webllm-proxy Copilot implementation](https://github.com/SamuelHaidu/webllm-proxy/tree/main/webllm_proxy/providers/copilot)
is useful prior art, but is not evidence that FancyGPT's own browser path works.
It confirms two editions that must not be merged into one heuristic decoder:

- M365 BizChat uses SignalR JSON records separated by `0x1e`; answer updates are
  cumulative and progress/search message types are interleaved;
- consumer `copilot.microsoft.com` uses event JSON frames with incremental
  `appendText` payloads and different completion signals.

For either edition, the extension must first capture bounded per-lease frames
from the exact known socket path and retain a request/message identity that can
be correlated to the submitted turn. A socket closing is not a required turn
boundary because the page can reuse a long-lived connection. Until those local
fixtures and live correlation evidence exist, raw frames stay inside the page,
the runtime decoder remains disabled, and Copilot remains `observed` only.

This distinction follows Chrome's requirement to treat an MV3 service worker as
ephemeral and persist needed state rather than relying on globals; see the
[extension service-worker lifecycle](https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle)
and [`chrome.storage` reference](https://developer.chrome.com/docs/extensions/reference/api/storage).

## End-to-end contract

```text
Codex / Claude Code / Gemini CLI / MCP
                  |
          protocol adapter
                  |
           NormalizedTurn
                  |
        GatewayService / Engine
                  |
        ModelRequest.metadata.site
                  |
           TunnelManager
                  |
       extension background SITES
                  |
        content adapter.executeTurn
            /                 \
     rendered page       captured transport
            \                 /
             trusted reply selection
                  |
       GatewayResult / ModelResponse
                  |
         client wire response
```

The stable boundary between core and browser is `ModelRequest` plus its
metadata. The browser side must not know whether a request came from MCP,
OpenAI Responses, Anthropic Messages, or Gemini generateContent. Conversely,
the gateway must not know selectors, page event names, response paths, or
browser tab details.

Concurrent turns follow [BROWSER_PARALLELISM.md](BROWSER_PARALLELISM.md). The
required model is one execution-surface lease per turn; shared current-window
or current-tab variables are not a valid ownership boundary.

### Request contract

Every automatic turn passed to a site carries:

- a unique request/job id;
- `metadata.site`, using the canonical lowercase site id;
- conversation mode: fresh, persistent, or continue;
- an optional provider conversation id;
- a prompt containing the normalized client transcript and tool envelope;
- a finite timeout and generation epoch;
- an optional progress consumer and cancellation token.

Canonical ids are `chatgpt`, `gemini`, `grok`, and `copilot`. Public model
aliases select a site; tunnel selection remains independent.

### Response contract

A successful browser turn returns:

- final text or a strictly validated tool-call envelope;
- provider conversation id when the site exposes one;
- one response identity belonging to the submitted turn;
- optional cumulative progress snapshots;
- scrubbed diagnostics describing capture shapes, never credentials or raw
  unrelated page data.

A decoder result is eligible only when it is trustworthy. Unknown operations,
unplaced text, ambiguous response ownership, missing completion evidence, or a
disagreement that cannot be explained must fall back to the page or fail the
turn. The core must never reinterpret raw site captures.

## Core connection points

When the browser track declares a site adapter/decoder ready, connect it in this
order. Keeping the order makes intermediate commits truthful and testable.

1. Register the site in `src/fancy_gpt/web/sites/` and `SiteRegistry` with
   canonical hosts, fresh URL, and conversation URL rules.
2. Add the same URL policy to `extension/common/background.js`; build-generated
   copies are updated only through `scripts/build_extension.py`.
3. Add gateway aliases and explicit alias-to-site mapping. Do not extend
   prefix guessing in `GatewayService.resolve_site`; use one declarative model
   catalog shared by `/v1/models` and capability discovery.
4. Add the site's adapter capability in `gateway_capabilities.py`. Advertise
   only what the browser path actually supports, even if the public upstream
   API supports more modalities.
5. Ensure tunnel selection remains site-neutral. A tunnel is eligible because
   its connected worker reports that site/build as healthy, not because the
   tunnel id names a browser vendor.
6. Extend CLI/MCP `sites list`, health and inspection output from the same site
   registry.
7. Add normalized gateway tests for every public alias and protocol. Then add
   one live matrix row per supported browser tunnel.
8. Mark the site `core-routable`, and only mark it `verified` after a real turn,
   continuation, streaming/final extraction, cancellation, and tool loop pass.

## Required tests per site

### Browser and decoder

- health on the correct and incorrect host;
- fresh and continuing submission;
- prompt acceptance and exactly-once submit;
- response ownership when old turns mutate late;
- partial progress and final completion;
- cancellation before and after prompt acceptance;
- malformed, truncated and unknown transport frames;
- decoder/page agreement on recorded live fixtures;
- adapter build-id drift.
- parallel surface allocation with randomized completion order;
- strict isolation of progress, capture, cancellation, and cleanup by lease.

### Core and client protocols

- every alias resolves to the intended site without affecting tunnel choice;
- OpenAI, Anthropic and Gemini input normalize to the same semantic turn;
- text response and tool call map back to each client protocol;
- session continuation preserves the site and provider conversation binding;
- changing to an incompatible site reconstructs intentionally;
- unsupported modality is rejected before browser submit;
- disconnect and cancellation reach the owning browser job;
- capability and model discovery agree with actual routing.

### Live acceptance

For each site, record the date, browser, extension build id, scenario, result,
conversation continuation result, stream/page comparison, and sanitized failure
category. A live pass is compatibility evidence, not a permanent claim about a
third-party UI.

## Merge boundaries

The browser track should deliver one site as a vertical slice containing its
adapter, capture classification, decoder, fixtures, and a short live evidence
record. The core track should consume only the canonical site id and documented
request/response contract.

Generated extension directories are build artifacts. Resolve source changes in
`extension/common/` first and regenerate them once, after concurrent work has
landed. This prevents Chromium, Edge, Firefox and packaged asset copies from
drifting or producing noisy conflicts.

## Definition of done

A site is supported end to end only when all of the following are true:

- it is registered once and appears consistently in CLI, MCP and gateway
  discovery;
- at least one model alias resolves to it explicitly;
- a healthy extension worker can drive fresh and continuing turns;
- the final reply is bound to the submitted turn;
- streaming either yields trustworthy deltas or cleanly falls back;
- text and tool-call turns work through the three gateway protocols;
- cancellation and timeout are terminal and inspectable;
- the live matrix contains a recent passing record.
