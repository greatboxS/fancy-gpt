/* End-to-end tests of the real adapters against a scripted page.
 *
 * These execute executeTurn itself, so a change that breaks how a turn is
 * submitted, observed, settled or cancelled fails here rather than in a live
 * browser run.
 */
const {loadAdapters, test, assert, assertEqual, rejects, report} = require("./harness.js");
const {StubElement} = require("./dom_stub.js");

const ENVELOPE = '{"type":"message","text":"done"}';

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

  await test("chatgpt cancellation before any reply still returns cleanly", async () => {
    loadAdapters(["site_chatgpt.js"]);
    chatgptPage();
    const adapter = globalThis.FancyGPTSites.chatgpt;
    const running = adapter.executeTurn("PROMPT-H", 6000, null, {isCancelled: () => true});
    const result = await running;
    assert(result.cancelled === true, "cancelled");
    // Nothing was generating, so there was no stop control to click.
    assert(result.stoppedGeneration === false, "no stop control means nothing to stop");
    assertEqual(result.text, "", "no partial text existed");
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

  report();
}

main();
