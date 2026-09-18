"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

function event() {
  const listeners = new Set();
  return {
    addListener(fn) { listeners.add(fn); },
    removeListener(fn) { listeners.delete(fn); },
    emit(...args) { for (const fn of [...listeners]) fn(...args); },
    get size() { return listeners.size; },
  };
}

async function main() {
  const waitUntil = async predicate => {
    for (let i = 0; i < 20 && !predicate(); ++i) await new Promise(resolve => setImmediate(resolve));
  };
  let nextWindow = 10;
  let nextTab = 100;
  let createdWindows = 0;
  const windows = new Map();
  const pending = new Map();
  const sent = [];
  const stored = {};
  const sessionStored = {};
  const removed = event();
  let handler;
  let reloads = 0;
  let connects = 0;
  let autoSubmit = true;

  const browser = {
    windows: {
      async create(options) {
        createdWindows += 1;
        const value = {id: nextWindow++, tabs: [{id: nextTab++}], options};
        windows.set(value.id, value);
        return value;
      },
      async remove(id) { windows.delete(id); },
      async get(id) {
        const value = windows.get(id);
        if (!value) throw new Error("window not found");
        return value;
      },
      async update(id, options) {
        const value = windows.get(id);
        if (!value) throw new Error("window not found");
        value.options = {...value.options, ...options};
        return value;
      },
    },
    tabs: {
      onRemoved: removed,
      async get(id) {
        for (const value of windows.values()) {
          const tab = value.tabs?.find(item => item.id === id);
          if (tab) return tab;
        }
        throw new Error("tab not found");
      },
      async update(id, options) {
        const tab = await this.get(id);
        Object.assign(tab, options);
        return tab;
      },
      async sendMessage(tabId, message) {
        if (message.type === "fancy_cancel_turn") return {ok: true};
        if (message.type === "fancy_execute_turn" && autoSubmit) {
          setImmediate(() => browser.runtime.onMessage.emit(
            {type: "fancy_turn_submitted", jobId: message.jobId, leaseId: message.leaseId,
             generationEpoch: message.generationEpoch}, {tab: {id: tabId}}, () => {},
          ));
        }
        return new Promise((resolve, reject) => pending.set(tabId, {resolve, reject, message}));
      },
    },
    storage: {local: {
      async get(defaults) {
        await new Promise(resolve => setImmediate(resolve));
        if (typeof defaults === "string") return {[defaults]: stored[defaults]};
        return {...defaults, ...stored};
      },
      async set(values) {
        await new Promise(resolve => setImmediate(resolve));
        Object.assign(stored, values);
      },
    }, session: {
      async get(key) { return {[key]: sessionStored[key]}; },
      async set(values) { Object.assign(sessionStored, values); },
    }},
    runtime: {
      onConnect: event(), onInstalled: event(), onStartup: event(), onMessage: event(),
      reload() { reloads += 1; },
    },
    alarms: {onAlarm: event(), create() {}},
  };
  const transport = {
    setHandler(fn) { handler = fn; },
    async connect() { connects += 1; }, disconnect() {}, status() { return {connected: false}; },
    send(message) { sent.push(message); },
  };
  windows.set(9, {id: 9, tabs: [{id: 99}]});
  sessionStored.fancyGptSurfaceLeases = [
    {leaseId: 999, jobId: "orphan", epoch: 1, windowId: 9, tabId: 99},
  ];
  const context = {
    browser, console, setTimeout, clearTimeout, setInterval, clearInterval,
    __FANCYGPT_TEST__: true, FancyGPTTransport: transport,
  };
  context.globalThis = context;
  const source = fs.readFileSync(path.join(__dirname, "../common/background.js"), "utf8");
  vm.runInNewContext(source, context, {filename: "background.js"});

  await waitUntil(() => connects === 1);
  browser.alarms.onAlarm.emit({name: "fancy-gpt-bridge-reconnect"});
  await waitUntil(() => connects === 2);
  assert.strictEqual(connects, 2, "a browser alarm wakes a disconnected MV3 worker and reconnects it");

  await waitUntil(() => !windows.has(9));
  assert(!windows.has(9), "a restarted worker quarantines its recorded orphan window");
  assert.strictEqual(sessionStored.fancyGptSurfaceLeases.length, 0);

  autoSubmit = false;
  const first = handler({type: "job", operation: "model.turn", site: "chatgpt", job_id: "a", prompt: "A", generation_epoch: 1});
  const second = handler({type: "job", operation: "model.turn", site: "chatgpt", job_id: "b", prompt: "B", generation_epoch: 1});
  await waitUntil(() => windows.size === 1);
  const firstEntry = context.FancyGPTBackgroundTest.activeJobs.get("a");
  browser.runtime.onMessage.emit(
    {type: "fancy_turn_submitted", jobId: "a", leaseId: firstEntry.leaseId + 99, generationEpoch: 1},
    {tab: {id: firstEntry.tabId}}, () => {},
  );
  await new Promise(resolve => setImmediate(resolve));
  assert.strictEqual(windows.size, 1, "a wrong lease acknowledgement must not release focus");
  browser.runtime.onMessage.emit(
    {type: "fancy_turn_submitted", jobId: "a", leaseId: firstEntry.leaseId, generationEpoch: 1},
    {tab: {id: firstEntry.tabId}}, () => {},
  );
  autoSubmit = true;
  await waitUntil(() => windows.size === 2);

  assert.strictEqual(windows.size, 2, "concurrent turns must own separate windows");
  assert.strictEqual(context.FancyGPTBackgroundTest.surfaceLeases.size, 2);
  assert.strictEqual(sessionStored.fancyGptSurfaceLeases.length, 2, "live lease ownership is persisted");
  const entries = [...context.FancyGPTBackgroundTest.activeJobs.values()];
  assert.notStrictEqual(entries[0].tabId, entries[1].tabId, "tabs must not be shared");
  assert.strictEqual(removed.size, 2, "each in-flight send owns one close watcher");

  const secondTab = context.FancyGPTBackgroundTest.activeJobs.get("b").tabId;
  pending.get(secondTab).resolve({ok: true, text: "B", responseIdentity: "b1"});
  await second;
  assert.strictEqual(windows.size, 1, "out-of-order completion closes only its lease");
  assert.strictEqual(removed.size, 1, "winning send removes its close watcher");

  const firstTab = context.FancyGPTBackgroundTest.activeJobs.get("a").tabId;
  pending.get(firstTab).resolve({ok: true, text: "A", responseIdentity: "a1"});
  await first;
  assert.strictEqual(windows.size, 0);
  assert.strictEqual(sessionStored.fancyGptSurfaceLeases.length, 0, "released leases leave no recovery record");
  assert.strictEqual(removed.size, 0);
  assert.deepStrictEqual(sent.filter(item => item.type === "job_result").map(item => item.job_id).sort(), ["a", "b"]);

  // A persisted provider conversation keeps its rendered surface between
  // tool-loop turns. The next continuation gets a new lease/fence but reuses
  // the same browser window and tab, so long reviews do not flicker windows.
  const createdBeforePersistent = createdWindows;
  const persistent = handler({
    type: "job", operation: "model.turn", site: "chatgpt", job_id: "persist-1", prompt: "first",
    generation_epoch: 11, conversation: {mode: "persistent"},
  });
  await waitUntil(() => context.FancyGPTBackgroundTest.activeJobs.get("persist-1")?.tabId != null);
  const persistentEntry = context.FancyGPTBackgroundTest.activeJobs.get("persist-1");
  const persistentTab = persistentEntry.tabId;
  const persistentWindow = [...windows.values()].find(value => value.tabs?.some(tab => tab.id === persistentTab))?.id;
  pending.get(persistentTab).resolve({
    ok: true, text: "tool please", responseIdentity: "persist-a1", conversationId: "conv-keep-0001",
  });
  await persistent;
  assert.strictEqual(windows.size, 1, "persistent turn keeps one idle task window");
  assert.strictEqual(context.FancyGPTBackgroundTest.idleSurfaces.size, 1);

  const continued = handler({
    type: "job", operation: "model.turn", site: "chatgpt", job_id: "persist-2", prompt: "tool result",
    generation_epoch: 12, conversation: {mode: "continue", conversation_id: "conv-keep-0001"},
  });
  await waitUntil(() => context.FancyGPTBackgroundTest.activeJobs.get("persist-2")?.tabId != null);
  const continuedEntry = context.FancyGPTBackgroundTest.activeJobs.get("persist-2");
  assert.strictEqual(continuedEntry.tabId, persistentTab, "continuation reuses the same tab");
  assert.strictEqual(createdWindows, createdBeforePersistent + 1, "continuation must not create another window");
  const continuedWindow = [...windows.values()].find(value => value.tabs?.some(tab => tab.id === continuedEntry.tabId))?.id;
  assert.strictEqual(continuedWindow, persistentWindow, "continuation reuses the same window");
  pending.get(continuedEntry.tabId).resolve({
    ok: true, text: "final", responseIdentity: "persist-a2", conversationId: "conv-keep-0001",
  });
  await continued;
  assert.strictEqual(windows.size, 1, "surface remains idle for more conversation turns");
  await context.FancyGPTBackgroundTest.evictIdleSurface("chatgpt:conv-keep-0001");
  assert.strictEqual(windows.size, 0, "explicit idle cleanup closes the cached surface");

  // Cancellation while waiting for the focus arbiter must not allocate a
  // browser surface, much less submit a prompt after the caller has gone.
  autoSubmit = false;
  const blocker = handler({
    type: "job", operation: "model.turn", site: "chatgpt", job_id: "focus-owner", prompt: "owner", generation_epoch: 7,
  });
  const focusWaiter = handler({
    type: "job", operation: "model.turn", site: "chatgpt", job_id: "focus-waiter", prompt: "waiter", generation_epoch: 7,
  });
  await waitUntil(() => context.FancyGPTBackgroundTest.activeJobs.get("focus-owner")?.tabId != null);
  const ownerEntry = context.FancyGPTBackgroundTest.activeJobs.get("focus-owner");
  const windowsBeforeCancel = createdWindows;
  await handler({type: "cancel", job_id: "focus-waiter", generation_epoch: 7, control_id: "cancel-focus"});
  browser.runtime.onMessage.emit(
    {type: "fancy_turn_submitted", jobId: "focus-owner", leaseId: ownerEntry.leaseId, generationEpoch: 7},
    {tab: {id: ownerEntry.tabId}}, () => {},
  );
  await focusWaiter;
  assert.strictEqual(createdWindows, windowsBeforeCancel, "a cancelled focus waiter must not allocate a window");
  assert(sent.some(item => item.type === "job_cancelled" && item.job_id === "focus-waiter"));
  pending.get(ownerEntry.tabId).resolve({ok: true, text: "owner", responseIdentity: "owner1"});
  await blocker;
  assert.strictEqual(windows.size, 0);
  autoSubmit = true;

  const jobs = ["c", "d", "e", "f"].map(job_id => handler({
    type: "job", operation: "model.turn", site: "chatgpt", job_id, prompt: job_id, generation_epoch: 1,
  }));
  await waitUntil(() => windows.size === 4);
  await handler({type: "job", operation: "model.turn", site: "chatgpt", job_id: "overflow", prompt: "x"});
  assert.strictEqual(windows.size, 4, "configured capacity must bound rendered surfaces");
  assert(sent.some(item => item.type === "job_error" && item.job_id === "overflow" && /capacity/.test(item.error)));
  await handler({type: "extension.reload", control_id: "busy-reload"});
  assert(sent.some(item => item.type === "reload_result" && item.control_id === "busy-reload" && !item.accepted));
  await new Promise(resolve => setTimeout(resolve, 120));
  assert.strictEqual(reloads, 0, "an active turn must fence extension reload");
  for (const jobId of ["c", "d", "e", "f"]) {
    const tabId = context.FancyGPTBackgroundTest.activeJobs.get(jobId).tabId;
    pending.get(tabId).resolve({ok: true, text: jobId, responseIdentity: `${jobId}1`});
  }
  await Promise.all(jobs);
  assert.strictEqual(windows.size, 0);

  const closedJob = handler({
    type: "job", operation: "model.turn", site: "chatgpt", job_id: "closed", prompt: "x", generation_epoch: 1,
  });
  await waitUntil(() => context.FancyGPTBackgroundTest.activeJobs.get("closed")?.tabId != null);
  const closedTab = context.FancyGPTBackgroundTest.activeJobs.get("closed").tabId;
  removed.emit(closedTab);
  await closedJob;
  assert.strictEqual(windows.size, 0, "external tab close releases its exact window lease");
  assert.strictEqual(removed.size, 0, "external close removes the watcher too");
  assert(sent.some(item => item.type === "job_error" && item.job_id === "closed" && /closed/.test(item.error)));

  await Promise.all(Array.from({length: 12}, (_, i) =>
    context.FancyGPTBackgroundTest.rememberObservation("test", {sequence: i})
  ));
  const observations = await context.FancyGPTBackgroundTest.takeObservations();
  assert.strictEqual(JSON.stringify(observations.map(item => item.sequence)), JSON.stringify(Array.from({length: 12}, (_, i) => i)));
  assert.strictEqual((await context.FancyGPTBackgroundTest.takeObservations()).length, 0);
  await handler({type: "extension.reload", control_id: "reload-1"});
  assert(sent.some(item => item.type === "reload_result" && item.control_id === "reload-1" && item.accepted));
  await new Promise(resolve => setTimeout(resolve, 120));
  assert.strictEqual(reloads, 1, "reload happens only after its acknowledgement is sent");
  console.log("background lifecycle: 1 passed");
}

main().catch(error => { console.error(error); process.exitCode = 1; });
