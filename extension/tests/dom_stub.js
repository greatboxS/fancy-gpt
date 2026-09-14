/* A minimal DOM, enough to run the real site adapters in Node.
 *
 * The adapters are the foundation of the whole system and they are the part
 * that runs where we cannot see it, so they are tested by executing them rather
 * than by asserting that their source contains certain strings. This stub
 * implements only what they actually touch, and deliberately reproduces the two
 * behaviours that matter: querySelectorAll returning several matches (which the
 * adapters treat as ambiguous), and mutations notifying observers.
 */

class StubElement {
  constructor(tag, attrs = {}) {
    this.tagName = tag.toUpperCase();
    // HTML attribute names are case-insensitive, so they are normalised here
    // rather than letting a test pass only because it happened to match the
    // casing a selector was written in.
    this.attributes = {};
    for (const [name, value] of Object.entries(attrs)) this.attributes[name.toLowerCase()] = value;
    this.children = [];
    this.parent = null;
    this._text = attrs.text ?? "";
    this.clicks = 0;
    this.focused = false;
    this.hidden = false;
    this.value = attrs.value ?? undefined;
    this.isContentEditable = this.attributes.contenteditable === "true";
  }

  get innerText() {
    if (this.children.length === 0) return this._text;
    return [this._text, ...this.children.map(child => child.innerText)].filter(Boolean).join("\n");
  }
  set innerText(value) { this._text = value; this._notify(); }

  // textContent is NOT innerText. It concatenates the raw text nodes with no
  // regard for layout, so every line break that only existed because two block
  // elements render on separate lines is absent. Reproducing that here is the
  // point: an adapter that reads textContent and expects innerText's line
  // structure silently destroys the fenced-block protocol, and a stub that
  // aliased the two hid exactly that failure.
  get textContent() {
    return [this._text, ...this.children.map(child => child.textContent)].join("");
  }
  set textContent(value) { this.innerText = value; }

  // Real adapters walk text nodes, not just elements.
  get childNodes() {
    const own = this._text ? [{nodeType: 3, nodeValue: this._text, textContent: this._text}] : [];
    return [...own, ...this.children];
  }

  setText(value) { this._text = value; this._notify(); }

  getAttribute(name) { return this.attributes[String(name).toLowerCase()] ?? null; }
  setAttribute(name, value) { this.attributes[String(name).toLowerCase()] = value; this._notify(); }

  append(child) { child.parent = this; this.children.push(child); this._notify(); }
  remove() {
    if (!this.parent) return;
    this.parent.children = this.parent.children.filter(item => item !== this);
    const root = this.parent; this.parent = null; root._notify();
  }

  _notify() { (this.ownerDocument ?? document)?._notifyObservers?.(); }

  get ownerDocument() { let node = this; while (node.parent) node = node.parent; return node._document ?? null; }

  matches(selector) { return matchesSelector(this, selector); }

  closest(selector) {
    let node = this;
    while (node) { if (node.matches?.(selector)) return node; node = node.parent; }
    return null;
  }

  _descendants() {
    const out = [];
    for (const child of this.children) { out.push(child); out.push(...child._descendants()); }
    return out;
  }

  querySelectorAll(selector) { return this._descendants().filter(node => matchesSelector(node, selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }

  click() { this.clicks += 1; if (this.onclick) this.onclick(); this._notify(); }
  focus() { this.focused = true; }
  dispatchEvent() { return true; }
  getBoundingClientRect() {
    return this.hidden ? {width: 0, height: 0} : {width: 100, height: 20};
  }
  getClientRects() { return this.hidden ? [] : [this.getBoundingClientRect()]; }
}

/* Supports the selector shapes the adapters actually use: tag, #id, .class,
 * [attr], [attr="value"], [attr*="value"], and a tag with one of those. */
function matchesSelector(node, selector) {
  for (const part of selector.split(",").map(item => item.trim())) {
    if (matchesSingle(node, part)) return true;
  }
  return false;
}

function matchesSingle(node, selector) {
  let rest = selector.trim();
  if (!rest) return false;
  const tagMatch = rest.match(/^([a-zA-Z][\w-]*)/);
  if (tagMatch) {
    if (node.tagName !== tagMatch[1].toUpperCase()) return false;
    rest = rest.slice(tagMatch[1].length);
  }
  while (rest.length) {
    let matched = false;
    const id = rest.match(/^#([\w-]+)/);
    if (id) { if (node.attributes.id !== id[1]) return false; rest = rest.slice(id[0].length); matched = true; }
    const cls = rest.match(/^\.([\w-]+)/);
    if (cls) {
      const classes = String(node.attributes.class ?? "").split(/\s+/);
      if (!classes.includes(cls[1])) return false;
      rest = rest.slice(cls[0].length); matched = true;
    }
    const attr = rest.match(/^\[([\w-]+)(?:([*^$]?=)"([^"]*)")?\]/);
    if (attr) {
      const [, name, operator, value] = attr;
      const actual = node.attributes[name.toLowerCase()];
      if (actual === undefined || actual === null) return false;
      if (operator === "=" && actual !== value) return false;
      if (operator === "*=" && !String(actual).includes(value)) return false;
      rest = rest.slice(attr[0].length); matched = true;
    }
    if (!matched) return false;
  }
  return true;
}

class StubDocument extends StubElement {
  constructor() {
    super("document");
    this._document = this;
    this.observers = [];
    this._listeners = {};
    this.body = new StubElement("body");
    this.body.parent = this;
    this.children.push(this.body);
  }
  _notifyObservers() { for (const observer of [...this.observers]) observer._fire(); }
  createRange() { return {selectNodeContents() {}}; }

  /* Page visibility, because it decides whether a reply renders at all.
   *
   * A hidden document stops being painted, so a site that streams its reply
   * into the DOM freezes it part-written. An adapter has to be able to see
   * that, and a stub without it cannot test the difference. */
  get visibilityState() { return this.hidden ? "hidden" : "visible"; }
  hasFocus() { return !this.hidden; }
  addEventListener(type, handler) { (this._listeners[type] ??= []).push(handler); }
  removeEventListener(type, handler) {
    this._listeners[type] = (this._listeners[type] ?? []).filter(item => item !== handler);
  }
  setVisibility(state) {
    this.hidden = state === "hidden";
    for (const handler of [...(this._listeners.visibilitychange ?? [])]) handler();
  }
}

class StubMutationObserver {
  constructor(callback) { this.callback = callback; this.connected = false; }
  observe() { this.connected = true; document.observers.push(this); }
  disconnect() {
    this.connected = false;
    document.observers = document.observers.filter(item => item !== this);
  }
  _fire() { if (this.connected) this.callback([], this); }
}

/* setComposer goes through the real editing path, so the stub provides the
 * same surface a contenteditable and a textarea expose. */
function installEditingApis() {
  globalThis.Event = class { constructor(type, init = {}) { this.type = type; Object.assign(this, init); } };
  globalThis.InputEvent = class extends globalThis.Event {};
  globalThis.HTMLTextAreaElement = {
    prototype: {},
  };
  Object.defineProperty(globalThis.HTMLTextAreaElement.prototype, "value", {
    configurable: true,
    set(value) { this._text = value; this.value_ = value; this._notify?.(); },
    get() { return this.value_; },
  });
  globalThis.document.execCommand = (command, _ui, value) => {
    if (command !== "insertText") return false;
    const target = globalThis.document._composerTarget;
    if (!target) return false;
    target.setText(value);
    return true;
  };
}

function resetDom() {
  globalThis.document = new StubDocument();
  globalThis.MutationObserver = StubMutationObserver;
  globalThis.getComputedStyle = element => ({
    visibility: element.hidden ? "hidden" : "visible",
    display: element.hidden ? "none" : "block",
  });
  globalThis.window = {getSelection: () => ({removeAllRanges() {}, addRange() {}})};
  globalThis.location = {pathname: "/", hash: "", href: "https://chatgpt.com/", hostname: "chatgpt.com"};
  globalThis.navigator = {onLine: true};
  installEditingApis();
  return globalThis.document;
}

module.exports = {StubElement, StubDocument, StubMutationObserver, resetDom, matchesSelector};
