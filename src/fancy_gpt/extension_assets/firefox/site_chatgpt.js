/* ChatGPT site adapter. Owns every ChatGPT DOM assumption. */
(() => {
  // Everything that is not a ChatGPT DOM assumption comes from the shared kit,
  // so a second adapter starts from what already works rather than repeating it.
  const kit = globalThis.FancyGPTSiteKit;
  const {firstVisible, waitFor, findButtonByText, setComposer, looksLikeCompleteJson, stopGeneration, observeText} = kit;

  const SELECTORS = {
    composer: ["#prompt-textarea", "textarea", '[contenteditable="true"]'],
    send: ['button[data-testid="send-button"]', 'button[aria-label*="Send"]'],
    stop: ['button[data-testid="stop-button"]', 'button[aria-label*="Stop"]'],
    turns: "[data-turn-id]",
    assistant: ['[data-message-author-role="assistant"]', '[data-testid="conversation-turn-assistant"]']
  };

  function composerRoot(composer) {
    return composer?.closest("form") ?? composer?.parentElement ?? document;
  }

  function sendControl(composer) {
    return firstVisible(SELECTORS.send, composerRoot(composer)) ?? firstVisible(SELECTORS.send);
  }

  function describeControls(composer) {
    const counts = {};
    for (const selector of [...SELECTORS.send, ...SELECTORS.composer]) {
      counts[selector] = document.querySelectorAll(selector).length;
    }
    const text = composer ? (composer.value ?? composer.textContent ?? "") : "";
    return JSON.stringify({
      url: location.pathname,
      composerFound: Boolean(composer),
      composerChars: text.length,
      matches: counts,
    });
  }

  function turnIds() {
    return [...document.querySelectorAll(SELECTORS.turns)]
      .map(el => el.getAttribute("data-turn-id"))
      .filter(Boolean);
  }

  function assistantText(turnId) {
    const turns = [...document.querySelectorAll(SELECTORS.turns)].filter(el => el.getAttribute("data-turn-id") === turnId);
    if (turns.length !== 1) return null;
    const turn = turns[0];
    const role = turn.getAttribute("data-message-author-role");
    if (role === "assistant") return (turn.innerText ?? "").trim() || null;
    for (const selector of SELECTORS.assistant) {
      if (turn.matches?.(selector)) return (turn.innerText ?? "").trim() || null;
      const child = turn.querySelector(selector);
      if (child) return (child.innerText ?? "").trim() || null;
    }
    return null;
  }

  function currentConversationId() {
    const match = location.pathname.match(/^\/c\/([a-zA-Z0-9-]+)/);
    return match ? match[1] : null;
  }

  async function healthCheck() {
    if (location.hostname !== "chatgpt.com") return {ok: false, reason: "unexpected-host", build: kit.build};
    const composer = firstVisible(SELECTORS.composer);
    return {
      ok: Boolean(composer),
      reason: composer ? "ready" : "composer-unavailable",
      build: kit.build,
    };
  }

  async function executeTurn(prompt, timeoutMs, onProgress, options = {}) {
    if (location.hostname !== "chatgpt.com") throw new Error("FancyGPT ChatGPT adapter loaded on unexpected host");
    const composer = await waitFor(() => firstVisible(SELECTORS.composer), 20000, "ChatGPT composer unavailable; sign in first");
    const baseline = new Set(turnIds());
    // Resuming an existing conversation lands on a page that is still hydrating:
    // the composer is already visible, but the app has not attached to it yet, so
    // a single write is silently dropped and the send button never appears. Keep
    // re-writing until the app acknowledges by revealing the send control.
    let send = null;
    let target = composer;
    for (let attempt = 0; attempt < 6 && !send; ++attempt) {
      target = firstVisible(SELECTORS.composer) || composer;
      setComposer(target, prompt);
      try {
        send = await waitFor(() => sendControl(target), 2500, "send control not ready yet");
      } catch (_) {
        await new Promise(resolve => setTimeout(resolve, 600));
      }
    }
    if (!send) {
      throw new Error("ChatGPT send control unavailable: " + describeControls(target));
    }
    send.click();

    const deadline = Date.now() + timeoutMs;
    let boundId = null;
    // Progress is reported by a DOM observer rather than by this loop, because
    // the loop's timer is throttled while the automation window is hidden.
    let stopObserving = null;
    // Cancellation is checked inside the poll loop: clicking stop ends
    // generation in the page, and whatever text exists is returned as partial.
    const checkCancelled = () => {
      if (!options?.isCancelled?.()) return null;
      const stopped = stopGeneration(SELECTORS.stop);
      return {
        text: boundId != null ? (assistantText(boundId) ?? "") : "",
        responseIdentity: boundId ?? "chatgpt-cancelled",
        conversationId: currentConversationId?.() ?? null,
        cancelled: true,
        stoppedGeneration: stopped,
      };
    };
    let stableText = null;
    let stableCount = 0;
    let lastReported = null;
    while (Date.now() < deadline) {
      const candidates = [];
      for (const id of turnIds()) {
        if (baseline.has(id)) continue;
        const text = assistantText(id);
        if (text) candidates.push({id, text});
      }
      if (boundId == null) {
        if (candidates.length > 1) throw new Error("ambiguous ChatGPT response: multiple new assistant turns");
        if (candidates.length === 1) boundId = candidates[0].id;
      }
      // Checked after binding, not before: a cancel arriving before the first
      // successful bind would otherwise discard a reply that is already on
      // screen and report an empty result.
      const cancelled = checkCancelled();
      if (cancelled) {
        if (stopObserving) stopObserving();
        return cancelled;
      }
      if (boundId != null) {
        // A long conversation may retain a visible "Continue generating"
        // button on an older response. Searching the whole document and
        // clicking it before binding the new response traps this job in the
        // loop forever. Only continue the assistant turn created by this job.
        const boundTurns = [...document.querySelectorAll(SELECTORS.turns)]
          .filter(el => el.getAttribute("data-turn-id") === boundId);
        const continueButton = boundTurns.length === 1
          ? findButtonByText(/continue generating/i, boundTurns[0])
          : null;
        if (continueButton) {
          continueButton.click();
          stableCount = 0;
          await new Promise(resolve => setTimeout(resolve, 500));
          continue;
        }
        if (onProgress && stopObserving === null && boundTurns.length === 1) {
          const observedId = boundId;
          // Scoped to this reply: observing the whole document would re-scan it
          // on every unrelated mutation the page makes.
          stopObserving = observeText(
            () => assistantText(observedId),
            text => { lastReported = text; onProgress(text); },
            {target: boundTurns[0]},
          );
        }
        const text = assistantText(boundId);
        const streaming = Boolean(firstVisible(SELECTORS.stop));
        const complete = text != null && looksLikeCompleteJson(text);
        if (text && text === stableText && !streaming && complete) stableCount += 1;
        else stableCount = 0;
        stableText = text;
        if (text && stableCount >= 3) {
          if (stopObserving) stopObserving();
          return {text, responseIdentity: boundId, conversationId: currentConversationId()};
        }
      }
      await new Promise(resolve => setTimeout(resolve, 500));
    }
    if (stopObserving) stopObserving();
    throw new Error("ChatGPT response timed out");
  }

  globalThis.FancyGPTSites = globalThis.FancyGPTSites ?? {};
  globalThis.FancyGPTSites.chatgpt = {
    id: "chatgpt",
    freshUrl: "https://chatgpt.com/?temporary-chat=true",
    healthCheck,
    executeTurn
  };
})();
