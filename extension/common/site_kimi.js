/* FancyGPTSites.kimi policy. Shared mechanics live in site_extra.js. */
globalThis.FancyGPTCreateGenericSite("kimi", {
  hosts: ["www.kimi.com", "kimi.com", "www.kimi.ai", "kimi.ai"], freshUrl: "https://www.kimi.com/",
  composer: [
    '#chat-box .chat-input-editor[contenteditable="true"]', '#chat-box .chat-input-editor',
    '[role="textbox"][contenteditable="true"]', 'div[contenteditable="true"]', 'textarea',
  ],
  send: [
    '#chat-box .send-button-container', '#chat-box .send-button', '.chat-editor-action .send-button-container',
    'button[type="submit"]', 'button[aria-label*="Send" i]', 'button[class*="send" i]', '[class*="send-button" i]',
  ],
  stop: ['button[aria-label*="Stop" i]', 'button[class*="stop"]'],
  responses: ['div[class*="assistant"] div[class*="markdown"]', 'div[class*="segment-content"]', 'div[class*="markdown"]'],
});
