/* Every adapter, down every path, in one pass.
 *
 * Two reference errors shipped in one day. Both parsed, both built, both left
 * every suite green, and both broke a real turn -- because nothing executed
 * the code they were in. One sat in Gemini's resume path, which no test ever
 * ran; the other in the page hook, which no test ever loaded. Each cost a
 * browser reload and a wrong diagnosis to find, one at a time.
 *
 * This runs all of it at once: every registered site, fresh and resuming,
 * cancelled and timing out, health-checked. It does not assert what a turn
 * returns -- the suites beside it do that -- it asserts that no path dies of
 * a name that is not there. A site is refused entry unless it has a fixture
 * here, so adding one cannot quietly skip this.
 */
const {loadAdapters, test, assert, report} = require("./harness.js");
const {StubElement} = require("./dom_stub.js");

const ENVELOPE = '{"type":"message","text":"done"}';

/* A page each adapter can find its own controls on. Deliberately per site:
 * a shared fixture would drift away from what one of them actually needs and
 * pass by finding nothing. */
const PAGES = {
  chatgpt(existing) {
    const composer = new StubElement("div", {contentEditable: "true", id: "prompt-textarea"});
    document.body.append(composer);
    document.body.append(new StubElement("button", {"data-testid": "send-button"}));
    document._composerTarget = composer;
    if (existing) {
      const turn = new StubElement("div", {"data-turn-id": "turn-old", "data-message-author-role": "assistant"});
      turn.append(new StubElement("div", {class: "markdown", text: "an earlier answer"}));
      document.body.append(turn);
    }
  },
  gemini(existing) {
    const composer = new StubElement("div", {contentEditable: "true", role: "textbox"});
    document.body.append(composer);
    document.body.append(new StubElement("button", {class: "send-button"}));
    document._composerTarget = composer;
    if (existing) document.body.append(new StubElement("model-response", {text: "an earlier answer"}));
  },
};

const HOSTS = {chatgpt: "chatgpt.com", gemini: "gemini.google.com"};
const SITE_FILES = {chatgpt: "site_chatgpt.js", gemini: "site_gemini.js"};

// A programming mistake, as opposed to a turn that legitimately gave up.
// "send control unavailable" is a finding; "x is not defined" is a defect.
const DEFECT = /is not defined|is not a function|Cannot read|Cannot access|undefined is not/i;

function begin(site, {existing = false} = {}) {
  loadAdapters([SITE_FILES[site]], HOSTS[site]);
  PAGES[site](existing);
  return globalThis.FancyGPTSites[site];
}

async function mustNotBeADefect(label, run) {
  try {
    await run();
  } catch (error) {
    const message = String(error && error.message || error);
    assert(!DEFECT.test(message), `${label}: ${message}`);
  }
}

async function main() {
  const sites = Object.keys(SITE_FILES);

  await test("every registered adapter has a fixture here", () => {
    loadAdapters(Object.values(SITE_FILES));
    for (const id of Object.keys(globalThis.FancyGPTSites)) {
      assert(PAGES[id], `${id} has no page fixture, so this suite would skip it silently`);
    }
  });

  for (const site of sites) {
    await test(`${site}: a fresh turn runs without a defect`, async () => {
      const adapter = begin(site);
      // The reply lands after submission, as a page reveals it.
      setTimeout(() => PAGES[site] && replyFor(site), 40);
      await mustNotBeADefect(`${site} fresh`, () =>
        adapter.executeTurn("PROMPT", 2500, null, {}));
    });

    await test(`${site}: resuming a conversation runs without a defect`, async () => {
      // The path that only exists when continuing, and the one that hid a
      // reference error behind a first turn that worked perfectly.
      const adapter = begin(site, {existing: true});
      setTimeout(() => replyFor(site), 40);
      await mustNotBeADefect(`${site} resume`, () =>
        adapter.executeTurn("PROMPT", 2500, null, {continuing: true}));
    });

    await test(`${site}: cancelling mid-turn runs without a defect`, async () => {
      const adapter = begin(site);
      await mustNotBeADefect(`${site} cancel`, () =>
        adapter.executeTurn("PROMPT", 2500, null, {isCancelled: () => true}));
    });

    await test(`${site}: giving up on an unanswered turn runs without a defect`, async () => {
      // Nothing ever replies. The timeout report is itself code, and it is the
      // code that runs least often and is read most closely when it does.
      const adapter = begin(site);
      await mustNotBeADefect(`${site} timeout`, () =>
        adapter.executeTurn("PROMPT", 900, null, {}));
    });

    await test(`${site}: a progress observer runs without a defect`, async () => {
      const adapter = begin(site);
      setTimeout(() => replyFor(site), 40);
      await mustNotBeADefect(`${site} progress`, () =>
        adapter.executeTurn("PROMPT", 2500, () => {}, {}));
    });

    await test(`${site}: the health check runs without a defect`, async () => {
      const adapter = begin(site);
      await mustNotBeADefect(`${site} health`, () => adapter.healthCheck());
    });
  }

  report();
}

function replyFor(site) {
  if (site === "chatgpt") {
    const turn = new StubElement("div", {"data-turn-id": "turn-new", "data-message-author-role": "assistant"});
    turn.append(new StubElement("div", {class: "markdown", text: ENVELOPE}));
    document.body.append(turn);
    return;
  }
  document.body.append(new StubElement("model-response", {text: ENVELOPE}));
}

main();
