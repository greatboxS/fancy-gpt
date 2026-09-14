/* DOM adapters for MCP-only sites: Grok, Kimi and GLM/Z.ai. */
(() => {
  const kit = globalThis.FancyGPTSiteKit;
  const {firstVisible, waitFor, setComposer, stopGeneration, observeText,
         createCompletionGate, createActivityWaiter} = kit;

  const configs = {
    grok: {
      hosts: ["grok.com", "x.com"], freshUrl: "https://grok.com/",
      composer: ['textarea[placeholder]', 'textarea', 'div[contenteditable="true"]'],
      send: ['button[type="submit"]', 'button[aria-label*="Send" i]'],
      stop: ['button[aria-label*="Stop" i]'],
      responses: ['div[data-testid="message-bubble"]', 'div[class*="message"] .prose', 'article .prose'],
    },
    kimi: {
      hosts: ["www.kimi.com", "kimi.com", "www.kimi.ai", "kimi.ai"], freshUrl: "https://www.kimi.com/",
      composer: ['div[contenteditable="true"]', 'textarea'],
      send: ['button[type="submit"]', 'button[aria-label*="Send" i]', 'button[class*="send"]'],
      stop: ['button[aria-label*="Stop" i]', 'button[class*="stop"]'],
      responses: ['div[class*="assistant"] div[class*="markdown"]', 'div[class*="segment-content"]', 'div[class*="markdown"]'],
    },
    glm: {
      hosts: ["chat.z.ai", "z.ai"], freshUrl: "https://chat.z.ai/",
      composer: ['textarea', 'div[contenteditable="true"]'],
      send: ['button[type="submit"]', 'button[aria-label*="Send" i]', 'button[class*="send"]'],
      stop: ['button[aria-label*="Stop" i]', 'button[class*="stop"]'],
      responses: ['div[class*="assistant"] div[class*="markdown"]', 'div[class*="message-content"]', 'div[class*="markdown"]'],
    },
  };

  const visibleNodes = selectors => {
    for (const selector of selectors) {
      const nodes = [...document.querySelectorAll(selector)].filter(node => node.getClientRects().length);
      if (nodes.length) return nodes;
    }
    return [];
  };
  const textOf = node => (node?.innerText ?? node?.textContent ?? "").trim();

  function createAdapter(id, config) {
    const hostOk = () => config.hosts.includes(location.hostname);
    const responseNodes = () => visibleNodes(config.responses);
    const isGenerating = () => Boolean(firstVisible(config.stop) || document.querySelector('[aria-busy="true"]'));
    const conversationId = () => location.pathname.split('/').filter(Boolean).at(-1) || null;

    async function healthCheck() {
      const composer = hostOk() && firstVisible(config.composer);
      return {ok: Boolean(composer), reason: composer ? "ready" : "composer-unavailable", build: kit.build};
    }

    async function executeTurn(prompt, timeoutMs, onProgress, options = {}) {
      if (!hostOk()) throw new Error(`FancyGPT ${id} adapter loaded on unexpected host`);
      const composer = await waitFor(() => firstVisible(config.composer), 20000, `${id} composer unavailable; sign in first`);
      const baseline = responseNodes().length;
      setComposer(composer, prompt);
      const send = await waitFor(() => firstVisible(config.send), 10000, `${id} send control unavailable`);
      send.click();
      options?.onSubmitted?.();

      const gate = createCompletionGate({stabilityMs: 1500});
      const activity = createActivityWaiter();
      let lastText = null;
      let lastActivity = Date.now();
      if (options?.onTick) options.onTick(() => activity.notify());
      const latest = () => {
        const nodes = responseNodes();
        return nodes.length > baseline ? textOf(nodes[nodes.length - 1]) || null : null;
      };
      const stopObserving = onProgress ? observeText(latest, onProgress) : null;
      const release = () => { activity.stop(); if (stopObserving) stopObserving(); };
      const deadline = Date.now() + timeoutMs;
      while (Date.now() < deadline && Date.now() - lastActivity < 90000) {
        if (options?.isCancelled?.()) {
          const stopped = stopGeneration(config.stop);
          release();
          return {text: latest() ?? "", responseIdentity: `${id}-${baseline + 1}`,
                  conversationId: conversationId(), cancelled: true, stoppedGeneration: stopped};
        }
        const text = latest();
        const active = isGenerating();
        if (active || text !== lastText) { lastActivity = Date.now(); lastText = text; }
        if (gate.observe({text, active, complete: Boolean(text)})) {
          release();
          return {text, responseIdentity: `${id}-${baseline + 1}`, conversationId: conversationId()};
        }
        await activity.wait(gate.remainingMs == null ? 500 : Math.max(1, Math.min(500, gate.remainingMs)));
      }
      release();
      throw new Error(`${id} response stalled or exceeded its deadline`);
    }

    globalThis.FancyGPTSites = globalThis.FancyGPTSites ?? {};
    globalThis.FancyGPTSites[id] = {id, freshUrl: config.freshUrl, healthCheck, executeTurn};
  }

  for (const [id, config] of Object.entries(configs)) createAdapter(id, config);
})();
