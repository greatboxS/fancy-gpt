/* FancyGPTSites.glm policy. Shared mechanics live in site_extra.js. */
globalThis.FancyGPTCreateGenericSite("glm", {
  hosts: ["chat.z.ai", "z.ai"], freshUrl: "https://chat.z.ai/",
  composer: ['textarea', '[role="textbox"][contenteditable="true"]', 'div[contenteditable="true"]'],
  send: ['button[type="submit"]', 'button[aria-label*="Send" i]', 'button[class*="send" i]'],
  stop: ['button[aria-label*="Stop" i]', 'button[class*="stop" i]'],
  responses: ['div[class*="assistant"] div[class*="markdown"]', 'div[class*="message-content"]', 'div[class*="markdown"]'],
  excludeResponses: [
    '[class*="thinking" i]', '[class*="reasoning" i]',
    '[data-testid*="thinking" i]', '[data-testid*="reasoning" i]',
  ],
});
