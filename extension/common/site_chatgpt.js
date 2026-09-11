/* ChatGPT site adapter. Owns every ChatGPT DOM assumption. */
(() => {
  const SELECTORS = {
    composer: ["#prompt-textarea", "textarea", '[contenteditable="true"]'],
    send: ['button[data-testid="send-button"]', 'button[aria-label*="Send"]'],
    stop: ['button[data-testid="stop-button"]', 'button[aria-label*="Stop"]'],
    turns: "[data-turn-id]",
    assistant: ['[data-message-author-role="assistant"]', '[data-testid="conversation-turn-assistant"]']
  };

  function firstVisible(selectors) {
    for (const selector of selectors) {
      const values = [...document.querySelectorAll(selector)].filter(el => {
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
      });
      if (values.length === 1) return values[0];
    }
    return null;
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

  function findButtonByText(pattern) {
    const buttons = [...document.querySelectorAll("button")];
    return buttons.find(btn => {
      const rect = btn.getBoundingClientRect();
      const style = getComputedStyle(btn);
      if (!(rect.width > 0 && rect.height > 0) || style.visibility === "hidden" || style.display === "none") return false;
      return pattern.test((btn.innerText || "").trim());
    }) ?? null;
  }

  function looksLikeCompleteJson(text) {
    const trimmed = text.trim();
    if (!trimmed) return false;
    const first = trimmed[0];
    if (first !== "{" && first !== "[") return true;
    const opens = (trimmed.match(/[{[]/g) || []).length;
    const closes = (trimmed.match(/[}\]]/g) || []).length;
    return opens === closes;
  }

  async function healthCheck() {
    if (location.hostname !== "chatgpt.com") return {ok: false, reason: "unexpected-host"};
    const composer = firstVisible(SELECTORS.composer);
    return {ok: Boolean(composer), reason: composer ? "ready" : "composer-unavailable"};
  }

  async function executeTurn(prompt, timeoutMs, onProgress) {
    if (location.hostname !== "chatgpt.com") throw new Error("FancyGPT ChatGPT adapter loaded on unexpected host");
    const composer = await waitFor(() => firstVisible(SELECTORS.composer), 20000, "ChatGPT composer unavailable; sign in first");
    const baseline = new Set(turnIds());
    setComposer(composer, prompt);
    const send = await waitFor(() => firstVisible(SELECTORS.send), 10000, "unique ChatGPT send button not found");
    send.click();

    const deadline = Date.now() + timeoutMs;
    let boundId = null;
    let stableText = null;
    let stableCount = 0;
    let lastReported = null;
    while (Date.now() < deadline) {
      const continueButton = findButtonByText(/continue generating/i);
      if (continueButton) {
        continueButton.click();
        stableCount = 0;
        await new Promise(resolve => setTimeout(resolve, 500));
        continue;
      }
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
        if (text && stableCount >= 3) return {text, responseIdentity: boundId};
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
