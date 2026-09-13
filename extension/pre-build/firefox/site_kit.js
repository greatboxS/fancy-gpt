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

  /* Report text changes as the DOM makes them, not on a polling timer.
   *
   * Automation runs in a minimized window so it stays out of the user's way,
   * and browsers throttle setTimeout hard in hidden windows - observed as
   * 24-second gaps where the page had actually been streaming all along. A
   * MutationObserver is driven by the DOM changing rather than by a timer, so
   * partial output keeps flowing while the window is hidden.
   *
   * Returns a stop function. Never let a reporting error break the turn.
   */
  function observeText(getText, onChange, options = {}) {
    const target = options.target ?? document.body;
    // A streaming page mutates hundreds of times a second, and reading the
    // reply forces layout. Re-reading on every mutation saturates the main
    // thread and starves the very loop that detects completion - which hung
    // real turns until they timed out. Reads are therefore rate limited, and
    // skipping a mutation is safe because each read returns the whole reply so
    // far, not a delta.
    const baseIntervalMs = options.minIntervalMs ?? 150;
    // If reading turns out to be expensive on this page, back off rather than
    // keep paying that cost: correctness of the turn matters more than the
    // granularity of progress.
    const maxIntervalMs = options.maxIntervalMs ?? 2000;
    const slowReadMs = options.slowReadMs ?? 50;
    let interval = baseIntervalMs;
    let last = null;
    let lastReadAt = 0;
    const report = force => {
      const now = Date.now();
      if (!force && now - lastReadAt < interval) return;
      lastReadAt = now;
      let text = null;
      const started = Date.now();
      try { text = getText(); } catch (_) { return; }
      const cost = Date.now() - started;
      interval = cost > slowReadMs
        ? Math.min(maxIntervalMs, Math.max(interval * 2, cost * 4))
        : baseIntervalMs;
      if (text && text !== last) {
        last = text;
        try { onChange(text); } catch (_) {}
      }
    };
    const observer = new MutationObserver(() => report(false));
    observer.observe(target, {childList: true, subtree: true, characterData: true});
    report(true);
    return () => { try { observer.disconnect(); } catch (_) {} };
  }

/* Decide that a reply has finished, without trusting any single signal.
   *
   * Both independent reviews of this agreed on the same two traps, and on the
   * shape of the answer:
   *
   *  - The stop control disappearing is NEGATIVE evidence only. It also
   *    disappears between a tool or search phase and the text that follows, so
   *    on its own it reports a half-finished reply as complete.
   *  - Text parsing as complete JSON is VALIDATION, not a completion signal. A
   *    streaming prefix can momentarily parse and then receive more bytes.
   *
   * So a moment where both look finished only opens a *candidate*, and any
   * further activity cancels it. The candidate becomes a result only after the
   * page has stayed quiet for a stability window.
   *
   * `observe` must be called on DOM mutations and on a clock, because a reply
   * that has genuinely finished produces no further mutations to trigger on.
   */
  function createCompletionGate(options = {}) {
    const stabilityMs = options.stabilityMs ?? 1200;
    const now = options.now ?? (() => Date.now());
    let candidateText = null;
    let candidateSince = 0;
    return {
      observe({text, active, complete}) {
        if (active || !text || !complete) {
          candidateText = null;
          return null;
        }
        if (text !== candidateText) {
          // New text restarts the window: this is what stops a momentarily
          // valid JSON prefix from resolving.
          candidateText = text;
          candidateSince = now();
          return null;
        }
        return now() - candidateSince >= stabilityMs ? {text} : null;
      },
      get pending() { return candidateText !== null; },
    };
  }

/* Wait for the page to do something, rather than for a fixed delay.
   *
   * The automation window is minimized and browsers throttle setTimeout there
   * severely - a reply that arrived in 7 seconds took 69 to be noticed. A
   * mutation, or a tick pushed in from the extension's background worker, wakes
   * the waiter immediately; the timeout is only a floor for pages that go
   * completely quiet.
   */
  function createActivityWaiter(target = document.body) {
    let wake = null;
    const notify = () => { const resolve = wake; wake = null; if (resolve) resolve("activity"); };
    const observer = new MutationObserver(notify);
    observer.observe(target, {childList: true, subtree: true, characterData: true});
    return {
      notify,
      wait(maxMs) {
        return new Promise(resolve => {
          let done = false;
          const settle = reason => { if (done) return; done = true; wake = null; resolve(reason); };
          wake = settle;
          setTimeout(() => settle("timeout"), maxMs);
        });
      },
      stop() { try { observer.disconnect(); } catch (_) {} wake = null; },
    };
  }

  // Stamped at export time; every adapter reports this one value.
  const BUILD = "62ca44787dc9";

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
    observeText,
    createCompletionGate,
    createActivityWaiter,
  };
})();
