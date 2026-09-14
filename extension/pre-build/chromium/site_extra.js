/* Shared DOM adapter engine for MCP-only sites. Site policy and selectors live
 * in site_<id>.js so one site's UI update stays isolated. */
(() => {
  const kit = globalThis.FancyGPTSiteKit;
  const {firstVisible, waitFor, setComposer, stopGeneration, observeText,
         createCompletionGate, createActivityWaiter} = kit;

  const visibleNodes = selectors => {
    // One site can change markup between old and new messages during a UI
    // rollout. Returning only the first selector that matched meant an old
    // message hid the new response forever (observed on Kimi). A combined CSS
    // query keeps every matching response in document order and deduplicates
    // nodes matched by more than one fallback.
    try {
      return [...document.querySelectorAll(selectors.join(","))]
        .filter(node => node.getClientRects().length);
    } catch (_) {
      return [];
    }
  };
  const textOf = node => (node?.innerText ?? node?.textContent ?? "").trim();
  const thinkingOnly = text => {
    const value = String(text ?? "").trim();
    return value.length <= 120 && /^(thinking|reasoning|analyzing|searching|思考中|正在思考)[.…。\s]*$/i.test(value);
  };

  const submitComposer = async (composer, selectors) => {
    // Give reactive UIs a moment to enable/render their send control after the
    // input event. Kimi uses a clickable div container, while Grok currently
    // exposes a normal button.
    let send = null;
    try { send = await waitFor(() => firstVisible(selectors), 3000, "send control unavailable"); } catch (_) {}
    if (send) { send.click(); return true; }
    const form = composer.closest?.("form");
    if (form?.requestSubmit) { form.requestSubmit(); return true; }
    composer.focus?.();
    for (const type of ["keydown", "keypress", "keyup"]) {
      composer.dispatchEvent(new KeyboardEvent(type, {
        key: "Enter", code: "Enter", keyCode: 13, which: 13,
        bubbles: true, cancelable: true, composed: true,
      }));
    }
    return true;
  };

  function createAdapter(id, config) {
    const hostOk = () => config.hosts.includes(location.hostname);
    const responseNodes = () => visibleNodes(config.responses).filter(node =>
      !(config.excludeResponses ?? []).some(selector => node.closest?.(selector))
    );
    const isGenerating = () => Boolean(firstVisible(config.stop) || document.querySelector('[aria-busy="true"]'));
    const conversationId = () => location.pathname.split('/').filter(Boolean).at(-1) || null;

    async function healthCheck() {
      // React/Lexical sites can reach document "interactive" well before the
      // chat composer hydrates. A synchronous probe made Grok fail in 1.6s
      // with zero inputs even though the page was still starting.
      let composer = null;
      if (hostOk()) {
        try { composer = await waitFor(() => firstVisible(config.composer), 12000, "composer unavailable"); }
        catch (_) {}
      }
      const genericInputs = [...document.querySelectorAll('textarea, [contenteditable="true"], [role="textbox"]')];
      const visibleGenericInputs = genericInputs.filter(node => Boolean(node.getClientRects?.().length));
      const loginControls = document.querySelectorAll(
        'a[href*="login" i], a[href*="sign-in" i], button[data-testid*="login" i], button[data-testid*="sign-in" i]'
      ).length;
      return {
        ok: Boolean(composer), reason: composer ? "ready" : "composer-unavailable", build: kit.build,
        probe: {
          path: location.pathname, readyState: document.readyState,
          genericInputs: genericInputs.length, visibleGenericInputs: visibleGenericInputs.length,
          loginControls, titleSuggestsAuth: /log\s*in|sign\s*in/i.test(document.title),
        },
      };
    }

    async function executeTurn(prompt, timeoutMs, onProgress, options = {}) {
      if (!hostOk()) throw new Error(`FancyGPT ${id} adapter loaded on unexpected host`);
      const composer = await waitFor(() => firstVisible(config.composer), 20000, `${id} composer unavailable; sign in first`);
      if (config.prepareConversation) {
        await config.prepareConversation({options, waitFor, firstVisible});
      }
      if (options?.continuing) {
        // On a resumed conversation the composer hydrates before the old
        // messages. Taking the baseline immediately made the previous answer
        // appear "new" and return its old request_id. Wait until history has
        // stopped changing before submitting the continuation.
        let signature = null;
        let stableSince = Date.now();
        await waitFor(() => {
          const nodes = responseNodes();
          const next = `${nodes.length}:${nodes.map(node => textOf(node).length).join(",")}`;
          if (next !== signature) { signature = next; stableSince = Date.now(); }
          return Date.now() - stableSince >= 1200;
        }, 10000, `${id} conversation history did not settle`);
      }
      const baselineNodes = new Map(responseNodes().map(node => [node, textOf(node)]));
      const baseline = baselineNodes.size;
      const expectedRequestId = String(prompt).match(/"request_id"\s*:\s*"([A-Za-z0-9_-]+)"/)?.[1] ?? null;
      setComposer(composer, prompt);
      await submitComposer(composer, config.send);
      options?.onSubmitted?.();

      const gate = createCompletionGate({stabilityMs: 1500});
      const activity = createActivityWaiter();
      let lastText = null;
      let lastActivity = Date.now();
      let lastNetworkActivityAt = null;
      if (options?.onTick) options.onTick(() => activity.notify());
      const latest = () => {
        const nodes = responseNodes();
        // Structured FancyGPT prompts carry a unique request id. Prefer it
        // over DOM identity because Grok virtualizes/reuses assistant nodes in
        // resumed threads. This cannot bind to an earlier response.
        if (expectedRequestId) {
          const exact = nodes.filter(node => textOf(node).includes(expectedRequestId));
          if (exact.length) {
            const raw = textOf(exact[exact.length - 1]);
            const text = config.cleanResponse ? config.cleanResponse(raw) : raw;
            return text && !thinkingOnly(text) ? text : null;
          }
        }
        const candidates = nodes.filter(node =>
          !baselineNodes.has(node) || textOf(node) !== baselineNodes.get(node)
        );
        if (!candidates.length) return null;
        const raw = textOf(candidates[candidates.length - 1]);
        const text = config.cleanResponse ? config.cleanResponse(raw) : raw;
        return text && !thinkingOnly(text) ? text : null;
      };
      const stopObserving = onProgress ? observeText(latest, onProgress) : null;
      const release = () => { activity.stop(); if (stopObserving) stopObserving(); };
      const deadline = Date.now() + timeoutMs;
      while (Date.now() < deadline && Date.now() - lastActivity < 90000) {
        const networkAt = options?.networkActivityAt?.();
        if (networkAt != null && networkAt !== lastNetworkActivityAt) {
          lastNetworkActivityAt = networkAt;
          lastActivity = Math.max(lastActivity, networkAt);
        }
        if (options?.isCancelled?.()) {
          const stopped = stopGeneration(config.stop);
          release();
          return {text: latest() ?? "", responseIdentity: `${id}-${baseline + 1}`,
                  conversationId: options?.temporary ? null : conversationId(),
                  cancelled: true, stoppedGeneration: stopped};
        }
        const text = latest();
        // Some sites replace or rename their Stop control frequently. Bytes
        // arriving from the model are authoritative evidence that generation
        // is still active and prevent the completion gate from returning a
        // stable-looking prefix.
        const active = isGenerating()
          || (lastNetworkActivityAt != null && Date.now() - lastNetworkActivityAt < 1200);
        if (active || text !== lastText) { lastActivity = Date.now(); lastText = text; }
        if (gate.observe({text, active, complete: Boolean(text)})) {
          release();
          return {text, responseIdentity: `${id}-${baseline + 1}`,
                  conversationId: options?.temporary ? null : conversationId()};
        }
        await activity.wait(gate.remainingMs == null ? 500 : Math.max(1, Math.min(500, gate.remainingMs)));
      }
      release();
      throw new Error(`${id} response stalled or exceeded its deadline`);
    }

    globalThis.FancyGPTSites = globalThis.FancyGPTSites ?? {};
    globalThis.FancyGPTSites[id] = {id, freshUrl: config.freshUrl, healthCheck, executeTurn};
  }

  globalThis.FancyGPTCreateGenericSite = createAdapter;
})();
