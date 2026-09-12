/* Gemini site adapter. Owns every gemini.google.com DOM assumption.
 *
 * The shared kit supplies everything that is not site-specific; what lives here
 * is the selector set, how a reply is identified, and how a conversation id is
 * read back. Those are the parts that drift when the site changes, which is why
 * they are isolated to this file.
 */
(() => {
  const kit = globalThis.FancyGPTSiteKit;
  const {firstVisible, waitFor, setComposer, createSettleTracker} = kit;

  const SELECTORS = {
    composer: [
      'rich-textarea div[contenteditable="true"]',
      'div[contenteditable="true"][role="textbox"]',
      "textarea",
    ],
    send: [
      'button[aria-label*="Send" i]',
      'button[aria-label*="Gửi" i]',
      "button.send-button",
    ],
    stop: [
      'button[aria-label*="Stop" i]',
      'button[aria-label*="Dừng" i]',
      "button.stop-button",
    ],
    responses: ["model-response", "message-content.model-response-text"],
  };

  function currentConversationId() {
    const match = location.pathname.match(/^\/app\/([a-zA-Z0-9-]+)/);
    return match ? match[1] : null;
  }

  function responseNodes() {
    for (const selector of SELECTORS.responses) {
      const found = [...document.querySelectorAll(selector)];
      if (found.length) return found;
    }
    return [];
  }

  function latestResponseText(baselineCount) {
    const nodes = responseNodes();
    // Bind to the reply this turn produced, never to whatever is on screen: an
    // existing conversation is already full of earlier answers.
    if (nodes.length <= baselineCount) return null;
    const text = (nodes[nodes.length - 1].innerText ?? "").trim();
    return text || null;
  }

  async function healthCheck() {
    if (location.hostname !== "gemini.google.com") {
      return {ok: false, reason: "unexpected-host", build: globalThis.FANCYGPT_ADAPTER_BUILD ?? null};
    }
    const composer = firstVisible(SELECTORS.composer);
    return {
      ok: Boolean(composer),
      reason: composer ? "ready" : "composer-unavailable",
      build: globalThis.FANCYGPT_ADAPTER_BUILD ?? null,
    };
  }

  async function executeTurn(prompt, timeoutMs, onProgress) {
    if (location.hostname !== "gemini.google.com") {
      throw new Error("FancyGPT Gemini adapter loaded on unexpected host");
    }
    const composer = await waitFor(
      () => firstVisible(SELECTORS.composer), 20000, "Gemini composer unavailable; sign in first",
    );
    const baselineCount = responseNodes().length;

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

    const tracker = createSettleTracker();
    const deadline = Date.now() + timeoutMs;
    let lastReported = null;
    while (Date.now() < deadline) {
      const text = latestResponseText(baselineCount);
      if (text && text !== lastReported) {
        lastReported = text;
        try { onProgress?.(text); } catch (_) {}
      }
      const streaming = Boolean(firstVisible(SELECTORS.stop));
      if (tracker.observe(text, {streaming})) {
        return {
          text,
          responseIdentity: `gemini-response-${baselineCount + 1}`,
          conversationId: currentConversationId(),
        };
      }
      await new Promise(resolve => setTimeout(resolve, 500));
    }
    throw new Error("Gemini response timed out");
  }

  globalThis.FancyGPTSites = globalThis.FancyGPTSites ?? {};
  globalThis.FancyGPTSites.gemini = {
    id: "gemini",
    freshUrl: "https://gemini.google.com/app",
    healthCheck,
    executeTurn,
  };
})();
