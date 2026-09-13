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

  // Everything a site streams is captured verbatim and decoded by the runtime.
  // Nothing here knows what a reply looks like: that knowledge changes as sites
  // change, and it must not live behind a browser reload.
  const CAPTURE_BYTES = 4 * 1024 * 1024;

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
    const captured = summary.contentType.includes("event-stream") ? [] : null;
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
          if (data === "[DONE]") { summary.sawDone = true; }
          if (captured && summary.bytes <= CAPTURE_BYTES) captured.push(data);
          if (data === "[DONE]") continue;
          try {
            const parsed = JSON.parse(data);
            for (const name of fieldNames(parsed)) summary.dataFields.add(name);
            noteDeltaShape(parsed, summary);
          } catch (_) {}
        }
      }
    } catch (_) {
      // The page may abandon the stream; what was seen so far is still useful.
    }
    if (captured && captured.length) {
      /* The stream's own events, unread.
       *
       * This is content rather than shape, and it is the model's reply -- the
       * same text the turn returns anyway. It is forwarded rather than decoded
       * because how to read it is knowledge that changes when a site changes,
       * and knowledge that lives in an extension can only be corrected by
       * asking someone to reload their browser. The runtime decodes it, where
       * it can be fixed and tested without leaving the repository.
       */
      report({kind: "events", path: summary.path, contentType: summary.contentType, events: captured});
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
          // Anything with a body is described. Restricting this to the two
          // content types ChatGPT happens to use would hide a site that frames
          // its reply differently -- which is exactly what we are here to find
          // out about the ones not yet measured.
          describeStream(response, shape);
        }).catch(() => {});
      }
    } catch (_) {
      // Observation must never change what the page gets back.
    }
    return result;
  };

  report({kind: "installed", path: shapeOfPath(location.href)});
})();
