/* Run the page hook, rather than parse it.
 *
 * It was once left calling two functions a refactor had deleted. Every check
 * in place passed -- the file parsed, the extension built, the suites were
 * green -- because a missing reference is not a syntax error, and nothing
 * executed the file. In the browser it threw on its first line of real work
 * and reported nothing at all, which reads exactly like a site that makes no
 * requests, and cost a wrong diagnosis and a reload cycle to find.
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const {test, assert, assertEqual, report} = require("./harness.js");

const SOURCE = fs.readFileSync(path.join(__dirname, "..", "common", "page_hook.js"), "utf8");

function sseResponse(lines, contentType = "text/event-stream") {
  const body = lines.map(line => `data: ${line}\n`).join("") + "\n";
  const chunks = [new TextEncoder().encode(body)];
  const make = () => ({
    status: 200,
    headers: {get: name => (name === "content-type" ? contentType : null)},
    clone() { return make(); },
    get body() {
      let index = 0;
      return {getReader: () => ({read: async () => (index < chunks.length
        ? {done: false, value: chunks[index++]}
        : {done: true, value: undefined})})};
    },
  });
  return make();
}

function runHook({response}) {
  const posted = [];
  const context = {
    console,
    TextDecoder,
    TextEncoder,
    URL,
    Date,
    JSON,
    Promise,
    location: {href: "https://chatgpt.com/", origin: "https://chatgpt.com"},
    XMLHttpRequest: class FakeXhr {
      constructor() { this.status = 200; this.responseText = ""; this.listeners = {}; }
      open(method, url) { this.method = method; this.url = url; }
      send() { FakeXhr.sent.push(this); }
      addEventListener(type, handler) { (this.listeners[type] ??= []).push(handler); }
      getResponseHeader() { return "application/json"; }
      grow(text) {
        this.responseText = text;
        this.readyState = 3;
        for (const h of this.listeners.readystatechange ?? []) h();
      }
      finish(text) { this.responseText = text; for (const h of this.listeners.loadend ?? []) h(); }
      static sent = [];
    },
    WebSocket: class FakeSocket {
      constructor(url) { this.url = url; this.listeners = {}; FakeSocket.made.push(this); }
      addEventListener(type, handler) { (this.listeners[type] ??= []).push(handler); }
      emit(type, event) { for (const handler of this.listeners[type] ?? []) handler(event); }
      static made = [];
    },
  };
  context.window = context;
  context.window.postMessage = message => posted.push(message);
  context.window.fetch = async () => response;
  vm.createContext(context);
  vm.runInContext(SOURCE, context, {filename: "page_hook.js"});
  return {context, posted};
}

async function main() {
  await test("the hook installs and announces itself", async () => {
    const {context, posted} = runHook({response: sseResponse(["{}"])});
    assert(context.window.__fancyGptPageHook === true, "it must mark itself installed");
    const installed = posted.find(item => item.kind === "installed");
    assert(installed, "an install with no announcement is indistinguishable from one that threw");
    assertEqual(installed.source, "fancygpt-page-hook", "messages are namespaced");
  });

  await test("a streamed reply is captured verbatim and reported", async () => {
    const events = ['{"p":"/message/content/parts/0","o":"append","v":"hi"}', '{"v":"!"}', "[DONE]"];
    const {context, posted} = runHook({response: sseResponse(events)});
    await context.window.fetch("https://chatgpt.com/backend-api/f/conversation", {method: "POST"});
    await new Promise(resolve => setTimeout(resolve, 20));

    const captured = posted.find(item => item.kind === "events");
    assert(captured, "the events a turn depends on must reach the content script");
    assertEqual(captured.events, events, "forwarded unread, exactly as they arrived");
    const shape = posted.find(item => item.kind === "stream");
    assert(shape, "the shape report accompanies them");
    assertEqual(shape.sawDone, true, "the stream's own terminator is noticed");
  });

  await test("identifying path segments never leave the page", async () => {
    const {context, posted} = runHook({response: sseResponse(["{}"])});
    await context.window.fetch("https://chatgpt.com/backend-api/conversation/abc123def456", {method: "POST"});
    await new Promise(resolve => setTimeout(resolve, 20));
    const shape = posted.find(item => item.kind === "stream");
    assert(shape && !shape.path.includes("abc123def456"), "an id must be reduced, not reported");
  });

  await test("the page still gets its own response back", async () => {
    const response = sseResponse(["{}"]);
    const {context} = runHook({response});
    const returned = await context.window.fetch("https://chatgpt.com/x", {method: "POST"});
    assertEqual(returned, response, "observation must be invisible to the page");
  });

  await test("only an event stream is forwarded verbatim", async () => {
    // A JSON response gets a shape report like everything else, but its body is
    // not carried out of the page. Forwarding is for the reply a turn is
    // waiting on, not for every call a site happens to make.
    const {context, posted} = runHook({response: sseResponse(['{"a":1}'], "application/json")});
    await context.window.fetch("https://chatgpt.com/backend-api/lat/r", {method: "POST"});
    await new Promise(resolve => setTimeout(resolve, 20));
    assert(posted.some(item => item.kind === "stream"), "its shape is still reported");
    assert(!posted.some(item => item.kind === "events"), "its body is not");
  });

  await test("a GET is left alone entirely", async () => {
    const {context, posted} = runHook({response: sseResponse(["{}"])});
    await context.window.fetch("https://chatgpt.com/anything");
    await new Promise(resolve => setTimeout(resolve, 20));
    assert(!posted.some(item => item.kind === "stream" || item.kind === "events"),
      "a reply is submitted, so only what the page posts is worth watching");
  });

  await test("a site that streams over a socket is not invisible", async () => {
    // Copilot carries its reply in SignalR frames, not a response body. A hook
    // that watches only fetch reports nothing there -- the same silence as a
    // hook that failed to load, which has already cost one wrong diagnosis.
    const {context, posted} = runHook({response: sseResponse(["{}"])});
    const socket = new context.window.WebSocket("wss://example.test/chat/abc123def456");
    socket.emit("message", {data: "frame one"});
    socket.emit("message", {data: "frame two"});
    socket.emit("close", {});

    const seen = posted.find(item => item.kind === "socket");
    assert(seen, "its traffic must show up as something");
    assertEqual(seen.frames, 2, "frames are counted");
    assert(!seen.path.includes("abc123def456"), "and identifiers still never leave the page");
    assert(!JSON.stringify(seen).includes("frame one"), "frames are measured, not carried out");
  });

  await test("a site that answers over XHR is not invisible", async () => {
    // Gemini reported nothing while ChatGPT reported eight requests on the same
    // turn: the signature of watching the wrong transport, not of a quiet site.
    // Google's batchexecute endpoints are driven through XHR.
    const {context, posted} = runHook({response: sseResponse(["{}"])});
    const request = new context.window.XMLHttpRequest();
    request.open("POST", "https://gemini.google.com/_/BardChatUi/data/assistant/StreamGenerate?rt=c");
    request.send("f.req=...");
    request.finish(")]}'\n\n[[\"wrb.fr\",null,\"payload\"]]");

    const seen = posted.find(item => item.kind === "xhr");
    assert(seen, "its traffic must show up as something");
    assert(seen.path.includes("StreamGenerate"), "and be identifiable by endpoint");
    assert(seen.chars > 0, "with the size that arrived");
    assert(!JSON.stringify(seen).includes("wrb.fr"), "measured, not carried out of the page");
  });

  await test("a response that grows is told apart from one that arrives whole", async () => {
    /* The distinction decides what a decoder can be responsible for.
     *
     * A body that grows can carry a reply as it is written; one that appears
     * complete says nothing until it is finished, so the page still has to say
     * when the turn is done. A total size cannot tell those apart.
     */
    const {context, posted} = runHook({response: sseResponse(["{}"])});

    const streamed = new context.window.XMLHttpRequest();
    streamed.open("POST", "https://gemini.google.com/_/BardChatUi/data/StreamGenerate");
    streamed.send();
    streamed.grow("part");
    streamed.grow("part and more");
    streamed.finish("part and more and the rest");

    const whole = new context.window.XMLHttpRequest();
    whole.open("POST", "https://gemini.google.com/_/BardChatUi/data/batchexecute");
    whole.send();
    whole.grow("all of it at once");
    whole.finish("all of it at once");

    const reports = posted.filter(item => item.kind === "xhr");
    assertEqual(reports.length, 2, "both are reported");
    assertEqual(reports[0].progressive, true, "a growing body is recognised");
    assertEqual(reports[1].progressive, false, "one that was already complete is not");
    assert(!JSON.stringify(reports).includes("the rest"), "growth is measured, never carried out");
  });

  await test("only a body that grew is carried out of the page", async () => {
    // A response nobody watched grow is far more likely to be telemetry than
    // an answer, and there is no reason to move it.
    const {context, posted} = runHook({response: sseResponse(["{}"])});

    const streamed = new context.window.XMLHttpRequest();
    streamed.open("POST", "https://gemini.google.com/_/BardChatUi/data/StreamGenerate");
    streamed.send();
    streamed.grow("start");
    streamed.finish("start and the answer");

    const whole = new context.window.XMLHttpRequest();
    whole.open("POST", "https://gemini.google.com/_/BardChatUi/data/batchexecute");
    whole.send();
    whole.grow("telemetry");
    whole.finish("telemetry");

    const bodies = posted.filter(item => item.kind === "body");
    assertEqual(bodies.length, 1, "exactly the one that grew");
    assert(bodies[0].text.includes("the answer"), "forwarded whole, for the runtime to read");
    assert(!JSON.stringify(bodies).includes("telemetry"), "and nothing else");
  });

  await test("a reply that arrived too fast to be seen growing is still forwarded", async () => {
    /* Growth is a hint, not the test.
     *
     * Two live turns answered quickly enough that nothing was sampled mid
     * flight, so nothing was forwarded and the decoder was left with a few
     * hundred characters of telemetry and no answer to find.
     */
    const {context, posted} = runHook({response: sseResponse(["{}"])});
    const request = new context.window.XMLHttpRequest();
    request.open("POST", "https://gemini.google.com/_/BardChatUi/data/StreamGenerate");
    request.send();
    request.finish("x".repeat(4000));
    await new Promise(resolve => setTimeout(resolve, 20));

    const bodies = posted.filter(item => item.kind === "body");
    assertEqual(bodies.length, 1, "size alone is enough when growth was never seen");
  });

  await test("chatter is still left in the page", async () => {
    const {context, posted} = runHook({response: sseResponse(["{}"])});
    const request = new context.window.XMLHttpRequest();
    request.open("POST", "https://gemini.google.com/_/BardChatUi/data/batchexecute");
    request.send();
    request.finish("a few hundred characters of telemetry");
    await new Promise(resolve => setTimeout(resolve, 20));

    assert(!posted.some(item => item.kind === "body"),
      "a reply, framing included, is not a few hundred characters");
  });

  report();
}

main();
