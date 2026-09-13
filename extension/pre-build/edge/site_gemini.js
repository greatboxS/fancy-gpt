/* Gemini site adapter. Owns every gemini.google.com DOM assumption.
 *
 * The shared kit supplies everything that is not site-specific; what lives here
 * is the selector set, how a reply is identified, and how a conversation id is
 * read back. Those are the parts that drift when the site changes, which is why
 * they are isolated to this file.
 */
(() => {
  const kit = globalThis.FancyGPTSiteKit;
  const {firstVisible, waitFor, setComposer, looksLikeCompleteJson, stopGeneration,
         observeText, createCompletionGate, createActivityWaiter} = kit;

  const SELECTORS = {
    composer: [
      'rich-textarea div[contenteditable="true"]',
      'div[contenteditable="true"][role="textbox"]',
      "textarea",
    ],
    // Structural anchors first, because matching aria-label text only works in
    // the languages we happen to have listed: a Spanish or French UI matched
    // none of them and the turn failed as "send control unavailable".
    send: [
      "button.send-button",
      'button[data-test-id="send-button"]',
      'button[aria-label*="Send" i]',
      'button[aria-label*="Gửi" i]',
    ],
    stop: [
      "button.stop-button",
      'button[data-test-id="stop-button"]',
      'button[aria-label*="Stop" i]',
      'button[aria-label*="Dừng" i]',
    ],
    responses: ["message-content.model-response-text", "model-response"],
    // Other ways the page says it is still working, beyond the stop control.
    generating: [
      '[aria-busy="true"]',
      "model-thoughts[data-thinking='true']",
      ".response-loading",
      "mat-progress-bar",
    ],
  };

  function currentConversationId() {
    const match = location.pathname.match(/^\/app\/([a-zA-Z0-9-]+)/);
    return match ? match[1] : null;
  }

  /* Is the page still working on this turn?
   *
   * The stop control alone is not enough: it also disappears between a search
   * or tool phase and the text that follows. A selector that does not match
   * contributes nothing, so listing several costs nothing.
   */
  function isGenerating() {
    if (firstVisible(SELECTORS.stop)) return true;
    for (const selector of SELECTORS.generating) {
      try { if (document.querySelector(selector)) return true; } catch (_) {}
    }
    return false;
  }

  function responseNodes() {
    for (const selector of SELECTORS.responses) {
      const found = [...document.querySelectorAll(selector)];
      if (found.length) return found;
    }
    return [];
  }

  /* Site chrome that reads as text but is not part of the reply.
   *
   * innerText on the response element drags in action buttons ("Copy", "Good
   * response"), reasoning panels ("Show thinking"), citation lists and draft
   * switchers. None of that came from the model, and all of it corrupts the
   * envelope the gateway has to parse.
   */
  const CHROME_SELECTORS = [
    "button",
    "[role=\"button\"]",
    "[role=\"toolbar\"]",
    "model-thoughts",
    "sources-list",
    "message-actions",
    ".action-bar",
    ".thought-panel",
  ];

  function readWithoutChrome(root) {
    const chrome = new Set();
    for (const selector of CHROME_SELECTORS) {
      let matches = [];
      try { matches = [...root.querySelectorAll(selector)]; } catch (_) { continue; }
      for (const node of matches) chrome.add(node);
    }
    if (chrome.size === 0) return (root.textContent ?? root.innerText ?? "").trim();
    // Read the parts that are not chrome, rather than string-subtracting the
    // chrome afterwards: the same words can legitimately appear in the reply.
    const parts = [];
    const walk = node => {
      if (chrome.has(node)) return;
      if (!node.children || node.children.length === 0) {
        const text = (node.textContent ?? node.innerText ?? "").trim();
        if (text) parts.push(text);
        return;
      }
      for (const child of node.children) walk(child);
    };
    walk(root);
    return parts.join("\n").trim();
  }

  function latestResponseText(baselineCount) {
    const nodes = responseNodes();
    // Bind to the reply this turn produced, never to whatever is on screen: an
    // existing conversation is already full of earlier answers.
    if (nodes.length <= baselineCount) return null;
    const node = nodes[nodes.length - 1];
    // The response element wraps the answer in the site's own chrome -- an
    // attribution line such as "Gemini said" -- which is not part of what the
    // model replied and must not reach the parser.
    const content = node.querySelector("message-content") ?? node;
    // Gemini renders fenced JSON with a visual language label ("JSON") beside
    // the actual code. innerText includes that site chrome, while the code
    // element contains only the model payload.
    const codeBlocks = [...content.querySelectorAll("code")]
      .map(element => (element.textContent ?? element.innerText ?? "").trim())
      .filter(Boolean);
    if (codeBlocks.length === 1) return codeBlocks[0];
    const text = readWithoutChrome(content);
    return text || null;
  }

  async function settledResponseCount(continuing) {
    if (!continuing) return responseNodes().length;
    // Gemini hydrates an existing thread after the composer becomes available.
    // A baseline captured before that hydration mistakes the previous answer for
    // the reply to the new prompt.
    await waitFor(
      () => responseNodes().length || null,
      15000,
      "Gemini conversation history did not load",
    );
    let lastCount = -1;
    let stableSince = Date.now();
    const deadline = Date.now() + 10000;
    while (Date.now() < hardDeadline && Date.now() - lastActivityAt < idleLimitMs) {
      const count = responseNodes().length;
      if (count !== lastCount) {
        lastCount = count;
        stableSince = Date.now();
      } else if (Date.now() - stableSince >= 1500) {
        return count;
      }
      await new Promise(resolve => setTimeout(resolve, 200));
    }
    return responseNodes().length;
  }

  async function healthCheck() {
    if (location.hostname !== "gemini.google.com") {
      return {ok: false, reason: "unexpected-host", build: kit.build};
    }
    const composer = firstVisible(SELECTORS.composer);
    return {
      ok: Boolean(composer),
      reason: composer ? "ready" : "composer-unavailable",
      build: kit.build,
    };
  }

  async function executeTurn(prompt, timeoutMs, onProgress, options = {}) {
    if (location.hostname !== "gemini.google.com") {
      throw new Error("FancyGPT Gemini adapter loaded on unexpected host");
    }
    const composer = await waitFor(
      () => firstVisible(SELECTORS.composer), 20000, "Gemini composer unavailable; sign in first",
    );
    const baselineCount = await settledResponseCount(Boolean(options.continuing));

    // An established editor drops a single write, so keep writing until the send
    // control appears -- that control is the only reliable acknowledgement that
    // the editor accepted the text.
    let send = null;
    let target = composer;
    for (let attempt = 0; attempt < 6 && !send; ++attempt) {
      target = firstVisible(SELECTORS.composer) || composer;
      setComposer(target, prompt);
      try {
        send = await waitFor(
          () => firstVisible(SELECTORS.send, target.closest("form") ?? document) ?? firstVisible(SELECTORS.send),
          2500,
          "send control not ready yet",
        );
      } catch (_) {
        await new Promise(resolve => setTimeout(resolve, 600));
      }
    }
    if (!send) {
      throw new Error(
        "Gemini send control unavailable: " + JSON.stringify({
          path: location.pathname,
          composerChars: (target?.textContent ?? "").length,
          sendMatches: SELECTORS.send.map(s => document.querySelectorAll(s).length),
        }),
      );
    }
    send.click();

    // Give up on inactivity rather than elapsed time: a long reasoning turn
    // can legitimately outrun any fixed deadline, and the page is the thing
    // that knows whether it is still working.
    const idleLimitMs = Math.max(15000, Number(options?.idleTimeoutMs ?? 90000));
    const hardDeadline = Date.now() + timeoutMs;
    let lastActivityAt = Date.now();
    let lastSeenText = null;
    let lastReported = null;
    const gate = createCompletionGate({stabilityMs: 1200});
    const activity = createActivityWaiter();
    let tickCount = 0;
    let loopIterations = 0;
    let maxEvaluateMs = 0;
    // An unthrottled clock from the background worker: a finished reply
    // produces no further mutations to wake this loop with.
    if (options?.onTick) options.onTick(() => { tickCount += 1; activity.notify(); });
    // Driven by DOM mutations, because this loop's timer is throttled while the
    // automation window is hidden.
    const stopObserving = onProgress
      ? observeText(
          () => latestResponseText(baselineCount),
          text => { lastReported = text; onProgress(text); },
        )
      : null;
    const release = () => { activity.stop(); if (stopObserving) stopObserving(); };
    while (Date.now() < hardDeadline && Date.now() - lastActivityAt < idleLimitMs) {
      const evaluateStartedAt = Date.now();
      if (options?.isCancelled?.()) {
        const stopped = stopGeneration(SELECTORS.stop);
        release();
        return {
          text: latestResponseText(baselineCount) ?? "",
          responseIdentity: `gemini-response-${baselineCount + 1}`,
          conversationId: currentConversationId(),
          cancelled: true,
          stoppedGeneration: stopped,
        };
      }
      const text = latestResponseText(baselineCount);
      // The stop control also vanishes between a search or tool phase and the
      // text that follows, so its absence alone must not end the turn.
      const active = isGenerating();
      if (active || text !== lastSeenText) {
        lastActivityAt = Date.now();
        lastSeenText = text;
      }
      const complete = text != null && looksLikeCompleteJson(text);
      if (gate.observe({text, active, complete})) {
        maxEvaluateMs = Math.max(maxEvaluateMs, Date.now() - evaluateStartedAt);
        const diagnostics = {
          ticksReceived: tickCount, loopIterations,
          maxEvaluateMs, waiter: activity.stats(), completionCandidateAgeMs: gate.candidateAgeMs,
        };
        release();
        return {
          text,
          responseIdentity: `gemini-response-${baselineCount + 1}`,
          conversationId: currentConversationId(),
          diagnostics,
        };
      }
      maxEvaluateMs = Math.max(maxEvaluateMs, Date.now() - evaluateStartedAt);
      // Woken by a mutation or a background tick; the delay is only a floor.
      loopIterations += 1;
      const waitMs = gate.remainingMs == null ? 500 : Math.max(1, Math.min(500, gate.remainingMs));
      await activity.wait(waitMs);
    }
    release();
    const idleFor = Math.round((Date.now() - lastActivityAt) / 1000);
    throw new Error((Date.now() >= hardDeadline
        ? "Gemini response exceeded the absolute limit; "
        : `Gemini response stalled: no activity for ${idleFor}s; `)
      + JSON.stringify({
        ticksReceived: tickCount, loopIterations, maxEvaluateMs,
        waiter: activity.stats(), candidatePending: gate.pending,
        completionCandidateAgeMs: gate.candidateAgeMs,
      }));
  }

  globalThis.FancyGPTSites = globalThis.FancyGPTSites ?? {};
  globalThis.FancyGPTSites.gemini = {
    id: "gemini",
    freshUrl: "https://gemini.google.com/app",
    healthCheck,
    executeTurn,
  };
})();
