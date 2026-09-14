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
  // The current Z.ai DOM sometimes places this UI heading in the same text
  // node as the final JSON instead of wrapping it in the reasoning element.
  cleanResponse: text => String(text ?? "").replace(
    /^(?:Thought Process|Thinking|Reasoning)\s*(?=\{|\[)/i,
    "",
  ).trim(),
});
