/* FancyGPTSites.grok policy. Shared mechanics live in site_extra.js. */
globalThis.FancyGPTCreateGenericSite("grok", {
  hosts: ["grok.com", "x.com"], freshUrl: "https://grok.com/",
  // Temporary means Grok Private Chat, not merely "do not bind locally".
  // Fail closed if the private route did not stick, so a transient SPA change
  // cannot accidentally leave a supposedly temporary request in chat history.
  prepareConversation: async ({options, waitFor}) => {
    if (!options?.temporary) return;
    const notice = () => /This chat won.t appear in your history and will not be used to train models/i
      .test(document.body?.innerText ?? "");
    if (notice()) return;
    const switcher = await waitFor(
      () => document.querySelector('a[aria-label="Switch to Private Chat"]'),
      10000,
      "Grok Private Chat control is unavailable; refusing to send a persistent turn",
    );
    switcher.click();
    await waitFor(() => notice() || Boolean(document.querySelector(
      'a[aria-label="Switch to Normal Chat"], a[aria-label="Exit Private Chat"], a[aria-label="Leave Private Chat"]'
    )), 10000, "Grok Private Chat was not activated; refusing to send a persistent turn");
  },
  composer: [
    'div.ProseMirror[role="textbox"]', '[data-testid="chat-input"] [contenteditable="true"]',
    'textarea[data-testid="grok-compose-input"]', 'textarea[placeholder*="Grok" i]',
    'textarea[placeholder*="Ask" i]', 'div[contenteditable="true"][data-lexical-editor="true"]',
    '[role="textbox"][contenteditable="true"]', 'textarea[spellcheck="false"]',
    'textarea', 'div[contenteditable="true"]',
  ],
  send: [
    'button[aria-label="Send message"]', 'button[data-testid="send-button"]',
    'button[data-testid*="send" i]', 'button[data-testid*="submit" i]',
    'button[type="submit"]', 'button[aria-label*="Send" i]',
  ],
  stop: ['button[aria-label*="Stop" i]'],
  responses: [
    '[data-testid="assistant-message"]', 'div[data-testid="message-bubble"]',
    'div[class*="message"] .prose', 'article .prose',
  ],
  // Grok renders its elapsed-thinking badge into the assistant node's
  // innerText. It is UI chrome, not part of the model's JSON response.
  cleanResponse: text => String(text ?? "").replace(
    /^(?:(?:Worked|Thought)\s+for\s+\d+(?:\.\d+)?s\s*)?(?:JSON\s*)?(?=\{|\[)/i,
    "",
  ).trim(),
});
