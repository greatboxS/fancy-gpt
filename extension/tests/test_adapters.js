/* End-to-end tests of the real adapters against a scripted page.
 *
 * These execute executeTurn itself, so a change that breaks how a turn is
 * submitted, observed, settled or cancelled fails here rather than in a live
 * browser run.
 */
const {loadAdapters, test, assert, assertEqual, rejects, report} = require("./harness.js");
const {StubElement} = require("./dom_stub.js");

const ENVELOPE = '{"type":"message","text":"done"}';
const ENVELOPE_WITH_REFS =
  '{"type":"outcome","edit":{"old_ref":"E1-OLD","new_ref":"E1-NEW"}}';

function chatgptPage() {
  const composer = new StubElement("div", {contentEditable: "true", id: "prompt-textarea"});
  const send = new StubElement("button", {"data-testid": "send-button"});
  document.body.append(composer);
  document.body.append(send);
  document._composerTarget = composer;
  return {composer, send};
}

/* Reveal a reply the way the page does: after the prompt has been sent.
 * Creating it synchronously would put it in the adapter's baseline, where it is
 * correctly ignored as pre-existing. */
function replyAfterSend(id, text, delayMs = 60) {
  return new Promise(resolve => setTimeout(() => resolve(assistantTurn(id, text)), delayMs));
}

function assistantTurn(id, text) {
  const turn = new StubElement("div", {
    "data-turn-id": id, "data-message-author-role": "assistant", text,
  });
  document.body.append(turn);
  return turn;
}

function stopControl() {
  const stop = new StubElement("button", {"data-testid": "stop-button"});
  document.body.append(stop);
  return stop;
}

async function main() {
  // -- ChatGPT: a normal turn ------------------------------------------------
  await test("chatgpt submits the prompt and returns the settled reply", async () => {
    loadAdapters(["site_chatgpt.js"]);
    const {composer, send} = chatgptPage();
    const adapter = globalThis.FancyGPTSites.chatgpt;

    const running = adapter.executeTurn("PROMPT-A", 8000, null, {});
    // The page reveals the reply only after the prompt was sent.
    setTimeout(() => {
      assertEqual(composer.innerText, "PROMPT-A", "prompt must reach the composer");
      assert(send.clicks === 1, "send must be clicked exactly once");
      assistantTurn("turn-1", ENVELOPE);
    }, 50);

    const result = await running;
    assertEqual(result.text, ENVELOPE, "returned text");
    assertEqual(result.responseIdentity, "turn-1", "bound to the turn it created");
    assert(result.diagnostics?.waiter, "successful turns expose waiter diagnostics");
    assert(typeof result.diagnostics.maxEvaluateMs === "number", "evaluation cost is measured");
  });

  await test("chatgpt reads message content without turn action labels", async () => {
    // The raw-text probe is a temporary bypass; this covers the real path.
    globalThis.FANCY_GPT_RAW_TEXT_PROBE = false;
    loadAdapters(["site_chatgpt.js"]);
    chatgptPage();
    const adapter = globalThis.FancyGPTSites.chatgpt;
    const running = adapter.executeTurn("PROMPT-A2", 8000, null, {});
    setTimeout(() => {
      const turn = assistantTurn("turn-1", "");
      const content = new StubElement("div", {class: "markdown", text: ENVELOPE});
      // Chromium may defer layout for an occluded tab: innerText remains at an
      // old rendered prefix while textContent already reflects the live DOM.
      Object.defineProperty(content, "innerText", {get: () => '{"type":"message","text'});
      Object.defineProperty(content, "textContent", {get: () => ENVELOPE});
      turn.append(content);
      turn.append(new StubElement("div", {text: "Copy\nGood response\nBad response"}));
    }, 50);

    const result = await running;
    assertEqual(result.text, ENVELOPE, "UI controls must not contaminate the reply");
  });

  await test("chatgpt keeps fenced block labels and indentation on their own lines", async () => {
    // The raw-text probe is a temporary bypass; this covers the real path.
    globalThis.FANCY_GPT_RAW_TEXT_PROBE = false;
    loadAdapters(["site_chatgpt.js"]);
    chatgptPage();
    const adapter = globalThis.FancyGPTSites.chatgpt;
    const running = adapter.executeTurn("PROMPT-A3", 8000, null, {});
    setTimeout(() => {
      const turn = assistantTurn("turn-1", "");
      const markdown = new StubElement("div", {class: "markdown"});
      // A real code block: the ``` fence never reaches the DOM. It renders as a
      // header label beside a Copy control, and only layout puts them on
      // separate lines -- which is exactly what textContent does not do.
      const block = (label, body) => {
        const pre = new StubElement("pre");
        const header = new StubElement("div");
        header.append(new StubElement("div", {text: label}));
        header.append(new StubElement("button", {text: "Copy"}));
        pre.append(header);
        pre.append(new StubElement("code", {text: body}));
        markdown.append(pre);
      };
      block("json", ENVELOPE_WITH_REFS);
      block("fancygpt:E1-OLD", "    def run(self):\n        return 1");
      block("fancygpt:E1-NEW", "    def run(self):\n        return 2");
      turn.append(markdown);
      turn.append(new StubElement("div", {text: "Copy\nGood response\nBad response"}));
    }, 50);

    const result = await running;
    const lines = result.text.split("\n");
    assert(lines.some(line => line === "```fancygpt:E1-OLD"),
      "a block label must open a fence the contract can find");
    assert(lines.some(line => line === "        return 1"),
      "verbatim indentation must survive extraction: " + JSON.stringify(result.text));
    assert(!/Good response/.test(result.text), "turn action labels must not leak in");
  });

  await test("chatgpt reads every content part of a split reply", async () => {
    loadAdapters(["site_chatgpt.js"]);
    chatgptPage();
    const adapter = globalThis.FancyGPTSites.chatgpt;
    const running = adapter.executeTurn("PROMPT-A4", 8000, null, {});
    setTimeout(() => {
      const turn = assistantTurn("turn-1", "");
      // ChatGPT splits one assistant turn into several content parts.
      turn.append(new StubElement("div", {class: "markdown", text: '{"type":"message",'}));
      turn.append(new StubElement("div", {class: "markdown", text: '"text":"done"}'}));
    }, 50);

    const result = await running;
    assert(result.text.includes('"text":"done"}'),
      "the later parts of a split reply must not be dropped: " + JSON.stringify(result.text));
  });

  await test("chatgpt ignores an old turn that only finishes rendering late", async () => {
    globalThis.FANCY_GPT_RAW_TEXT_PROBE = false;
    loadAdapters(["site_chatgpt.js"]);
    chatgptPage();
    // A continued conversation: an earlier reply is present but still empty,
    // so it is in the baseline with no text.
    const older = assistantTurn("turn-old", "");
    const adapter = globalThis.FancyGPTSites.chatgpt;
    const running = adapter.executeTurn("PROMPT-A5", 8000, null, {});
    setTimeout(() => {
      // It draws itself in after we submitted. That is not a second reply.
      older.setText('{"type":"message","text":"an earlier answer"}');
      assistantTurn("turn-new", ENVELOPE);
    }, 50);

    const result = await running;
    assertEqual(result.responseIdentity, "turn-new", "must bind to the turn this job created");
    assertEqual(result.text, ENVELOPE, "and return its text");
  });

  await test("chatgpt streams partial text through the observer", async () => {
    loadAdapters(["site_chatgpt.js"]);
    chatgptPage();
    const adapter = globalThis.FancyGPTSites.chatgpt;
    const seen = [];

    const running = adapter.executeTurn("PROMPT-B", 8000, text => seen.push(text), {});
    const turn = await replyAfterSend("turn-1", '{"type":"message","text":"par');
    // The observer is attached once the adapter binds the reply, so wait for
    // the first report before simulating further rendering.
    while (seen.length === 0) await new Promise(resolve => setTimeout(resolve, 20));
    // Reads are rate limited, so renders are spaced past that interval; a page
    // that mutates faster is conflated on purpose.
    await new Promise(resolve => setTimeout(resolve, 200));
    turn.setText('{"type":"message","text":"partial');
    await new Promise(resolve => setTimeout(resolve, 200));
    turn.setText(ENVELOPE);

    await running;
    // Every observed render reached the consumer, in order, without repeats.
    assert(seen.length >= 3, `expected several progress reports, got ${seen.length}`);
    assertEqual(seen[seen.length - 1], ENVELOPE, "last report is the final text");
    assertEqual(new Set(seen).size, seen.length, "no duplicate reports");
  });

  await test("chatgpt does not settle while the stop control is visible", async () => {
    loadAdapters(["site_chatgpt.js"]);
    chatgptPage();
    const stop = stopControl();
    const adapter = globalThis.FancyGPTSites.chatgpt;

    const running = adapter.executeTurn("PROMPT-C", 8000, null, {});
    await replyAfterSend("turn-1", ENVELOPE);
    // Still streaming for a while, then quiet.
    let settledEarly = false;
    running.then(() => { settledEarly = true; }, () => {});
    await new Promise(resolve => setTimeout(resolve, 900));
    assert(settledEarly === false, "must not settle while the site says it is still generating");
    stop.remove();
    const result = await running;
    assertEqual(result.text, ENVELOPE, "settles once generation stops");
  });

  await test("chatgpt refuses an ambiguous response", async () => {
    loadAdapters(["site_chatgpt.js"]);
    chatgptPage();
    const adapter = globalThis.FancyGPTSites.chatgpt;
    const running = adapter.executeTurn("PROMPT-D", 4000, null, {});
    // Two new assistant turns: neither can be attributed to this job.
    await replyAfterSend("turn-1", ENVELOPE);
    assistantTurn("turn-2", ENVELOPE);
    await rejects(running, /ambiguous/i, "two new turns must be reported as ambiguous");
  });

  await test("chatgpt reports a missing send control instead of hanging", async () => {
    loadAdapters(["site_chatgpt.js"]);
    const composer = new StubElement("div", {contentEditable: "true", id: "prompt-textarea"});
    document.body.append(composer);
    document._composerTarget = composer;
    // No send button ever appears.
    const adapter = globalThis.FancyGPTSites.chatgpt;
    await rejects(adapter.executeTurn("PROMPT-E", 3000, null, {}), /send control unavailable/i,
      "a composer that never reveals send must fail explicitly");
  });

  await test("chatgpt reports a missing composer as a sign-in problem", async () => {
    loadAdapters(["site_chatgpt.js"]);
    const adapter = globalThis.FancyGPTSites.chatgpt;
    // An empty page is what a signed-out session looks like.
    await rejects(adapter.executeTurn("PROMPT-F", 1500, null, {}), /sign in/i,
      "a missing composer must point at authentication");
  });

  // -- ChatGPT: cancellation -------------------------------------------------
  await test("chatgpt cancellation clicks stop and returns partial text", async () => {
    loadAdapters(["site_chatgpt.js"]);
    chatgptPage();
    const stop = stopControl();
    const adapter = globalThis.FancyGPTSites.chatgpt;
    let cancelled = false;

    const running = adapter.executeTurn("PROMPT-G", 8000, null, {isCancelled: () => cancelled});
    await replyAfterSend("turn-1", '{"type":"message","text":"half');
    setTimeout(() => { cancelled = true; }, 60);

    const result = await running;
    assert(result.cancelled === true, "must report cancellation distinctly");
    assert(result.stoppedGeneration === true, "must report that stop was clicked");
    assertEqual(stop.clicks, 1, "the site's own stop control is clicked once");
    assert(result.text.includes("half"), "whatever was produced is kept");
    // Regression: cancelling before the first successful bind used to report an
    // empty result and a synthetic identity, discarding a visible reply.
    assertEqual(result.responseIdentity, "turn-1", "the reply on screen must still be identified");
  });

  await test("chatgpt cancellation before the prompt is accepted is a clean cancel", async () => {
    loadAdapters(["site_chatgpt.js"]);
    chatgptPage();
    const adapter = globalThis.FancyGPTSites.chatgpt;
    // Cancelled before the page takes the prompt: nothing was submitted, so
    // this must report cancellation rather than a rejected submission, and
    // nothing about it is uncertain.
    const result = await adapter.executeTurn("PROMPT-H", 6000, null, {isCancelled: () => true});
    assert(result.cancelled === true, "cancelled");
    assert(result.stoppedGeneration === false, "no stop control means nothing to stop");
    assertEqual(result.text, "", "no partial text existed");
  });

  await test("chatgpt cancellation after acceptance but before a reply", async () => {
    loadAdapters(["site_chatgpt.js"]);
    const {composer, send} = chatgptPage();
    send.onclick = () => composer.setText("");
    const adapter = globalThis.FancyGPTSites.chatgpt;
    let cancelled = false;
    const running = adapter.executeTurn("PROMPT-H2", 6000, null, {isCancelled: () => cancelled});
    setTimeout(() => { cancelled = true; }, 80);
    const result = await running;
    assert(result.cancelled === true, "cancelled");
    assertEqual(result.text, "", "no reply had been produced yet");
  });

  await test("chatgpt disconnects its observer when the turn ends", async () => {
    loadAdapters(["site_chatgpt.js"]);
    chatgptPage();
    const adapter = globalThis.FancyGPTSites.chatgpt;
    const running = adapter.executeTurn("PROMPT-I", 8000, () => {}, {});
    await replyAfterSend("turn-1", ENVELOPE);
    await running;
    // An observer left attached keeps firing for the life of the page.
    assertEqual(document.observers.length, 0, "observer must be disconnected");
  });

  // -- Gemini ----------------------------------------------------------------
  await test("gemini health check reports a missing composer", async () => {
    loadAdapters(["site_gemini.js"]);
    const health = await globalThis.FancyGPTSites.gemini.healthCheck();
    assert(health.ok === false, "an empty page is not ready");
    assert(typeof health.reason === "string" && health.reason.length > 0, "a reason is required");
    assert(typeof health.build === "string", "the build must be reported so drift is detectable");
  });

  await test("chatgpt health check reports readiness and its build", async () => {
    loadAdapters(["site_chatgpt.js"]);
    chatgptPage();
    const health = await globalThis.FancyGPTSites.chatgpt.healthCheck();
    assert(health.ok === true, "a page with a composer is ready");
    assert(typeof health.build === "string" && health.build.length > 0, "build id must be reported");
  });

  await test("both adapters expose the same contract", () => {
    loadAdapters(["site_chatgpt.js", "site_gemini.js"]);
    for (const id of ["chatgpt", "gemini"]) {
      const adapter = globalThis.FancyGPTSites[id];
      assert(adapter, `${id} adapter must register itself`);
      for (const member of ["id", "freshUrl", "healthCheck", "executeTurn"]) {
        assert(adapter[member] !== undefined, `${id} must expose ${member}`);
      }
      // Defaults do not count toward Function.length, so the arity check is
      // that the first three are required and options is optional.
      assertEqual(adapter.executeTurn.length, 3,
        `${id}.executeTurn must take (prompt, timeoutMs, onProgress) with optional options`);
    }
  });

  // -- submission is verified, not assumed -----------------------------------
  await test("chatgpt retries and then reports a prompt the page never accepted", async () => {
    loadAdapters(["site_chatgpt.js"]);
    const {composer, send} = chatgptPage();
    // A send control that does nothing: the app ignores the click and the
    // prompt stays in the composer. This used to wait out the whole timeout,
    // indistinguishable from a slow model, until a human clicked submit.
    send.onclick = () => {};
    const adapter = globalThis.FancyGPTSites.chatgpt;
    await rejects(adapter.executeTurn("PROMPT-J", 20000, null, {}), /did not accept/i,
      "an ignored submission must be reported, not waited out");
    assert(send.clicks >= 2, `the click should be retried, saw ${send.clicks}`);
  });

  await test("chatgpt accepts a submission once the composer is cleared", async () => {
    loadAdapters(["site_chatgpt.js"]);
    const {composer, send} = chatgptPage();
    // The real page clears the composer when it takes the prompt.
    send.onclick = () => composer.setText("");
    const adapter = globalThis.FancyGPTSites.chatgpt;
    const running = adapter.executeTurn("PROMPT-K", 8000, null, {});
    await replyAfterSend("turn-1", ENVELOPE);
    const result = await running;
    assertEqual(result.text, ENVELOPE, "the turn completes normally");
    assertEqual(send.clicks, 1, "no retry is needed when the page accepts it");
  });

  await test("chatgpt treats an appearing stop control as acceptance", async () => {
    loadAdapters(["site_chatgpt.js"]);
    const {send} = chatgptPage();
    // Some flows keep the composer text but start generating immediately.
    send.onclick = () => stopControl();
    const adapter = globalThis.FancyGPTSites.chatgpt;
    const running = adapter.executeTurn("PROMPT-L", 8000, null, {});
    await replyAfterSend("turn-1", ENVELOPE);
    // Generation finishes, the stop control goes away.
    setTimeout(() => { const s = document.querySelectorAll('button[data-testid="stop-button"]')[0]; if (s) s.remove(); }, 100);
    const result = await running;
    assertEqual(result.text, ENVELOPE, "acceptance can be signalled by generation starting");
    assertEqual(send.clicks, 1, "no retry needed");
  });

  await test("gemini resuming a conversation reaches the composer at all", async () => {
    /* The path that only runs when continuing.
     *
     * Its loop once tested a deadline and an idle clock belonging to a
     * different function, so resuming a conversation threw a ReferenceError
     * before the prompt was ever sent -- while starting a fresh one, which
     * never reaches this code, worked perfectly. Nothing executed it, so
     * nothing said so.
     */
    loadAdapters(["site_gemini.js"], "gemini.google.com");
    const composer = new StubElement("div", {contentEditable: "true", role: "textbox"});
    document.body.append(composer);
    document._composerTarget = composer;
    // An earlier answer already on the page, as a resumed thread has.
    document.body.append(new StubElement("model-response", {text: "an earlier answer"}));

    const adapter = globalThis.FancyGPTSites.gemini;
    const running = adapter.executeTurn("PROMPT-G", 4000, null, {continuing: true});
    setTimeout(() => {
      const reply = new StubElement("model-response", {text: ENVELOPE});
      document.body.append(reply);
    }, 100);

    // Whatever it settles on, it must not die on a name that is not there.
    await running.catch(error => {
      assert(!/is not defined/.test(String(error && error.message)),
        "resuming must not fail on an undefined reference: " + error);
      return null;
    });
  });

  // -- gemini text extraction ------------------------------------------------
  await test("gemini strips the site's own chrome from the reply", async () => {
    loadAdapters(["site_gemini.js"], "gemini.google.com");
    const response = new StubElement("model-response", {});
    const content = new StubElement("message-content", {class: "model-response-text"});
    content.append(new StubElement("div", {text: "The real reply."}));
    content.append(new StubElement("button", {text: "Copy"}));
    content.append(new StubElement("model-thoughts", {text: "Show thinking: internal notes"}));
    response.append(content);
    document.body.append(response);

    const composer = new StubElement("div", {contentEditable: "true", role: "textbox"});
    const send = new StubElement("button", {class: "send-button"});
    document.body.append(composer); document.body.append(send);
    document._composerTarget = composer;

    const adapter = globalThis.FancyGPTSites.gemini;
    const running = adapter.executeTurn("P", 6000, null, {});
    setTimeout(() => {
      const reply = new StubElement("model-response", {});
      const replyContent = new StubElement("message-content", {class: "model-response-text"});
      replyContent.append(new StubElement("div", {text: ENVELOPE}));
      replyContent.append(new StubElement("button", {text: "Good response"}));
      reply.append(replyContent);
      document.body.append(reply);
    }, 60);

    const result = await running;
    // Button labels and the reasoning panel must not reach the parser.
    assert(!result.text.includes("Good response"), "action buttons must be excluded");
    assert(!result.text.includes("Show thinking"), "reasoning panels must be excluded");
    assertEqual(result.text, ENVELOPE, "only the model's own reply is returned");
  });

  await test("gemini finds its controls without relying on a language", async () => {
    loadAdapters(["site_gemini.js"], "gemini.google.com");
    // A UI in a language none of the aria-labels cover.
    const composer = new StubElement("div", {contentEditable: "true", role: "textbox"});
    const send = new StubElement("button", {class: "send-button", "aria-label": "Enviar mensaje"});
    document.body.append(composer); document.body.append(send);
    const health = await globalThis.FancyGPTSites.gemini.healthCheck();
    assert(health.ok === true, "a localised UI must still be usable");
  });

  await test("chatgpt rebinds when a thinking placeholder is replaced", async () => {
    loadAdapters(["site_chatgpt.js"]);
    const {composer, send} = chatgptPage();
    send.onclick = () => composer.setText("");
    const adapter = globalThis.FancyGPTSites.chatgpt;
    const seen = [];

    const running = adapter.executeTurn("PROMPT-M", 10000, text => seen.push(text), {});
    // ChatGPT shows a placeholder turn first.
    const placeholder = await replyAfterSend("turn-thinking", "Thinking");
    await new Promise(resolve => setTimeout(resolve, 250));
    // Then replaces it with the real answer under a different turn id.
    placeholder.remove();
    assistantTurn("turn-answer", ENVELOPE);

    const result = await running;
    // Staying bound to the placeholder used to hang until the timeout.
    assertEqual(result.text, ENVELOPE, "the replacement answer must be picked up");
    assertEqual(result.responseIdentity, "turn-answer", "must rebind to the real turn");
    assert(seen.includes(ENVELOPE), "progress must follow the rebind");
  });

  await test("chatgpt does not rebind while its turn is still present", async () => {
    loadAdapters(["site_chatgpt.js"]);
    const {composer, send} = chatgptPage();
    send.onclick = () => composer.setText("");
    const adapter = globalThis.FancyGPTSites.chatgpt;
    const running = adapter.executeTurn("PROMPT-N", 8000, null, {});
    const turn = await replyAfterSend("turn-1", '{"type":"message","text":"gro');
    await new Promise(resolve => setTimeout(resolve, 200));
    turn.setText(ENVELOPE);
    const result = await running;
    assertEqual(result.responseIdentity, "turn-1", "a turn that is still there must keep its binding");
  });

  // -- stalling is inactivity, not elapsed time ------------------------------
  await test("a turn that is still generating is not killed by elapsed time", async () => {
    loadAdapters(["site_chatgpt.js"]);
    const {composer, send} = chatgptPage();
    send.onclick = () => composer.setText("");
    const stop = stopControl();
    const adapter = globalThis.FancyGPTSites.chatgpt;

    // A long reasoning turn: the site keeps saying it is working, and produces
    // no new text for longer than the idle limit would allow on its own.
    const running = adapter.executeTurn("PROMPT-O", 20000, null, {idleTimeoutMs: 15000});
    await replyAfterSend("turn-1", "Thinking");
    await new Promise(resolve => setTimeout(resolve, 600));
    // Still generating, so the turn must still be alive.
    let finished = false;
    running.then(() => { finished = true; }, () => { finished = true; });
    await new Promise(resolve => setTimeout(resolve, 300));
    assert(finished === false, "a turn the site says is still working must not be killed");

    stop.remove();
    const turn = document.querySelectorAll("[data-turn-id]")[0];
    turn.setText(ENVELOPE);
    const result = await running;
    assertEqual(result.text, ENVELOPE, "it completes when the page actually finishes");
  });

  await test("a stalled turn reports how long it was inactive", async () => {
    loadAdapters(["site_chatgpt.js"]);
    const {composer, send} = chatgptPage();
    send.onclick = () => composer.setText("");
    const adapter = globalThis.FancyGPTSites.chatgpt;
    // Nothing ever appears and nothing claims to be generating.
    await rejects(
      adapter.executeTurn("PROMPT-P", 30000, null, {idleTimeoutMs: 15000}),
      /stalled|no activity/i,
      "a stall must be reported as inactivity, with its diagnostic",
    );
  });

  // -- fast replies must not be missed ---------------------------------------
  await test("chatgpt notices a reply that updates an existing turn", async () => {
    loadAdapters(["site_chatgpt.js"]);
    // A turn is already on screen when the prompt is submitted.
    const existing = assistantTurn("turn-existing", "an earlier answer");
    const {composer, send} = chatgptPage();
    send.onclick = () => composer.setText("");
    const adapter = globalThis.FancyGPTSites.chatgpt;

    const running = adapter.executeTurn("PROMPT-Q", 8000, null, {});
    // The page answers by updating that same turn rather than creating one.
    setTimeout(() => existing.setText(ENVELOPE), 60);

    const result = await running;
    // Treating "already in the baseline" as "not mine" skipped this forever.
    assertEqual(result.text, ENVELOPE, "an updated existing turn is still the reply");
    assertEqual(result.responseIdentity, "turn-existing", "bound to the turn that changed");
  });

  await test("chatgpt ignores an existing turn that never changes", async () => {
    loadAdapters(["site_chatgpt.js"]);
    assistantTurn("turn-old", "an earlier answer");
    const {composer, send} = chatgptPage();
    send.onclick = () => composer.setText("");
    const adapter = globalThis.FancyGPTSites.chatgpt;
    const running = adapter.executeTurn("PROMPT-R", 8000, null, {});
    await replyAfterSend("turn-new", ENVELOPE);
    const result = await running;
    // The untouched earlier answer must not be mistaken for this turn's reply.
    assertEqual(result.responseIdentity, "turn-new", "unchanged turns stay ignored");
  });

  await test("a generating indicator other than the stop control counts as active", async () => {
    loadAdapters(["site_chatgpt.js"]);
    const {composer, send} = chatgptPage();
    send.onclick = () => composer.setText("");
    const adapter = globalThis.FancyGPTSites.chatgpt;
    const running = adapter.executeTurn("PROMPT-S", 9000, null, {});
    await replyAfterSend("turn-1", ENVELOPE);
    // No stop control, but the page still says it is streaming.
    const streaming = new StubElement("div", {"data-is-streaming": "true"});
    document.body.append(streaming);

    let finished = false;
    running.then(() => { finished = true; }, () => { finished = true; });
    await new Promise(resolve => setTimeout(resolve, 900));
    assert(finished === false, "a streaming indicator must keep the turn open");

    streaming.remove();
    const result = await running;
    assertEqual(result.text, ENVELOPE, "it finishes once nothing claims to be working");
  });

  report();
}

main();
