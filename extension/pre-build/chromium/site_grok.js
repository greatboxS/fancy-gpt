/* FancyGPTSites.grok policy. Shared mechanics live in site_extra.js. */
globalThis.FancyGPTCreateGenericSite("grok", {
  hosts: ["grok.com", "x.com"], freshUrl: "https://grok.com/",
  composer: [
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
  responses: ['div[data-testid="message-bubble"]', 'div[class*="message"] .prose', 'article .prose'],
});
