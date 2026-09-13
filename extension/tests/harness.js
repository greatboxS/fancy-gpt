/* A dependency-free test runner and adapter loader. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const {resetDom} = require("./dom_stub.js");

const COMMON = path.join(__dirname, "..", "common");
const results = {passed: 0, failed: 0, failures: []};
let currentTest = "(none)";

// A dangling rejection would otherwise crash the runner with no indication of
// which test produced it.
process.on("unhandledRejection", error => {
  console.error(`UNHANDLED REJECTION during "${currentTest}": ${error && error.message || error}`);
  results.failed += 1;
  results.failures.push(`${currentTest}: unhandled rejection: ${error && error.message || error}`);
});

const HOSTS = {"site_chatgpt.js": "chatgpt.com", "site_gemini.js": "gemini.google.com"};

function loadAdapters(names, host = null) {
  resetDom();
  // Adapters refuse to run on an unexpected host, so the stub must claim the
  // one being tested.
  const inferred = host || HOSTS[names[0]] || "chatgpt.com";
  globalThis.location.hostname = inferred;
  globalThis.location.href = `https://${inferred}/`;
  delete globalThis.FancyGPTSiteKit;
  delete globalThis.FancyGPTSites;
  for (const name of ["site_kit.js", ...names]) {
    vm.runInThisContext(fs.readFileSync(path.join(COMMON, name), "utf8"), {filename: name});
  }
}

function test(name, fn) {
  currentTest = name;
  try {
    const outcome = fn();
    if (outcome && typeof outcome.then === "function") {
      return outcome.then(
        () => { results.passed += 1; },
        error => { results.failed += 1; results.failures.push(`${name}: ${error && error.message || error}`); },
      );
    }
    results.passed += 1;
  } catch (error) {
    results.failed += 1;
    results.failures.push(`${name}: ${error && error.message || error}`);
  }
  return Promise.resolve();
}

function assert(condition, message) {
  if (!condition) throw new Error(message || "assertion failed");
}

function assertEqual(actual, expected, message) {
  const a = JSON.stringify(actual), b = JSON.stringify(expected);
  if (a !== b) throw new Error(`${message || "not equal"}: got ${a}, expected ${b}`);
}

async function rejects(promise, pattern, message) {
  try { await promise; } catch (error) {
    if (pattern && !pattern.test(String(error.message ?? error))) {
      throw new Error(`${message || "wrong error"}: ${error.message}`);
    }
    return;
  }
  throw new Error(message || "expected a rejection");
}

function report() {
  for (const failure of results.failures) console.error("FAIL " + failure);
  console.log(`${results.passed} passed, ${results.failed} failed`);
  process.exit(results.failed === 0 ? 0 : 1);
}

module.exports = {loadAdapters, test, assert, assertEqual, rejects, report, results};
