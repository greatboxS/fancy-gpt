/* ChatGPT site adapter. Owns every ChatGPT DOM assumption. */
(() => {
  const SELECTORS = {
    composer: ["#prompt-textarea", "textarea", '[contenteditable="true"]'],
    send: ['button[data-testid="send-button"]', 'button[aria-label*="Send"]'],
    stop: ['button[data-testid="stop-button"]', 'button[aria-label*="Stop"]'],
    turns: "[data-turn-id]",
    assistant: ['[data-message-author-role="assistant"]', '[data-testid="conversation-turn-assistant"]']
  };

  function firstVisible(selectors, root = document) {
    for (const selector of selectors) {
      const values = [...root.querySelectorAll(selector)].filter(el => {
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
      });
      if (values.length === 1) return values[0];
    }
    return null;
  }

  // The send control must be the composer's own. Searching the whole document
  // also finds unrelated buttons whose label merely contains "Send", and the
  // uniqueness rule then rejects every candidate -- reporting "not found" for
  // what is really "found too many".
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

  async function waitFor(getter, timeoutMs, message) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const value = getter();
      if (value) return value;
      await new Promise(resolve => setTimeout(resolve, 200));
    }
    throw new Error(message);
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

  function setComposer(composer, prompt) {
    composer.focus();
    if (composer.tagName.toLowerCase() === "textarea") {
      const descriptor = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value");
      descriptor?.set?.call(composer, prompt);
      composer.dispatchEvent(new Event("input", {bubbles: true}));
      composer.dispatchEvent(new Event("change", {bubbles: true}));
      return;
    }
    composer.textContent = prompt;
    composer.dispatchEvent(new InputEvent("input", {bubbles: true, inputType: "insertText", data: prompt}));
  }

  function findButtonByText(pattern, root = document) {
    const buttons = [...root.querySelectorAll("button")];
    return buttons.find(btn => {
      const rect = btn.getBoundingClientRect();
      const style = getComputedStyle(btn);
      if (!(rect.width > 0 && rect.height > 0) || style.visibility === "hidden" || style.display === "none") return false;
      return pattern.test((btn.innerText || "").trim());
    }) ?? null;
  }

  function currentConversationId() {
    const match = location.pathname.match(/^\/c\/([a-zA-Z0-9-]+)/);
    return match ? match[1] : null;
  }

  const BLOCK_LABEL = /^[ \t]*fancygpt[:\s]+([A-Za-z0-9._-]+)[ \t]*$/i;

  // The JSON document ends where the first verbatim code block begins. Braces
  // inside those blocks belong to source code and must not be counted.
  function jsonSegment(text) {
    const lines = text.split("\n");
    let stop = lines.length;
    for (let i = 0; i < lines.length; ++i) {
      if (BLOCK_LABEL.test(lines[i])) { stop = i; break; }
    }
    const head = lines.slice(0, stop).join("\n");
    const start = head.search(/[{[]/);
    return start === -1 ? null : head.slice(start);
  }

  function looksLikeCompleteJson(text) {
    const segment = jsonSegment(text);
    // Every stage answers with JSON, so text without any is a partial capture,
    // not a finished non-JSON reply.
    if (segment == null) return false;
    const opens = (segment.match(/[{[]/g) || []).length;
    const closes = (segment.match(/[}\]]/g) || []).length;
    if (opens !== closes) return false;
    // A reply that names verbatim blocks is only complete once they have arrived.
    const refs = segment.match(/"(?:old_ref|new_ref|content_ref)"\s*:\s*"([^"]+)"/g) || [];
    return refs.every(ref => {
      const id = ref.match(/:\s*"([^"]+)"/)[1].replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      return new RegExp("^[ \\t]*fancygpt[:\\s]+" + id + "[ \\t]*$", "im").test(text);
    });
  }

  // Stamped at export time. Reporting it back is the only way to tell a reloaded
  // extension from one that merely looks reloaded, which matters most when the
  // browser lives on a different machine than the runtime.
  const ADAPTER_BUILD = "__FANCYGPT_ADAPTER_BUILD__";

  async function healthCheck() {
    if (location.hostname !== "chatgpt.com") return {ok: false, reason: "unexpected-host", build: ADAPTER_BUILD};
    const composer = firstVisible(SELECTORS.composer);
    return {
      ok: Boolean(composer),
      reason: composer ? "ready" : "composer-unavailable",
      build: ADAPTER_BUILD,
    };
  }

  async function executeTurn(prompt, timeoutMs, onProgress) {
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
        const text = assistantText(boundId);
        if (text && text !== lastReported) {
          lastReported = text;
          try { onProgress?.(text); } catch (_) {}
        }
        const streaming = Boolean(firstVisible(SELECTORS.stop));
        const complete = text != null && looksLikeCompleteJson(text);
        if (text && text === stableText && !streaming && complete) stableCount += 1;
        else stableCount = 0;
        stableText = text;
        if (text && stableCount >= 3) return {text, responseIdentity: boundId, conversationId: currentConversationId()};
      }
      await new Promise(resolve => setTimeout(resolve, 500));
    }
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
