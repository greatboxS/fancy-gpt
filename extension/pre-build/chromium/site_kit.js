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

  // The fence is restored when a rendered code block is read, so the label may
  // arrive as the info string of a fence or, from an older reply, as a bare line.
  const BLOCK_LABEL = /^[ \t]*(?:```)?[ \t]*fancygpt[:\s]+([A-Za-z0-9._-]+)[ \t]*$/i;

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

  /* Scan one JSON value starting at `start`.
   *
   * Three outcomes, and keeping them apart is the whole point:
   *   {json}      a complete, parseable value
   *   {skipTo}    a structure that closed but is not JSON, so it was prose
   *               that merely contained a brace; resume scanning after it
   *   null        a structure that never closed, so the reply is still
   *               arriving and nothing after it can be trusted
   *
   * Collapsing the last two is how a truncated reply was accepted as finished:
   * the outer object had not closed, the scan walked on into it, found the
   * complete `[]` of an inner field, and called the turn done -- silently
   * handing back a JSON document cut off mid-key.
   */
  function scanJsonAt(segment, start) {
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
        // A closer with nothing open is punctuation in prose, not structure.
        if (depth < 0) return {skipTo: i + 1};
        if (depth === 0) {
          const json = segment.slice(start, i + 1);
          try { JSON.parse(json); } catch (_) { return {skipTo: i + 1}; }
          return {json};
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
    // Prose may precede the envelope and may itself contain braces, so a
    // structure that turns out not to be JSON is stepped over rather than
    // ending the search. One that never closes ends it immediately.
    let json = null;
    let i = 0;
    while (i < segment.length && json === null) {
      const char = segment[i];
      if (char === '"') {
        // Step over a quoted run so its contents are not read as structure.
        for (++i; i < segment.length; ++i) {
          if (segment[i] === "\\") ++i;
          else if (segment[i] === '"') break;
        }
        i += 1;
        continue;
      }
      if (char !== "{" && char !== "[") { i += 1; continue; }
      const found = scanJsonAt(segment, i);
      if (found === null) return false;
      if (found.json != null) { json = found.json; break; }
      i = found.skipTo;
    }
    if (json === null) return false;
    // A reply that names verbatim blocks is only complete once they have arrived.
    const refs = json.match(/"(?:old_ref|new_ref|content_ref)"\s*:\s*"([^"]+)"/g) || [];
    return refs.every(ref => {
      const id = ref.match(/:\s*"([^"]+)"/)[1].replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      return new RegExp("^[ \\t]*(?:```)?[ \\t]*fancygpt[:\\s]+" + id + "[ \\t]*$", "im").test(text);
    });
  }

  const LINE_BREAKING_TAGS = new Set([
    "ADDRESS", "ARTICLE", "ASIDE", "BLOCKQUOTE", "DD", "DIV", "DL", "DT",
    "FIELDSET", "FIGCAPTION", "FIGURE", "FOOTER", "FORM", "H1", "H2", "H3",
    "H4", "H5", "H6", "HEADER", "HR", "LI", "MAIN", "NAV", "OL", "P", "PRE",
    "SECTION", "TABLE", "TD", "TH", "TR", "UL",
  ]);
  // Elements the browser never renders as text. Everything else is kept,
  // including the site's own controls: a stray "Copy" on its own line is
  // harmless to every parser downstream, whereas guessing which subtree is
  // "chrome" once cost a whole reply -- 25 characters survived of 1155.
  const UNRENDERED_TAGS = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE"]);

  function readLiveText(root, options = {}) {
    if (!root) return "";
    const skip = options.skip instanceof Set ? options.skip : new Set(options.skip ?? []);
    // Chunks, not a growing string: trimming the tail of a 30KB accumulator on
    // every block boundary turns reading one long reply into quadratic work.
    // The first thing a code block's header says, which is where the fence's
    // info string ends up once markdown has rendered the fence away.
    const headerTextOf = (preNode, codeNode) => {
      let found = "";
      const visit = node => {
        if (found || !node || node === codeNode) return;
        if (node.nodeType === 3) {
          const text = (node.nodeValue ?? "").trim();
          if (text) found = text;
          return;
        }
        const tag = String(node.tagName ?? "").toUpperCase();
        if (UNRENDERED_TAGS.has(tag)) return;
        for (const child of node.childNodes ?? node.children ?? []) visit(child);
      };
      visit(preNode);
      return found;
    };

    const chunks = [];
    let lastChar = "";
    const push = text => { if (text) { chunks.push(text); lastChar = text[text.length - 1]; } };
    const trimTrailingSpaces = () => {
      while (chunks.length) {
        const trimmed = chunks[chunks.length - 1].replace(/[ \t]+$/, "");
        if (trimmed) { chunks[chunks.length - 1] = trimmed; lastChar = trimmed[trimmed.length - 1]; return; }
        chunks.pop();
        lastChar = chunks.length ? chunks[chunks.length - 1].slice(-1) : "";
      }
    };
    // Trailing spaces are dropped at a break so a line never ends in padding,
    // but newlines are never collapsed away: inside a fence they are payload.
    const breakLine = () => {
      trimTrailingSpaces();
      if (chunks.length && lastChar !== "\n") push("\n");
    };
    const walk = (node, inPre) => {
      if (!node || skip.has(node)) return;
      if (node.nodeType === 3) {
        const raw = node.nodeValue ?? node.textContent ?? "";
        if (inPre) { push(raw); return; }
        // Outside a fence, whitespace collapses exactly as CSS would render it,
        // so markup indentation never reaches the parser as content.
        const collapsed = raw.replace(/\s+/g, " ");
        // At the very start of the output too, or markup indentation before the
        // first element would become a leading space in the reply.
        push(!chunks.length || lastChar === "\n" ? collapsed.replace(/^ /, "") : collapsed);
        return;
      }
      const tag = String(node.tagName ?? "").toUpperCase();
      if (UNRENDERED_TAGS.has(tag)) return;
      if (tag === "BR") { trimTrailingSpaces(); push("\n"); return; }
      // A fence keeps its newlines and its leading indentation verbatim: the
      // code-change contract matches an OLD block character for character.
      // Its text is taken whole rather than walked -- syntax highlighting splits
      // one line into a span per token, and there is no block boundary inside
      // code worth reconstructing.
      if (tag === "CODE") { push(node.textContent ?? ""); return; }
      /* Put the ``` back around a rendered code block.
       *
       * The model writes fences; markdown renders them away, leaving the info
       * string as a bare line above the code and no delimiter at all below it.
       * Read back like that, a `fancygpt:<id>` block has no end: it runs to the
       * next label or off the end of the reply, swallowing whatever the model
       * said afterwards -- and that text is then written into the repository as
       * if it were source. A closing sentence after the last block corrupts the
       * file. The same missing fence makes a reply the model chose to wrap in
       * ```json arrive as `JSON` followed by the document, which parses as
       * neither.
       *
       * The boundary is not ambiguous in the DOM, only in the text, so it is
       * restored here where it is still known.
       */
      if (tag === "PRE") {
        const code = node.querySelector?.("code");
        if (code) {
          // Whatever the block's header says, minus the code itself. The site
          // draws the info string there; its Copy control is an icon and
          // contributes no text. Collected directly rather than by re-reading
          // the <pre>, which would re-enter this branch forever.
          const info = headerTextOf(node, code);
          breakLine();
          push("```" + info);
          push("\n");
          push((code.textContent ?? "").replace(/^\n/, "").replace(/\n$/, ""));
          breakLine();
          push("```");
          breakLine();
          return;
        }
      }
      const pre = inPre || tag === "PRE";
      const block = LINE_BREAKING_TAGS.has(tag);
      if (block) breakLine();
      const children = node.childNodes ?? node.children ?? [];
      if (children.length === 0) walk({nodeType: 3, nodeValue: node.textContent ?? ""}, pre);
      else for (const child of children) walk(child, pre);
      if (block) breakLine();
    };
    walk(root, false);
    // Leading NEWLINES only: a reply that is nothing but one fenced block starts
    // with its own indentation, and that indentation is content.
    return chunks.join("").replace(/^\n+/, "").replace(/\s+$/, "");
  }

  /* Watch whether the document stays visible for the whole turn.
   *
   * A hidden document stops being painted, and a site that renders its reply
   * progressively then freezes it part-written -- the reply stops growing, the
   * stop control stops disappearing, and the turn looks finished while holding
   * a fragment. Creating the tab visible is not enough on its own, because a
   * turn runs for a minute or more and the user will click elsewhere in it.
   *
   * This records the fact rather than fighting for focus, so a turn that went
   * wrong this way says so instead of looking like a site that went quiet.
   */
  function watchVisibility() {
    let everHidden = document.visibilityState === "hidden";
    const onChange = () => { if (document.visibilityState === "hidden") everHidden = true; };
    document.addEventListener("visibilitychange", onChange);
    return {
      stop() { try { document.removeEventListener("visibilitychange", onChange); } catch (_) {} },
      get state() {
        return {
          visibility: document.visibilityState,
          hiddenNow: document.hidden === true,
          hiddenDuringTurn: everHidden,
          hasFocus: typeof document.hasFocus === "function" ? document.hasFocus() : null,
        };
      },
    };
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
  const BUILD = "0146dd21a7ec";

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
    watchVisibility,
    looksLikeCompleteJson,
    createSettleTracker,
    stopGeneration,
    observeText,
    createCompletionGate,
    createActivityWaiter,
  };
})();
