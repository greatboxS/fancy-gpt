# Adding a site

A site is a web chat product FancyGPT can drive. The current target set is
ChatGPT, Gemini, Grok, and Microsoft Copilot, with readiness tracked separately
for observation, browser driving, decoding, core routing, and live verification.
Adding one is deliberately a loop of
*measure, then write*, because the alternative has a specific and expensive
failure mode.

## Why the order matters

A site's reply format is not documented and not stable. Writing a reader from
assumption does not produce an error — it produces text that reads correctly
and is missing pieces, or carries pieces that were never part of the answer.
That is worse than a crash: a crash is investigated, a plausible answer is
acted on.

Every rule in the ChatGPT decoder came from a live turn. One of them — that an
event carrying only a value continues both the path *and* the operation of the
event before it — was got wrong first time by reasoning about it, produced the
right text anyway, and was only caught because the decoder reports what it
could not place instead of quietly moving on.

So: no decoder is written for a site that has not been measured, and a site
with no decoder is not broken. It reads the rendered page, exactly as before.

## The loop

### 1. Let the site into the extension

`extension/chromium/manifest.json` and `extension/firefox/manifest.json` need
the origin in `host_permissions`, in the `page_hook.js` content script's
`matches`, and in the site content script's `matches`. Then
`python scripts/build_extension.py`.

### 2. Run one real turn and read the shapes

The page hook watches every request the site makes and reports shapes — paths
with identifiers reduced to `<id>`, content types, event names, field names,
sizes, counts. It never reports values. Its report arrives with the turn:

```
.fancy-gpt/executions/<execution>.diagnostics.json
```

Look for the request that carries the reply. Three answers decide everything:

- **Which transport.** `contentType: text/event-stream` is a server-sent
  stream; a `socket` report means the site answers over a WebSocket instead,
  as Microsoft Copilot does. They need different readers.
- **How the reply is framed.** `eventNames` and `dataFields` say whether text
  arrives whole, appended, or patched into a document.
- **Whether completion is knowable.** `sawDone`, or an equivalent terminal
  event. Without one, completion still has to come from the page, and the
  stream can only supply content.

### 3. Write the decoder in `src/fancy_gpt/stream_decoding.py`

In the runtime, never in the extension. A decoder is the part that changes
when a site changes, and a decoder inside an extension can only be corrected
by asking every user to reload their browser. The extension captures and
forwards unread; the runtime decides what the bytes mean.

Register it in `DECODERS` under the site id. A site absent from that mapping
has no decoder, which is the honest state rather than a pattern widened until
it matches something.

Two properties are not optional:

- **Refuse rather than guess.** An operation the decoder does not recognise,
  or a fragment it cannot place, must make `trustworthy` false. Refusing costs
  a fallback to the page; guessing costs a wrong answer that nothing catches.
- **Say what was dropped.** Counts and path shapes, so a decoder that has
  fallen behind the site announces it instead of returning a shorter reply.

### 4. Prove it against the page before trusting it

Both readings run on every turn. The decoded reply is compared with the text
read from the rendered page, and only a site that agrees over real turns earns
the right to answer on its own. This is what keeps the page path a real
fallback rather than one discovered to be unreachable months later.

### 5. Write the tests from the recorded events

The events captured in step 2 are the fixture. Tests belong in
`tests/test_stream_decoding.py`, and should cover the framing rules, a reply
cut short, an unrecognised operation, and malformed events — each asserting
that the result is refused rather than quietly wrong.

## Site status

Anything that answers a logged-in browser session over a stream. Measured
first, in every case.

| Site | State |
| --- | --- |
| ChatGPT | measured; DOM adapter, SSE decoder, and core route exist |
| Gemini | measured; DOM adapter, completed-response decoder, and core route exist |
| Grok | admitted and observed by the extension; browser adapter/decoder work is in progress |
| Microsoft Copilot | admitted and observed; its WebSocket transport requires a site-specific measured decoder |
| Claude.ai, DeepSeek, Mistral, Qwen, Kimi, Perplexity | candidates; unmeasured |

The integration boundary, ownership, readiness matrix, and definition of done
are maintained in [SITE_INTEGRATION_HANDOFF.md](SITE_INTEGRATION_HANDOFF.md).

## Prior art

[webllm-proxy](https://github.com/SamuelHaidu/webllm-proxy) (MIT) solves the
same problem for ChatGPT, Databricks and Copilot behind one OpenAI-compatible
endpoint. Two things were taken from reading it: that a provider interface
worth having is small — enumerate models, run a completion — and that Copilot
answers over SignalR frames, which is why the hook watches sockets as well as
fetches. Its namespaced model ids (`chatgpt__gpt-5-mini`) are a good answer to
exposing several sites through one model list.
