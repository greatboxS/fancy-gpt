/* Watch the page's own network calls, from inside the page.
 *
 * Everything this system reads today comes from the rendered DOM, and the
 * browser stops rendering a document it is not showing. That is not a bug to
 * be worked around; it is the ceiling of reading a page instead of reading
 * what the page was sent. The network does not stop in a hidden tab -- only
 * the painting does -- so the reply is still arriving even when the DOM has
 * frozen part-written.
 *
 * Before anything is built on that, it has to be known rather than assumed:
 * which request carries a reply, whether it is a stream, how it frames its
 * events, and what those events are shaped like. This file answers exactly
 * that and changes nothing else. It does not read a reply, does not hand one
 * back, and does not alter what the page receives.
 *
 * It records shapes and counts and never content, which is the same rule the
 * turn diagnostics follow: field names, event names, sizes, counts. Path
 * segments that look like identifiers are reduced to <id>, so a conversation
 * cannot be named by what is reported here.
 *
 * It must run in the page's own world to see the page's fetch, so the content
 * script injects it as a script element rather than as an isolated content
 * script. That works the same way in Chrome, Edge and Firefox.
 */
(() => {
  const CHANNEL = "fancygpt-page-hook";
  if (window.__fancyGptPageHook) return;
  window.__fancyGptPageHook = true;

  const ID_LIKE = /^[0-9a-f-]{8,}$|^\d+$/i;
  // Measured: the reply arrives on this path as text/event-stream.
  const REPLY_PATH = /\/backend-api\/[^/]*\/?conversation$/;
  const shapeOfPath = url => {
    try {
      const {pathname} = new URL(url, location.href);
      return pathname.split("/").map(part => (ID_LIKE.test(part) ? "<id>" : part)).join("/");
    } catch (_) {
      return "<unparseable>";
    }
  };

  // Field names only, never values, and only from the top level.
  const fieldNames = value => {
    if (!value || typeof value !== "object" || Array.isArray(value)) return [];
    return Object.keys(value).slice(0, 40);
  };

  /* The vocabulary of a delta, not its contents.
   *
   * The stream does not append text, it patches a document: each delta carries
   * an operation, a JSON-pointer path and a value. A decoder written on the
   * assumption that values concatenate would be wrong in a way that produces
   * plausible text, which is the worst kind. So the operations and the shape of
   * the paths are recorded -- pointer segments are structure, and array indices
   * are reduced to <n> -- while values are never touched beyond their type.
   */
  const noteDeltaShape = (parsed, summary) => {
    const visit = entry => {
      if (!entry || typeof entry !== "object") return;
      if (typeof entry.o === "string") summary.ops.add(entry.o.slice(0, 24));
      if (typeof entry.p === "string") {
        summary.paths.add(entry.p.replace(/\/\d+/g, "/<n>").slice(0, 80));
      }
      if ("v" in entry) {
        const value = entry.v;
        summary.valueTypes.add(Array.isArray(value) ? "array" : typeof value);
        if (Array.isArray(value)) for (const item of value) visit(item);
        else if (value && typeof value === "object" && ("o" in value || "p" in value)) visit(value);
      }
    };
    visit(parsed);
    if (Array.isArray(parsed)) for (const item of parsed) visit(item);
  };

  const report = payload => {
    try { window.postMessage({source: CHANNEL, ...payload}, location.origin); } catch (_) {}
  };

  const describeStream = async (response, shape) => {
    // A clone, so the page's own consumption is untouched: the hook must be
    // invisible to the application it is watching.
    let body;
    try { body = response.clone().body; } catch (_) { return; }
    if (!body) return;
    const reader = body.getReader();
    const decoder = new TextDecoder();
    const summary = {
      path: shape, status: response.status,
      contentType: response.headers.get("content-type") || "",
      chunks: 0, bytes: 0, eventNames: new Set(), dataFields: new Set(),
      ops: new Set(), paths: new Set(), valueTypes: new Set(),
      sawDone: false, firstChunkMs: null,
    };
    const startedAt = Date.now();
    const decoder2 = REPLY_PATH.test(shape) ? createDeltaDecoder() : null;
    let pending = "";
    try {
      for (;;) {
        const {done, value} = await reader.read();
        if (done) break;
        summary.chunks += 1;
        summary.bytes += value?.byteLength ?? 0;
        if (summary.firstChunkMs === null) summary.firstChunkMs = Date.now() - startedAt;
        pending += decoder.decode(value, {stream: true});
        const lines = pending.split("\n");
        pending = lines.pop() ?? "";
        for (const line of lines) {
          if (line.startsWith("event:")) { summary.eventNames.add(line.slice(6).trim().slice(0, 40)); continue; }
          if (!line.startsWith("data:")) continue;
          const data = line.slice(5).trim();
          if (data === "[DONE]") { summary.sawDone = true; if (decoder2) decoder2.markDone(); continue; }
          try {
            const parsed = JSON.parse(data);
            for (const name of fieldNames(parsed)) summary.dataFields.add(name);
            noteDeltaShape(parsed, summary);
            if (decoder2) decoder2.handle(parsed);
          } catch (_) {}
        }
      }
    } catch (_) {
      // The page may abandon the stream; what was seen so far is still useful.
    }
    if (decoder2) {
      /* The reply itself, carried back for comparison.
       *
       * This is the one thing here that is content rather than shape, and it is
       * the model's own reply -- the same text the turn returns anyway. It is
       * not used as the answer yet: it travels beside the reply read from the
       * page so the two can be checked against each other on real turns, before
       * anything depends on this decoder being right.
       */
      report({
        kind: "reply",
        path: summary.path,
        conversationId: decoder2.conversationId,
        messageId: decoder2.messageId,
        text: decoder2.text,
        stats: decoder2.stats(),
      });
    }
    report({
      kind: "stream",
      path: summary.path,
      status: summary.status,
      contentType: summary.contentType,
      chunks: summary.chunks,
      bytes: summary.bytes,
      eventNames: [...summary.eventNames].slice(0, 20),
      dataFields: [...summary.dataFields].slice(0, 40),
      ops: [...summary.ops].slice(0, 20),
      paths: [...summary.paths].slice(0, 30),
      valueTypes: [...summary.valueTypes].slice(0, 10),
      sawDone: summary.sawDone,
      firstChunkMs: summary.firstChunkMs,
      totalMs: Date.now() - startedAt,
    });
  };


  /* Rebuild the reply from the stream that carried it.
   *
   * Observed vocabulary, from a live turn rather than from documentation:
   * operations add, append, patch and replace; paths "", /message/status,
   * /message/end_turn, /message/metadata and /message/content/parts/<n>.
   * An event that carries only a value continues the previous path, which is
   * how the encoding stays small -- and is exactly the detail a decoder
   * written from assumption would miss, producing text that looks right.
   *
   * Nothing here guesses. An operation it does not know is counted and
   * ignored, never approximated, and the count travels with the result so a
   * decoder that has fallen behind the site says so instead of quietly
   * returning a shorter reply.
   */
  function createDeltaDecoder() {
    let document = null;
    let lastPath = null;
    let finished = false;
    const unknownOps = new Set();
    const skippedPaths = new Set();
    let applied = 0;
    let skipped = 0;
    // A skip that touched a text part is the one that matters: metadata we
    // cannot place changes nothing, but a dropped piece of the reply is a
    // shorter answer that still looks complete.
    let skippedText = 0;

    const segments = pointer => pointer.split("/").slice(1).map(part =>
      part.replace(/~1/g, "/").replace(/~0/g, "~"));

    const container = (pointer, create) => {
      const parts = segments(pointer);
      if (!parts.length) return null;
      let node = document;
      for (const part of parts.slice(0, -1)) {
        if (node == null) return null;
        if (node[part] === undefined && create) node[part] = {};
        node = node[part];
      }
      return node == null ? null : {node, key: parts[parts.length - 1]};
    };

    const apply = (operation, pointer, value) => {
      if (pointer === "" || pointer == null) {
        if (operation === "add" || operation === "replace") {
          document = value && typeof value === "object" ? value : document;
          applied += 1;
          return;
        }
      }
      const target = container(pointer, true);
      if (!target) {
        skipped += 1;
        const shape = String(pointer ?? "").replace(/\/\d+/g, "/<n>").slice(0, 60);
        skippedPaths.add(shape);
        if (shape.includes("/content/parts")) skippedText += 1;
        return;
      }
      const {node, key} = target;
      if (operation === "append") {
        node[key] = (node[key] ?? "") + String(value ?? "");
        applied += 1;
      } else if (operation === "add" || operation === "replace") {
        node[key] = value;
        applied += 1;
      } else {
        unknownOps.add(String(operation).slice(0, 24));
        skipped += 1;
        const shape = String(pointer ?? "").replace(/\/\d+/g, "/<n>").slice(0, 60);
        skippedPaths.add(shape);
        if (shape.includes("/content/parts")) skippedText += 1;
      }
    };

    const handle = entry => {
      if (!entry || typeof entry !== "object") return;
      const operation = typeof entry.o === "string" ? entry.o : null;
      if (operation === "patch") {
        // A batch: each member carries its own path and operation.
        if (Array.isArray(entry.v)) for (const member of entry.v) handle(member);
        return;
      }
      if (typeof entry.p === "string") lastPath = entry.p;
      if (operation) {
        apply(operation, typeof entry.p === "string" ? entry.p : lastPath, entry.v);
        return;
      }
      if ("v" in entry) {
        // No operation given: the encoding continues the previous path, and
        // for the reply that path is always a text part.
        apply("append", lastPath, entry.v);
      }
    };

    return {
      handle(parsed) {
        if (parsed && typeof parsed === "object" && parsed.message && !parsed.o && !parsed.p) {
          // The opening snapshot arrives as a whole conversation event.
          document = {message: parsed.message};
          applied += 1;
          return;
        }
        handle(parsed);
      },
      markDone() { finished = true; },
      get conversationId() { return document?.conversation_id ?? null; },
      get messageId() { return document?.message?.id ?? null; },
      get text() {
        const parts = document?.message?.content?.parts;
        if (!Array.isArray(parts)) return "";
        return parts.filter(part => typeof part === "string").join("");
      },
      get endTurn() { return document?.message?.end_turn === true; },
      get status() { return document?.message?.status ?? null; },
      stats() {
        return {
          applied, skipped, skippedText, finished,
          unknownOps: [...unknownOps].slice(0, 10),
          skippedPaths: [...skippedPaths].slice(0, 10),
          endTurn: this.endTurn, status: this.status,
          // What the decoder itself thinks of its result. Anything it could not
          // place inside the reply makes this false, because a reply that is
          // quietly short is the failure this whole exercise exists to avoid.
          trustworthy: skippedText === 0 && unknownOps.size === 0 && finished,
        };
      },
    };
  }

  const nativeFetch = window.fetch;
  window.fetch = function fancyGptObservedFetch(input, init) {
    const url = typeof input === "string" ? input : input?.url ?? "";
    const method = String(init?.method ?? (typeof input === "object" ? input?.method : "") ?? "GET").toUpperCase();
    const result = nativeFetch.apply(this, arguments);
    try {
      const shape = shapeOfPath(url);
      // Only what could plausibly carry a reply: a POST that streams back.
      if (method === "POST") {
        result.then(response => {
          const type = response.headers.get("content-type") || "";
          if (type.includes("event-stream") || type.includes("json")) describeStream(response, shape);
          else report({kind: "response", path: shape, status: response.status, contentType: type});
        }).catch(() => {});
      }
    } catch (_) {
      // Observation must never change what the page gets back.
    }
    return result;
  };

  report({kind: "installed", path: shapeOfPath(location.href)});
})();
