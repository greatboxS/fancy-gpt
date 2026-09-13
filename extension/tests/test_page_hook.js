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

  report();
}

main();
