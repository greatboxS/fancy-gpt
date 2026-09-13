/* Behavioural tests for the shared adapter primitives. */
const {loadAdapters, test, assert, assertEqual, report} = require("./harness.js");
const {StubElement} = require("./dom_stub.js");

async function main() {
  loadAdapters([]);
  const kit = globalThis.FancyGPTSiteKit;

  await test("firstVisible refuses an ambiguous selector", () => {
    const a = new StubElement("button", {"data-testid": "send-button"});
    const b = new StubElement("button", {"data-testid": "send-button"});
    document.body.append(a); document.body.append(b);
    // Two matches is ambiguous: binding to either could send the prompt nowhere.
    assertEqual(kit.firstVisible(['button[data-testid="send-button"]']), null, "ambiguous should be null");
    b.remove();
    assert(kit.firstVisible(['button[data-testid="send-button"]']) === a, "single match should bind");
  });

  await test("firstVisible skips hidden elements", () => {
    loadAdapters([]);
    const hidden = new StubElement("button", {"data-testid": "send-button"});
    hidden.hidden = true;
    document.body.append(hidden);
    assertEqual(globalThis.FancyGPTSiteKit.firstVisible(['button[data-testid="send-button"]']), null,
      "a hidden control must not be treated as available");
  });

  await test("stopGeneration clicks the site's own stop control", () => {
    loadAdapters([]);
    const stop = new StubElement("button", {"data-testid": "stop-button"});
    document.body.append(stop);
    assert(globalThis.FancyGPTSiteKit.stopGeneration(['button[data-testid="stop-button"]']) === true, "should report stopped");
    assertEqual(stop.clicks, 1, "stop must be clicked exactly once");
  });

  await test("stopGeneration reports false when generation already finished", () => {
    loadAdapters([]);
    // No stop control means nothing is generating; that is 'already terminal',
    // not a failed cancel.
    assert(globalThis.FancyGPTSiteKit.stopGeneration(['button[data-testid="stop-button"]']) === false, "should report nothing to stop");
  });

  await test("observeText reports on DOM mutation, not on a timer", () => {
    loadAdapters([]);
    const kit2 = globalThis.FancyGPTSiteKit;
    const node = new StubElement("div", {id: "reply", text: "one"});
    document.body.append(node);
    const seen = [];
    const stop = kit2.observeText(() => node.innerText, text => seen.push(text), {minIntervalMs: 0});
    node.setText("one two");
    node.setText("one two three");
    stop();
    node.setText("after disconnect");
    // The first report happens immediately, then once per change, and nothing
    // after disconnect.
    assertEqual(seen, ["one", "one two", "one two three"], "observer stream");
  });

  await test("observeText never reports the same text twice", () => {
    loadAdapters([]);
    const kit2 = globalThis.FancyGPTSiteKit;
    const node = new StubElement("div", {text: "same"});
    document.body.append(node);
    const seen = [];
    const stop = kit2.observeText(() => node.innerText, text => seen.push(text), {minIntervalMs: 0});
    node.setText("same");
    node.setText("same");
    stop();
    assertEqual(seen, ["same"], "duplicate snapshots must be suppressed");
  });

  await test("observeText survives a throwing consumer", () => {
    loadAdapters([]);
    const kit2 = globalThis.FancyGPTSiteKit;
    const node = new StubElement("div", {text: "a"});
    document.body.append(node);
    let calls = 0;
    const stop = kit2.observeText(
      () => node.innerText,
      () => { calls += 1; throw new Error("consumer blew up"); },
      {minIntervalMs: 0},
    );
    node.setText("b");
    stop();
    // Progress is a monitoring aid; a bad consumer must not break the turn.
    assert(calls >= 2, "observer kept reporting after a consumer error");
  });

  await test("observeText disconnects cleanly", () => {
    loadAdapters([]);
    const kit2 = globalThis.FancyGPTSiteKit;
    const node = new StubElement("div", {text: "a"});
    document.body.append(node);
    const stop = kit2.observeText(() => node.innerText, () => {});
    assertEqual(document.observers.length, 1, "observer registered");
    stop();
    assertEqual(document.observers.length, 0, "observer must not leak");
  });

  await test("observeText rate limits reads under a mutation storm", () => {
    loadAdapters([]);
    const kit2 = globalThis.FancyGPTSiteKit;
    const node = new StubElement("div", {text: "x"});
    document.body.append(node);
    let reads = 0;
    const stop = kit2.observeText(
      () => { reads += 1; return node.innerText; },
      () => {},
      {minIntervalMs: 10000},
    );
    // A streaming page mutates hundreds of times a second and each read forces
    // layout. Re-reading on every mutation starved the completion loop and hung
    // the turn, so reads must be bounded rather than driven one-per-mutation.
    for (let i = 0; i < 500; ++i) node.setText("x".repeat(i + 2));
    stop();
    assertEqual(reads, 1, "reads must be rate limited, not one per mutation");
  });

  await test("observeText scopes its observation to the node it is given", () => {
    loadAdapters([]);
    const kit2 = globalThis.FancyGPTSiteKit;
    const watched = new StubElement("div", {text: "watched"});
    document.body.append(watched);
    const stop = kit2.observeText(() => watched.innerText, () => {}, {target: watched, minIntervalMs: 0});
    // The observer is attached to the given node, not the whole document.
    assertEqual(document.observers.length, 1, "exactly one observer");
    stop();
    assertEqual(document.observers.length, 0, "disconnected");
  });

  await test("observeText backs off when reading the page is expensive", () => {
    loadAdapters([]);
    const kit2 = globalThis.FancyGPTSiteKit;
    const node = new StubElement("div", {text: "x"});
    document.body.append(node);
    let reads = 0;
    const stop = kit2.observeText(
      () => {
        reads += 1;
        // Simulate a read that forces an expensive layout.
        const until = Date.now() + 60;
        while (Date.now() < until) { /* spin */ }
        return node.innerText;
      },
      () => {},
      {minIntervalMs: 0, slowReadMs: 10, maxIntervalMs: 5000},
    );
    for (let i = 0; i < 50; ++i) node.setText("x".repeat(i + 2));
    stop();
    // Without back-off this would read once per mutation and starve the turn.
    assert(reads <= 3, `expected back-off to bound reads, got ${reads}`);
  });

  await test("observeText returns to its base interval once reads are cheap", () => {
    loadAdapters([]);
    const kit2 = globalThis.FancyGPTSiteKit;
    const node = new StubElement("div", {text: "a"});
    document.body.append(node);
    const seen = [];
    const stop = kit2.observeText(() => node.innerText, text => seen.push(text), {minIntervalMs: 0, slowReadMs: 10});
    node.setText("b");
    node.setText("c");
    stop();
    // Cheap reads must not be penalised by the back-off machinery.
    assertEqual(seen, ["a", "b", "c"], "cheap reads keep reporting every change");
  });

  await test("looksLikeCompleteJson distinguishes finished envelopes", () => {
    loadAdapters([]);
    const kit2 = globalThis.FancyGPTSiteKit;
    assert(kit2.looksLikeCompleteJson('{"type":"message","text":"done"}') === true, "complete");
    assert(kit2.looksLikeCompleteJson('{"type":"message","text":"partial') === false, "partial");
  });

  await test("settle tracker waits for the stop control to disappear", () => {
    loadAdapters([]);
    const tracker = globalThis.FancyGPTSiteKit.createSettleTracker(3);
    const json = '{"type":"message","text":"done"}';
    // Still streaming: never settles however stable the text looks.
    for (let i = 0; i < 10; ++i) assert(tracker.observe(json, {streaming: true}) === false, "must not settle while streaming");
    assert(tracker.observe(json, {streaming: false}) === false, "one quiet poll is not enough");
    assert(tracker.observe(json, {streaming: false}) === false, "two is not enough");
    assert(tracker.observe(json, {streaming: false}) === true, "settles after the required quiet polls");
  });

  await test("settle tracker resets when the text changes again", () => {
    loadAdapters([]);
    const tracker = globalThis.FancyGPTSiteKit.createSettleTracker(3);
    const json = '{"type":"message","text":"a"}';
    tracker.observe(json, {streaming: false});
    tracker.observe(json, {streaming: false});
    tracker.observe('{"type":"message","text":"ab"}', {streaming: false});
    assert(tracker.observe('{"type":"message","text":"ab"}', {streaming: false}) === false,
      "the stability count must restart when new text arrives");
  });

  // -- completion gate: the consensus of both independent reviews ------------
  await test("gate does not finish while the site says it is generating", () => {
    loadAdapters([]);
    let clock = 0;
    const gate = globalThis.FancyGPTSiteKit.createCompletionGate({stabilityMs: 100, now: () => clock});
    const text = '{"type":"message","text":"done"}';
    for (let i = 0; i < 20; ++i) {
      clock += 50;
      assertEqual(gate.observe({text, active: true, complete: true}), null, "must not finish while active");
    }
  });

  await test("gate requires the page to stay quiet before finishing", () => {
    loadAdapters([]);
    let clock = 0;
    const gate = globalThis.FancyGPTSiteKit.createCompletionGate({stabilityMs: 1000, now: () => clock});
    const text = '{"type":"message","text":"done"}';
    assertEqual(gate.observe({text, active: false, complete: true}), null, "the first quiet look only opens a candidate");
    clock += 999;
    assertEqual(gate.observe({text, active: false, complete: true}), null, "still inside the window");
    clock += 2;
    assertEqual(gate.observe({text, active: false, complete: true}), {text}, "finishes once quiet for long enough");
  });

  await test("a tool phase between generations cannot finish the turn", () => {
    loadAdapters([]);
    let clock = 0;
    const gate = globalThis.FancyGPTSiteKit.createCompletionGate({stabilityMs: 500, now: () => clock});
    const partial = '{"type":"message","text":"before tool"}';
    // The stop control vanishes while a tool runs: looks finished, is not.
    gate.observe({text: partial, active: false, complete: true});
    clock += 200;
    // The tool phase starts: activity cancels the candidate.
    assertEqual(gate.observe({text: partial, active: true, complete: true}), null, "activity cancels");
    clock += 5000;
    assertEqual(gate.observe({text: partial, active: true, complete: true}), null, "still generating");
    // Text resumes and eventually settles.
    const finalText = '{"type":"message","text":"before tool and after"}';
    assertEqual(gate.observe({text: finalText, active: false, complete: true}), null, "new text restarts the window");
    clock += 600;
    assertEqual(gate.observe({text: finalText, active: false, complete: true}), {text: finalText}, "finishes on the real end");
  });

  await test("a momentarily valid JSON prefix does not finish the turn", () => {
    loadAdapters([]);
    let clock = 0;
    const gate = globalThis.FancyGPTSiteKit.createCompletionGate({stabilityMs: 500, now: () => clock});
    // The stream briefly forms a complete object, then keeps going.
    assertEqual(gate.observe({text: '{"type":"message"}', active: false, complete: true}), null, "candidate only");
    clock += 300;
    assertEqual(gate.observe({text: '{"type":"message","text":"more"}', active: false, complete: true}), null,
      "more bytes restart the window");
    clock += 300;
    assertEqual(gate.observe({text: '{"type":"message","text":"more"}', active: false, complete: true}), null,
      "not yet quiet for long enough");
    clock += 300;
    assert(gate.observe({text: '{"type":"message","text":"more"}', active: false, complete: true}) !== null,
      "finishes once it really stops changing");
  });

  await test("incomplete text never opens a candidate", () => {
    loadAdapters([]);
    let clock = 0;
    const gate = globalThis.FancyGPTSiteKit.createCompletionGate({stabilityMs: 10, now: () => clock});
    assertEqual(gate.observe({text: '{"type":"message","text":"half', active: false, complete: false}), null, "incomplete");
    clock += 10000;
    assertEqual(gate.observe({text: '{"type":"message","text":"half', active: false, complete: false}), null,
      "time alone must not finish an incomplete reply");
    assert(gate.pending === false, "no candidate is pending");
  });

  report();
}

main();
