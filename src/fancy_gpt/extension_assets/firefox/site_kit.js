/* Shared site-adapter primitives. Owns no selectors and no site URLs.
 *
 * Every site adapter needs the same handful of browser-facing behaviours, and
 * each one of them was learned the hard way against ChatGPT. Keeping them here
 * means a second adapter starts from what already works instead of rediscovering
 * that a rich-text editor ignores a direct DOM write.
 */
(() => {
  function visible(element) {
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  }

  // Deliberately strict: a selector that matches several elements is ambiguous,
  // and binding to the wrong one silently sends a prompt nowhere.
  function firstVisible(selectors, root = document) {
    for (const selector of selectors) {
      const values = [...root.querySelectorAll(selector)].filter(visible);
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

  function findButtonByText(pattern, root = document) {
    const buttons = [...root.querySelectorAll("button")].filter(visible);
    const matches = buttons.filter(button => pattern.test(button.textContent ?? ""));
    return matches.length === 1 ? matches[0] : null;
  }

  function selectAll(element) {
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(element);
    selection?.removeAllRanges();
    selection?.addRange(range);
  }

  /* Put text into a composer the way a keystroke does.
   *
   * A modern chat composer is a rich-text editor that owns its own document
   * model. Assigning textContent mutates the DOM behind its back: the editor
   * either reverts the change or never learns the field is non-empty, and a send
   * control that only exists for a non-empty composer never appears. insertText
   * goes through the same input path a keystroke takes, so the editor updates
   * itself. Selecting first replaces existing text rather than appending on a retry.
   */
  function setComposer(composer, prompt) {
    composer.focus();
    if (composer.tagName.toLowerCase() === "textarea") {
      const descriptor = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value");
      descriptor?.set?.call(composer, prompt);
      composer.dispatchEvent(new Event("input", {bubbles: true}));
      composer.dispatchEvent(new Event("change", {bubbles: true}));
      return;
    }
    selectAll(composer);
    const inserted = document.execCommand("insertText", false, prompt);
    if (inserted && (composer.textContent ?? "").includes(prompt.slice(0, 32))) return;
    composer.textContent = prompt;
    composer.dispatchEvent(new InputEvent("input", {bubbles: true, inputType: "insertText", data: prompt}));
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

  /* Decide when a streamed reply has settled.
   *
   * A site tells us it is streaming through its own stop control, so that signal
   * is passed in rather than detected here. Requiring the text to be unchanged
   * across several polls as well covers the gap between the control disappearing
   * and the last token landing.
   */
  function createSettleTracker(requiredStablePolls = 3) {
    let lastText = null;
    let stable = 0;
    return {
      observe(text, {streaming}) {
        const structured = text != null && looksLikeCompleteJson(text);
        if (text && text === lastText && !streaming) stable += 1;
        else stable = 0;
        lastText = text;
        // Return stable non-JSON output too, so the protocol parser can reject it
        // promptly instead of turning a model contract violation into a timeout.
        const threshold = structured ? requiredStablePolls : Math.max(4, requiredStablePolls + 2);
        return Boolean(text) && stable >= threshold;
      },
    };
  }

  /* Click a site's own stop control to end generation.
   *
   * Returns false when no stop control is visible, which means generation had
   * already finished. The caller must treat that as "already terminal" rather
   * than as a failed cancel.
   */
  function stopGeneration(stopSelectors) {
    const control = firstVisible(stopSelectors);
    if (!control) return false;
    try { control.click(); return true; } catch (_) { return false; }
  }

  // Stamped at export time; every adapter reports this one value.
  const BUILD = "__FANCYGPT_ADAPTER_BUILD__";

  globalThis.FancyGPTSiteKit = {
    build: BUILD,
    visible,
    firstVisible,
    waitFor,
    findButtonByText,
    selectAll,
    setComposer,
    jsonSegment,
    looksLikeCompleteJson,
    createSettleTracker,
    stopGeneration,
  };
})();
