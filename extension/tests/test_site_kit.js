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
    assert(kit2.looksLikeCompleteJson('{"type":"message","text":"nested { brace }"}') === true,
      "braces inside strings are data, not structure");
    assert(kit2.looksLikeCompleteJson('{"type":"message","text":"escaped \\\"}\\\""}') === true,
      "escaped quotes and braces are parsed correctly");
    assert(kit2.looksLikeCompleteJson('{"type":"message","text":}') === false,
      "balanced but invalid JSON is incomplete");
    // Taken from a live turn that was accepted as finished and handed back a
    // document cut off mid-key. The outer object never closed; the scan used to
    // walk on into it, find the complete [] of an inner field, and settle.
    assert(kit2.looksLikeCompleteJson('{"answer":"done","material_context":[],"unknowns":[],"next_actio') === false,
      "an unclosed object is unfinished however complete its inner values are");
    assert(kit2.looksLikeCompleteJson('{"a":"partial') === false,
      "a reply cut off inside a string is unfinished");
    assert(kit2.looksLikeCompleteJson('I will use {placeholders} here.\n{"a":1}') === true,
      "prose that merely contains a brace is stepped over, not treated as the envelope");
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

  await test("activity arriving between waits is remembered", async () => {
    loadAdapters([]);
    const waiter = globalThis.FancyGPTSiteKit.createActivityWaiter();
    // This is the old blind spot: no promise is waiting when activity arrives.
    waiter.notify();
    const started = Date.now();
    assertEqual(await waiter.wait(3000), "activity", "latched activity wakes the next wait");
    assert(Date.now() - started < 100, "must not fall through to the throttled timeout");
    assertEqual(waiter.stats().immediateWakes, 1, "lost edge was consumed exactly once");
    waiter.stop();
  });

  await test("completion gate exposes its remaining stability deadline", () => {
    loadAdapters([]);
    let clock = 100;
    const gate = globalThis.FancyGPTSiteKit.createCompletionGate({stabilityMs: 1200, now: () => clock});
    gate.observe({text: '{"type":"message","text":"done"}', active: false, complete: true});
    assertEqual(gate.remainingMs, 1200, "candidate begins with a full stability window");
    clock += 450;
    assertEqual(gate.remainingMs, 750, "deadline decreases independently of mutations");
  });

  await test("readLiveText reinstates line structure textContent drops", () => {
    loadAdapters([]);
    const {readLiveText} = globalThis.FancyGPTSiteKit;
    const root = new StubElement("div");
    const pre = new StubElement("pre");
    const header = new StubElement("div");
    header.append(new StubElement("div", {text: "fancygpt:E1-OLD"}));
    header.append(new StubElement("button", {text: "Copy"}));
    pre.append(header);
    pre.append(new StubElement("code", {text: "    def run(self):\n        return 1"}));
    root.append(new StubElement("p", {text: "Here is the change."}));
    root.append(pre);

    const text = readLiveText(root);
    // The Copy control is kept. Nothing is dropped by guesswork: the reader has
    // no idea what is chrome and what is the answer, and a stray label on its
    // own line costs nothing, while dropping a subtree can cost the reply.
    assertEqual(text,
      "Here is the change.\n```fancygpt:E1-OLD\n    def run(self):\n        return 1\n```",
      "the fence is restored around the block, with its label as the info string");
  });

  await test("readLiveText keeps a highlighted code line intact", () => {
    loadAdapters([]);
    const {readLiveText} = globalThis.FancyGPTSiteKit;
    const code = new StubElement("code");
    // Syntax highlighting splits one line into a span per token. Treating those
    // as line boundaries would scatter the line across the output.
    for (const token of ["def ", "run", "(self):"]) code.append(new StubElement("span", {text: token}));
    assertEqual(readLiveText(code), "def run(self):", "tokens rejoin into one line");
  });

  await test("readLiveText skips nodes the caller marks as chrome", () => {
    loadAdapters([]);
    const {readLiveText} = globalThis.FancyGPTSiteKit;
    const root = new StubElement("div");
    const banner = new StubElement("div", {text: "Gemini said"});
    root.append(banner);
    root.append(new StubElement("p", {text: "the answer"}));
    assertEqual(readLiveText(root, {skip: new Set([banner])}), "the answer", "chrome removed");
  });

  await test("readLiveText keeps the indentation of a reply that is one block", () => {
    loadAdapters([]);
    const {readLiveText} = globalThis.FancyGPTSiteKit;
    const pre = new StubElement("pre");
    pre.append(new StubElement("code", {text: "    def run(self):\n        return 1"}));
    // A trailing trim on the joined output would silently eat the first line's
    // indentation, and the contract matches the OLD block character for character.
    assertEqual(readLiveText(pre), "```\n    def run(self):\n        return 1\n```",
      "leading indentation survives, inside the fence the model originally wrote");
  });

  await test("readLiveText keeps inline code inside its sentence", () => {
    loadAdapters([]);
    const {readLiveText} = globalThis.FancyGPTSiteKit;
    const p = new StubElement("p");
    p.append(new StubElement("span", {text: "set "}));
    p.append(new StubElement("code", {text: "x=1"}));
    p.append(new StubElement("span", {text: " first"}));
    assertEqual(readLiveText(p), "set x=1 first", "inline code is not a line break");
  });

  await test("readLiveText never loses content, whatever the markup", () => {
    loadAdapters([]);
    const {readLiveText} = globalThis.FancyGPTSiteKit;
    const root = new StubElement("div");
    // A wrapper the reader has no reason to understand. Earlier versions
    // treated such nodes as chrome and returned a fragment of the reply.
    const wrapper = new StubElement("div", {"aria-hidden": "true"});
    wrapper.append(new StubElement("p", {text: '{"type":"message","text":"done"}'}));
    root.append(wrapper);
    const text = readLiveText(root);
    assert(text.includes('{"type":"message","text":"done"}'),
      "an unrecognised wrapper must not hide the answer: " + JSON.stringify(text));
  });

  await test("visibility watcher remembers a document that went hidden", () => {
    loadAdapters([]);
    const watcher = globalThis.FancyGPTSiteKit.watchVisibility();
    assertEqual(watcher.state.hiddenDuringTurn, false, "starts visible");
    document.setVisibility("hidden");
    document.setVisibility("visible");
    // Visible again by the time anyone asks, but the turn still ran through a
    // stretch where the page was not being painted -- which is the thing worth
    // knowing when a reply comes back truncated.
    assertEqual(watcher.state.visibility, "visible", "reports the current state");
    assertEqual(watcher.state.hiddenDuringTurn, true, "and that it was hidden at some point");
    watcher.stop();
    document.setVisibility("hidden");
    assertEqual(watcher.state.hiddenDuringTurn, true, "stop detaches without losing what it saw");
  });

  await test("readLiveText puts the fence back around a rendered code block", () => {
    loadAdapters([]);
    const {readLiveText} = globalThis.FancyGPTSiteKit;
    const md = new StubElement("div", {class: "markdown"});
    const block = (label, body) => {
      const pre = new StubElement("pre");
      const header = new StubElement("div");
      header.append(new StubElement("div", {text: label}));
      header.append(new StubElement("button"));   // the Copy control: an icon, no text
      pre.append(header);
      pre.append(new StubElement("code", {text: body}));
      md.append(pre);
    };
    block("json", '{"edit":{"new_ref":"E1-NEW"}}');
    block("fancygpt:E1-NEW", "    def alpha():\n        return 2");
    md.append(new StubElement("p", {text: "Let me know if you want tests added."}));

    // Markdown renders the fence away, leaving the info string as a bare line
    // above the code and nothing at all below it. Read back like that, the last
    // block has no end: it swallows the closing sentence, and that text is then
    // written into the repository as source.
    assertEqual(readLiveText(md), [
      "```json",
      '{"edit":{"new_ref":"E1-NEW"}}',
      "```",
      "```fancygpt:E1-NEW",
      "    def alpha():",
      "        return 2",
      "```",
      "Let me know if you want tests added.",
    ].join("\n"), "every rendered block comes back delimited");
  });

  await test("a bare pre without code is still read verbatim", () => {
    loadAdapters([]);
    const {readLiveText} = globalThis.FancyGPTSiteKit;
    const pre = new StubElement("pre", {text: "    two spaces kept"});
    assertEqual(readLiveText(pre), "    two spaces kept", "no fence to restore, nothing invented");
  });

  await test("a hidden document does not make every control disappear", () => {
    /* A browser does not paint a tab it is not showing.
     *
     * A never-painted document can report every element as zero-sized, and
     * this check used to require a box with size -- so an automated turn in a
     * hidden tab would fail as "composer unavailable" on a page that was
     * perfectly ready. Reading the reply no longer needs the page painted, so
     * neither may finding the controls.
     */
    loadAdapters([]);
    const {visible} = globalThis.FancyGPTSiteKit;
    const control = new StubElement("button", {"data-testid": "send-button"});
    document.body.append(control);

    document.setVisibility("hidden");
    assert(visible(control), "a control in a hidden document is still usable");

    // What the site itself hides is still refused, in either state.
    control.hidden = true;
    assert(!visible(control), "display:none is the site's decision, not the browser's");
    document.setVisibility("visible");
    assert(!visible(control), "and it holds when the tab comes back");
  });

  report();
}

main();
