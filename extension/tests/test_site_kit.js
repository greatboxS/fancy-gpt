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
    const stop = kit2.observeText(() => node.innerText, text => seen.push(text));
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
    const stop = kit2.observeText(() => node.innerText, text => seen.push(text));
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
    const stop = kit2.observeText(() => node.innerText, () => { calls += 1; throw new Error("consumer blew up"); });
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

  report();
}

main();
