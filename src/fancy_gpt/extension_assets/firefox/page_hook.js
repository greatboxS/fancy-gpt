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
      sawDone: false, firstChunkMs: null,
    };
    const startedAt = Date.now();
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
          if (data === "[DONE]") { summary.sawDone = true; continue; }
          try { for (const name of fieldNames(JSON.parse(data))) summary.dataFields.add(name); } catch (_) {}
        }
      }
    } catch (_) {
      // The page may abandon the stream; what was seen so far is still useful.
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
