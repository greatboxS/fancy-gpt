/* ChatGPT site adapter. Owns every ChatGPT DOM assumption. */
(() => {
  // Everything that is not a ChatGPT DOM assumption comes from the shared kit,
  // so a second adapter starts from what already works rather than repeating it.
  const kit = globalThis.FancyGPTSiteKit;
  const {firstVisible, waitFor, findButtonByText, setComposer, looksLikeCompleteJson, stopGeneration,
         observeText, createCompletionGate, createActivityWaiter} = kit;

  const SELECTORS = {
    composer: ["#prompt-textarea", "textarea", '[contenteditable="true"]'],
    send: ['button[data-testid="send-button"]', 'button[aria-label*="Send"]'],
    stop: ['button[data-testid="stop-button"]', 'button[aria-label*="Stop"]'],
    // Other ways the page says it is still working. The stop control alone is
    // not enough: both independent reviews pointed out it disappears between a
    // tool or reasoning phase and the text that follows, and a turn judged
    // finished there is judged finished half way through. Selectors that do not
    // match simply contribute nothing, so listing several is cheap insurance.
    generating: [
      '[data-is-streaming="true"]',
      ".result-streaming",
      '[aria-busy="true"]',
      '[data-testid="thinking-indicator"]',
    ],
    turns: "[data-turn-id]",
    assistant: ['[data-message-author-role="assistant"]', '[data-testid="conversation-turn-assistant"]'],
    // The turn container also owns Copy/feedback controls. Reading its
    // innerText contaminates the protocol envelope with UI labels.
    assistantContent: ['[data-message-content]', '.markdown']
  };

  function composerRoot(composer) {
    return composer?.closest("form") ?? composer?.parentElement ?? document;
  }

  /* Is the page still working on this turn?
   *
   * Any one of these is positive evidence of activity. Their absence is not
   * evidence of completion, which is why the completion gate also requires the
   * text to stop changing.
   */
  function isGenerating() {
    if (firstVisible(SELECTORS.stop)) return true;
    for (const selector of SELECTORS.generating) {
      try { if (document.querySelector(selector)) return true; } catch (_) {}
    }
    return false;
  }

  function sendControl(composer) {
    return firstVisible(SELECTORS.send, composerRoot(composer)) ?? firstVisible(SELECTORS.send);
  }

  function describeControls(composer) {
    const counts = {};
    for (const selector of [...SELECTORS.send, ...SELECTORS.composer]) {
      counts[selector] = document.querySelectorAll(selector).length;
    }
    const text = composer ? (composer.value ?? composer.textContent ?? "") : "";
    return JSON.stringify({
      url: location.pathname,
      composerFound: Boolean(composer),
      composerChars: text.length,
      matches: counts,
    });
  }

  function turnIds() {
    return [...document.querySelectorAll(SELECTORS.turns)]
      .map(el => el.getAttribute("data-turn-id"))
      .filter(Boolean);
  }

  /* TEMPORARY PROBE -- remove once the stall is understood.
   *
   * Four live stalls reported a 25-45 character prefix while the finished
   * reply stood on screen. Two causes remain and they need opposite fixes:
   * the turn element holds the whole reply and our extraction loses it, or
   * the element holds the prefix too. Reading the turn's raw textContent
   * settles that, because it is the least clever thing that can be read: no
   * selector, no walk, no judgement about what any subtree means.
   *
   * It is not the end state. Raw textContent has no line breaks, so a fenced
   * `fancygpt:<id>` block would be unreadable -- which is exactly why the walk
   * exists. Once the numbers say which cause is real, this goes away. */
  // Tests set this to false to keep exercising the real extraction path.
  const RAW_TEXT_PROBE = globalThis.FANCY_GPT_RAW_TEXT_PROBE !== false;

  function assistantText(turnId) {
    const turns = [...document.querySelectorAll(SELECTORS.turns)].filter(el => el.getAttribute("data-turn-id") === turnId);
    if (turns.length !== 1) return null;
    const turn = turns[0];
    const {readLiveText} = globalThis.FancyGPTSiteKit;
    if (RAW_TEXT_PROBE) return (turn.textContent ?? "").trim() || null;
    const role = turn.getAttribute("data-message-author-role");
    if (role === "assistant") {
      for (const selector of SELECTORS.assistantContent) {
        // Every matching part, not just the first: ChatGPT splits one assistant
        // turn into several content parts, and reading one of them truncates
        // the reply into something that can never parse as a complete envelope.
        const parts = [...turn.querySelectorAll(selector)]
          // A nested match is already covered by the ancestor that contains it.
          .filter((node, _, all) => !all.some(other => other !== node && other.contains?.(node)))
          .map(node => readLiveText(node))
          .filter(Boolean);
        if (parts.length) return parts.join("\n").trim() || null;
      }
      return readLiveText(turn) || null;
    }
    for (const selector of SELECTORS.assistant) {
      if (turn.matches?.(selector)) return readLiveText(turn) || null;
      const child = turn.querySelector(selector);
      if (child) return readLiveText(child) || null;
    }
    return null;
  }

  function currentConversationId() {
    const match = location.pathname.match(/^\/c\/([a-zA-Z0-9-]+)/);
    return match ? match[1] : null;
  }

  async function healthCheck() {
    if (location.hostname !== "chatgpt.com") return {ok: false, reason: "unexpected-host", build: kit.build};
    const composer = firstVisible(SELECTORS.composer);
    return {
      ok: Boolean(composer),
      reason: composer ? "ready" : "composer-unavailable",
      build: kit.build,
    };
  }

  async function executeTurn(prompt, timeoutMs, onProgress, options = {}) {
    if (location.hostname !== "chatgpt.com") throw new Error("FancyGPT ChatGPT adapter loaded on unexpected host");
    const composer = await waitFor(() => firstVisible(SELECTORS.composer), 20000, "ChatGPT composer unavailable; sign in first");
    /* Remember what each turn said, not merely that it existed.
     *
     * A fast reply is not always a new turn: the page can answer by updating a
     * turn that was already on screen. Treating "already in the baseline" as
     * "not mine" then skips the reply forever and the turn stalls with the
     * answer visible - a false timeout on exactly the quickest replies.
     */
    const baseline = new Map(turnIds().map(id => [id, assistantText(id) ?? ""]));
    // Resuming an existing conversation lands on a page that is still hydrating:
    // the composer is already visible, but the app has not attached to it yet, so
    // a single write is silently dropped and the send button never appears. Keep
    // re-writing until the app acknowledges by revealing the send control.
    let send = null;
    let target = composer;
    for (let attempt = 0; attempt < 6 && !send; ++attempt) {
      target = firstVisible(SELECTORS.composer) || composer;
      setComposer(target, prompt);
      try {
        send = await waitFor(() => sendControl(target), 2500, "send control not ready yet");
      } catch (_) {
        await new Promise(resolve => setTimeout(resolve, 600));
      }
    }
    if (!send) {
      throw new Error("ChatGPT send control unavailable: " + describeControls(target));
    }

    /* Clicking send is not the same as the page accepting the prompt.
     *
     * A click that the app ignores leaves the prompt sitting in the composer,
     * and the turn then waits out its whole timeout for a reply that was never
     * requested - which looks, from outside, exactly like the model being slow.
     * Wait for the page to show that it took the prompt, and retry the click a
     * couple of times before reporting it as a rejected submission.
     */
    // Deliberately synchronous: waitFor does not await its getter, so an
    // async one would return a promise, which is always truthy, and every
    // submission would look accepted.
    const submitted = () => {
      const current = firstVisible(SELECTORS.composer);
      const composerText = current ? (current.value ?? current.textContent ?? "") : "";
      // Any one of these means the app acted on the submission.
      if (!composerText.includes(prompt.slice(0, 32))) return true;
      if (isGenerating()) return true;
      // A reply fast enough to land before the composer is observed as cleared
      // still counts as acceptance.
      return turnIds().some(id => {
        const text = assistantText(id);
        return Boolean(text) && (!baseline.has(id) || baseline.get(id) !== text);
      });
    };

    /* Never submit a composer the user has typed into since we wrote to it.
     *
     * The automation window is meant to be untouched, but it is a real browser
     * window and the user can reach it. Sending then would put their keystrokes
     * into an automated turn, and put our prompt into their conversation.
     * Trusted events are user input; synthetic ones are ours.
     */
    let userTouchedComposer = false;
    const noteUserInput = event => { if (event && event.isTrusted) userTouchedComposer = true; };
    for (const name of ["keydown", "paste", "input"]) {
      try { target.addEventListener(name, noteUserInput, true); } catch (_) {}
    }
    const releaseComposerWatch = () => {
      for (const name of ["keydown", "paste", "input"]) {
        try { target.removeEventListener(name, noteUserInput, true); } catch (_) {}
      }
    };

    let accepted = false;
    for (let attempt = 0; attempt < 3 && !accepted; ++attempt) {
      if (userTouchedComposer) {
        releaseComposerWatch();
        throw new Error("ChatGPT composer was edited by the user after the prompt was written; refusing to submit");
      }
      // Cancelling while the page has not taken the prompt is the cleanest
      // possible cancellation: nothing was submitted, so there is nothing
      // uncertain about it.
      if (options?.isCancelled?.()) {
        return {
          text: "", responseIdentity: "chatgpt-cancelled",
          conversationId: currentConversationId(), cancelled: true,
          stoppedGeneration: stopGeneration(SELECTORS.stop),
        };
      }
      if (attempt > 0) {
        const again = sendControl(target) || firstVisible(SELECTORS.send);
        if (!again) break;
        again.click();
      } else {
        send.click();
      }
      try {
        await waitFor(submitted, 3000, "not accepted yet");
        accepted = true;
      } catch (_) { /* try again */ }
    }
    releaseComposerWatch();
    if (!accepted) {
      throw new Error(
        "ChatGPT did not accept the submitted prompt (composer still holds it): " + describeControls(target)
      );
    }

    /* Give up on inactivity, not on elapsed time.
     *
     * A fixed deadline is the wrong signal: a long reasoning turn can
     * legitimately run past any constant, and killing it while the page is
     * visibly still working reports a failure that did not happen. What
     * actually indicates a stuck turn is the page doing nothing - no new text
     * and no generating indicator - for a while.
     *
     * The absolute cap remains only as a backstop against a turn that never
     * ends at all.
     */
    const idleLimitMs = Math.max(15000, Number(options?.idleTimeoutMs ?? 90000));
    // Counted so a stall can say whether the unthrottled clock was reaching us
    // at all. If a turn stalls with zero ticks, the clock is the fault; if it
    // stalls despite ticks, the fault is in what we do when we wake.
    let tickCount = 0;
    let wakeCount = 0;
    const hardDeadline = Date.now() + timeoutMs;
    let lastActivityAt = Date.now();
    let lastSeenText = null;
    let boundId = null;
    // Progress is reported by a DOM observer rather than by this loop, because
    // the loop's timer is throttled while the automation window is hidden.
    let stopObserving = null;
    // Cancellation is checked inside the poll loop: clicking stop ends
    // generation in the page, and whatever text exists is returned as partial.
    const checkCancelled = () => {
      if (!options?.isCancelled?.()) return null;
      const stopped = stopGeneration(SELECTORS.stop);
      return {
        text: boundId != null ? (assistantText(boundId) ?? "") : "",
        responseIdentity: boundId ?? "chatgpt-cancelled",
        conversationId: currentConversationId?.() ?? null,
        cancelled: true,
        stoppedGeneration: stopped,
      };
    };
    let lastReported = null;
    const gate = createCompletionGate({stabilityMs: 1200});
    const activity = createActivityWaiter();
    let maxEvaluateMs = 0;
    // A tick pushed from the background worker is an unthrottled clock: a reply
    // that has finished produces no more mutations to wake us with.
    if (options?.onTick) options.onTick(() => { tickCount += 1; activity.notify(); });
    const release = () => { activity.stop(); if (stopObserving) stopObserving(); };
    while (Date.now() < hardDeadline && Date.now() - lastActivityAt < idleLimitMs) {
      const evaluateStartedAt = Date.now();
      const candidates = [];
      for (const id of turnIds()) {
        const text = assistantText(id);
        if (!text) continue;
        // New, or an existing turn whose content changed after we submitted.
        if (baseline.has(id) && baseline.get(id) === text) continue;
        candidates.push({id, text});
      }
      if (boundId != null && assistantText(boundId) === null) {
        /* The turn we bound to is gone.
         *
         * ChatGPT shows a "Thinking" placeholder turn and then replaces it with
         * the real answer under a DIFFERENT data-turn-id. Staying bound to the
         * placeholder means assistantText() returns null forever, no completion
         * signal ever arrives, and the turn hangs until its timeout with the
         * answer sitting finished on screen. Observed live: progress stopped at
         * "Thinking" and the reply was never seen.
         */
        boundId = null;
        if (stopObserving) { stopObserving(); stopObserving = null; }
      }
      if (boundId == null) {
        if (candidates.length > 1) throw new Error("ambiguous ChatGPT response: multiple new assistant turns");
        if (candidates.length === 1) boundId = candidates[0].id;
      }
      // Checked after binding, not before: a cancel arriving before the first
      // successful bind would otherwise discard a reply that is already on
      // screen and report an empty result.
      const cancelled = checkCancelled();
      if (cancelled) {
        release();
        return cancelled;
      }
      if (boundId != null) {
        // A long conversation may retain a visible "Continue generating"
        // button on an older response. Searching the whole document and
        // clicking it before binding the new response traps this job in the
        // loop forever. Only continue the assistant turn created by this job.
        const boundTurns = [...document.querySelectorAll(SELECTORS.turns)]
          .filter(el => el.getAttribute("data-turn-id") === boundId);
        const continueButton = boundTurns.length === 1
          ? findButtonByText(/continue generating/i, boundTurns[0])
          : null;
        if (continueButton) {
          continueButton.click();
          await activity.wait(500);
          continue;
        }
        if (onProgress && stopObserving === null && boundTurns.length === 1) {
          const observedId = boundId;
          // Scoped to this reply: observing the whole document would re-scan it
          // on every unrelated mutation the page makes.
          stopObserving = observeText(
            () => assistantText(observedId),
            text => { lastReported = text; onProgress(text); },
            {target: boundTurns[0]},
          );
        }
        const text = assistantText(boundId);
        // "No stop control" is negative evidence only: it also vanishes between
        // a tool phase and the text that follows. Treat any generating
        // indicator as still active.
        const active = isGenerating();
        // Either the reply growing or the site saying it is working counts as
        // the turn being alive, and resets the idle clock.
        if (active || text !== lastSeenText) {
          lastActivityAt = Date.now();
          lastSeenText = text;
        }
        const complete = text != null && looksLikeCompleteJson(text);
        const settled = gate.observe({text, active, complete});
        if (settled) {
          maxEvaluateMs = Math.max(maxEvaluateMs, Date.now() - evaluateStartedAt);
          const diagnostics = {
            ticksReceived: tickCount, loopIterations: wakeCount,
            maxEvaluateMs, waiter: activity.stats(), completionCandidateAgeMs: gate.candidateAgeMs,
          };
          release();
          return {
            text: settled.text, responseIdentity: boundId,
            conversationId: currentConversationId(), diagnostics,
          };
        }
      }
      maxEvaluateMs = Math.max(maxEvaluateMs, Date.now() - evaluateStartedAt);
      // Woken by a mutation or a background tick; the delay is only a floor.
      wakeCount += 1;
      const waitMs = gate.remainingMs == null ? 500 : Math.max(1, Math.min(500, gate.remainingMs));
      await activity.wait(waitMs);
    }
    release();
    /* A timeout that only says "timed out" cannot be diagnosed without
     * reproducing it, and this one is intermittent. Report what the adapter
     * could actually see. Shapes and counts only - never page text, which
     * carries the user's content. */
    const newTurns = turnIds().filter(id => {
      const text = assistantText(id);
      return Boolean(text) && (!baseline.has(id) || baseline.get(id) !== text);
    });
    const boundText = boundId != null ? assistantText(boundId) : null;
    /* Where the text went, in shapes only.
     *
     * A stall whose bound turn holds far more text than the adapter extracted
     * is an extraction bug, not a site that went quiet, and the two are
     * indistinguishable from a character count alone. Selector strings are our
     * own constants and the numbers are lengths, so no page content is
     * reported. */
    const boundTurnElement = boundId == null ? null
      : [...document.querySelectorAll(SELECTORS.turns)]
          .find(el => el.getAttribute("data-turn-id") === boundId) ?? null;
    const extraction = boundTurnElement == null ? null : {
      turnChars: (boundTurnElement.textContent ?? "").length,
      bySelector: SELECTORS.assistantContent.map(selector => ({
        selector,
        matches: boundTurnElement.querySelectorAll(selector).length,
        chars: [...boundTurnElement.querySelectorAll(selector)]
          .map(node => (node.textContent ?? "").length),
      })),
    };
    const idleFor = Math.round((Date.now() - lastActivityAt) / 1000);
    const stallReason = Date.now() >= hardDeadline ? "absolute limit" : `no activity for ${idleFor}s`;
    throw new Error(`ChatGPT response stalled (${stallReason}); ` + JSON.stringify({
      boundId: boundId != null ? "bound" : "unbound",
      boundTurnStillPresent: boundId != null
        ? document.querySelectorAll(SELECTORS.turns).length > 0 &&
          [...document.querySelectorAll(SELECTORS.turns)]
            .some(el => el.getAttribute("data-turn-id") === boundId)
        : null,
      boundTextChars: boundText == null ? null : boundText.length,
      extraction,
      boundTextComplete: boundText != null && looksLikeCompleteJson(boundText),
      newTurnCount: newTurns.length,
      newTurnsWithText: newTurns.filter(id => assistantText(id)).length,
      generating: isGenerating(),
      candidatePending: gate.pending,
      completionCandidateAgeMs: gate.candidateAgeMs,
      ticksReceived: tickCount,
      loopIterations: wakeCount,
      maxEvaluateMs,
      waiter: activity.stats(),
      idleSeconds: idleFor,
    }));
  }

  globalThis.FancyGPTSites = globalThis.FancyGPTSites ?? {};
  globalThis.FancyGPTSites.chatgpt = {
    id: "chatgpt",
    freshUrl: "https://chatgpt.com/?temporary-chat=true",
    healthCheck,
    executeTurn
  };
})();
