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

  /* The complete JSON value starting at `start`, or null if it is unfinished.
   *
   * Structure is counted, not brace characters: a brace inside a JSON string is
   * data. Counting raw characters rejected a valid envelope such as
   * {"text":"return {\"ok\":true}"} forever, which reads as a timeout. */
  function completeJsonAt(segment, start) {
    let depth = 0;
    let inString = false;
    let escaped = false;
    for (let i = start; i < segment.length; ++i) {
      const char = segment[i];
      if (inString) {
        if (escaped) escaped = false;
        else if (char === "\\") escaped = true;
        else if (char === '"') inString = false;
        continue;
      }
      if (char === '"') { inString = true; continue; }
      if (char === "{" || char === "[") depth += 1;
      else if (char === "}" || char === "]") {
        depth -= 1;
        if (depth < 0) return null;
        if (depth === 0) {
          const json = segment.slice(start, i + 1);
          try { JSON.parse(json); } catch (_) { return null; }
          return json;
        }
      }
    }
    return null;
  }

  function looksLikeCompleteJson(text) {
    const segment = jsonSegment(text);
    // Every stage answers with JSON, so text without any is a partial capture,
    // not a finished non-JSON reply.
    if (segment == null) return false;
    // Prose may precede the envelope and may itself contain braces, so every
    // opening position is tried rather than committing to the first one. Fixing
    // on the first `{` let a sentence like "use {placeholders}" stall the turn
    // until its deadline.
    let json = null;
    for (let i = 0; i < segment.length && json === null; ++i) {
      const char = segment[i];
      if (char === "{" || char === "[") json = completeJsonAt(segment, i);
      else if (char === '"') {
        // Skip over a quoted run so its contents are not mistaken for a start.
        for (++i; i < segment.length; ++i) {
          if (segment[i] === "\\") ++i;
          else if (segment[i] === '"') break;
        }
      }
    }
    if (json === null) return false;
    // A reply that names verbatim blocks is only complete once they have arrived.
    const refs = json.match(/"(?:old_ref|new_ref|content_ref)"\s*:\s*"([^"]+)"/g) || [];
    return refs.every(ref => {
      const id = ref.match(/:\s*"([^"]+)"/)[1].replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      return new RegExp("^[ \\t]*fancygpt[:\\s]+" + id + "[ \\t]*$", "im").test(text);
    });
  }

  /* Read text the way the protocol needs it, from a tab that may be occluded.
   *
   * Chromium defers layout for a hidden or minimised tab, so innerText can stay
   * frozen at whatever prefix was last painted while the DOM has already
   * streamed on -- that is the lost-characters failure. textContent is live
   * because it never consults layout, but for the same reason it drops every
   * line break that existed only because two block elements render on separate
   * lines. The code-change contract cannot survive that: `fancygpt:<id>` block
   * labels are matched per line, and a fence's leading indentation is payload.
   *
   * So text is read live from text nodes and the line structure is reinstated
   * structurally, from the element boundaries, instead of being inherited from
   * a layout that may never have run.
   */
  const LINE_BREAKING_TAGS = new Set([
    "ADDRESS", "ARTICLE", "ASIDE", "BLOCKQUOTE", "BR", "DD", "DIV", "DL", "DT",
    "FIELDSET", "FIGCAPTION", "FIGURE", "FOOTER", "FORM", "H1", "H2", "H3",
    "H4", "H5", "H6", "HEADER", "HR", "LI", "MAIN", "NAV", "OL", "P", "PRE",
    "SECTION", "TABLE", "TD", "TH", "TR", "UL",
  ]);
  // Controls the site draws around a reply. Their labels ("Copy", "Edit") are
  // not model output and must never reach the parser.
  const NON_CONTENT_TAGS = new Set(["BUTTON", "SCRIPT", "STYLE", "NOSCRIPT", "SVG", "SELECT", "TEXTAREA"]);

  function readLiveText(root, options = {}) {
    if (!root) return "";
    const skip = options.skip instanceof Set ? options.skip : new Set(options.skip ?? []);

    // A subtree that renders as one line already has that line in its
    // textContent, so it is read whole. This is not an optimisation: a
    // highlighted code token is a <span> per word and a link or inline <code>
    // sits mid-sentence, so walking into them would scatter one line of prose
    // or source across a dozen.
    const isFlat = (node, inPre) => {
      for (const child of node.children ?? []) {
        const tag = String(child.tagName ?? "").toUpperCase();
        if (LINE_BREAKING_TAGS.has(tag) || NON_CONTENT_TAGS.has(tag)) return false;
        // Only a fence's <code> is verbatim; inline <code> stays in its line.
        if (inPre && tag === "CODE") return false;
        if (skip.has(child) || child.getAttribute?.("aria-hidden") === "true") return false;
        if (!isFlat(child, inPre)) return false;
      }
      return true;
    };

    const parts = [];
    const walk = (node, inPre) => {
      if (!node || skip.has(node)) return;
      if (node.nodeType === 3) {
        const text = (node.nodeValue ?? node.textContent ?? "").trim();
        if (text) parts.push(text);
        return;
      }
      const tag = String(node.tagName ?? "").toUpperCase();
      if (NON_CONTENT_TAGS.has(tag)) return;
      if (node.getAttribute?.("aria-hidden") === "true") return;
      // Parts are joined with a newline, so a <br> is already accounted for by
      // the split between the runs either side of it.
      if (tag === "BR") return;
      if (tag === "CODE" && inPre) {
        // The fence body: its newlines are real text nodes and its leading
        // whitespace is payload the code-change contract matches literally, so
        // it is taken exactly as it stands.
        const raw = node.textContent ?? node.innerText ?? "";
        const text = raw.replace(/^\n/, "").replace(/[ \t]*\n[ \t]*$/, "");
        if (text) parts.push(text);
        return;
      }
      // A site wraps its fence in <pre> together with a header carrying the
      // language label -- which is where a ```fancygpt:<id> tag ends up -- and a
      // Copy control, so <pre> is descended into rather than read whole.
      const nowInPre = inPre || tag === "PRE";
      if (isFlat(node, nowInPre)) {
        const raw = node.textContent ?? node.innerText ?? "";
        const text = nowInPre ? raw.replace(/^\n/, "").replace(/[ \t]*\n[ \t]*$/, "") : raw.trim();
        if (text) parts.push(text);
        return;
      }
      for (const child of node.childNodes ?? node.children ?? []) walk(child, nowInPre);
    };
    walk(root, false);
    // Only blank edges are removed. A plain trim would strip the leading
    // indentation of a reply that is nothing but one verbatim block.
    return parts.join("\n").replace(/^\n+/, "").replace(/\s+$/, "");
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
      get remainingMs() {
        return candidateText === null ? null : Math.max(0, stabilityMs - (now() - candidateSince));
      },
      get candidateAgeMs() { return candidateText === null ? null : Math.max(0, now() - candidateSince); },
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
    let generation = 0;
    let consumedGeneration = 0;
    let notifications = 0;
    let immediateWakes = 0;
    let awaitedWakes = 0;
    let timeouts = 0;
    const notify = () => {
      generation += 1;
      notifications += 1;
      const resolve = wake;
      if (resolve) resolve("activity");
    };
    const observer = new MutationObserver(notify);
    observer.observe(target, {childList: true, subtree: true, characterData: true});
    return {
      notify,
      wait(maxMs) {
        // Activity is level-triggered, not a disposable edge. A mutation or
        // background tick that arrives between two waits remains observable by
        // the next wait instead of being lost in a wake=null blind spot.
        if (generation !== consumedGeneration) {
          consumedGeneration = generation;
          immediateWakes += 1;
          return Promise.resolve("activity");
        }
        return new Promise(resolve => {
          let done = false;
          const settle = reason => {
            if (done) return;
            done = true;
            wake = null;
            consumedGeneration = generation;
            if (reason === "activity") awaitedWakes += 1;
            else timeouts += 1;
            resolve(reason);
          };
          wake = settle;
          setTimeout(() => settle("timeout"), maxMs);
        });
      },
      stats() { return {notifications, immediateWakes, awaitedWakes, timeouts, generation}; },
      stop() { try { observer.disconnect(); } catch (_) {} wake = null; },
    };
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
    readLiveText,
    looksLikeCompleteJson,
    createSettleTracker,
    stopGeneration,
    observeText,
    createCompletionGate,
    createActivityWaiter,
  };
})();
