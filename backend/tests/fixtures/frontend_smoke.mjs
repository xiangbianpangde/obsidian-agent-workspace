/**
 * Frontend module smoke test.
 *
 * Loads every paper ES module under a minimal DOM shim and asserts the
 * contracts the workbench actually relies on at mount time. Source-text
 * assertions cannot catch a missing method, a class that should extend
 * EventTarget, or a listener attached to the wrong object — all of which only
 * surface when the code runs.
 */
const failures = [];
const checks = [];

function check(name, condition, detail = '') {
  checks.push(name);
  if (!condition) failures.push(`${name}${detail ? ': ' + detail : ''}`);
}

// --- minimal DOM shim -------------------------------------------------------
class FakeClassList {
  constructor() { this._s = new Set(); }
  add(...c) { c.forEach((x) => this._s.add(x)); }
  remove(...c) { c.forEach((x) => this._s.delete(x)); }
  contains(c) { return this._s.has(c); }
  toggle(c) { this._s.has(c) ? this._s.delete(c) : this._s.add(c); }
}

class FakeElement extends EventTarget {
  constructor(tag = 'div') {
    super();
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.style = {};
    this.dataset = {};
    this._attrs = {};
    this.textContent = '';
    this._innerHTML = '';
    this.value = '';
    this.classList = new FakeClassList();
    this.isConnected = true;
  }
  get innerHTML() { return this._innerHTML; }
  set innerHTML(html) { this._innerHTML = String(html); }
  setAttribute(n, v) { this._attrs[n] = String(v); }
  getAttribute(n) { return this._attrs[n] ?? null; }
  removeAttribute(n) { delete this._attrs[n]; }
  appendChild(c) { this.children.push(c); return c; }
  replaceChildren(...c) { this.children = c; }
  replaceWith() {}
  remove() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return null; }
  addEventListener(...a) { super.addEventListener(...a); }
  scrollIntoView() {}
  getBoundingClientRect() { return { left: 0, top: 0, right: 0, bottom: 0, width: 100, height: 100 }; }
  get offsetTop() { return 0; }
  get clientWidth() { return 800; }
  get clientHeight() { return 600; }
  get scrollHeight() { return 1200; }
  get scrollTop() { return 0; }
  set scrollTop(v) {}
}

globalThis.document = {
  createElement: (t) => new FakeElement(t),
  createDocumentFragment: () => new FakeElement('fragment'),
  getElementById: () => new FakeElement(),
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener: () => {},
  removeEventListener: () => {},
  visibilityState: 'visible',
};
globalThis.window = {
  location: { href: 'http://127.0.0.1:8787/', origin: 'http://127.0.0.1:8787', hash: '' },
  addEventListener: () => {},
  removeEventListener: () => {},
  getSelection: () => null,
  isSecureContext: true,
  WorkspaceMarkdown: { render: () => ({ html: '<p>x</p>', error: null }) },
};
globalThis.localStorage = {
  _m: new Map(),
  getItem(k) { return this._m.has(k) ? this._m.get(k) : null; },
  setItem(k, v) { this._m.set(k, String(v)); },
  removeItem(k) { this._m.delete(k); },
};
globalThis.CustomEvent = class CustomEvent extends Event {
  constructor(type, init = {}) { super(type); this.detail = init.detail; }
};
globalThis.requestAnimationFrame = (cb) => setTimeout(cb, 0);
// navigator exists as a read-only getter in Node; define only if absent.

try { if (!globalThis.navigator) throw new Error('absent'); } catch { Object.defineProperty(globalThis, 'navigator', { value: { clipboard: { writeText: async () => {} } }, configurable: true }); }
globalThis.ResizeObserver = class { observe() {} disconnect() {} };

// --- load modules -----------------------------------------------------------
// Resolved from the repository root passed as argv[2] so the script can live
// outside the project (it is an ephemeral probe, not a deliverable).
const root = process.argv[2];
if (!root) throw new Error('usage: node run_frontend_smoke.mjs <repo-root>');
const base = new URL('frontend/dist/paper/', 'file://' + root.replace(/\/*$/, '') + '/');
const modules = {};
for (const name of [
  'api.js', 'layout.js', 'pdf-bridge.js', 'markdown-pane.js',
  'note-pane.js', 'annotations.js', 'ai-context.js',
  'workspace-state.js', 'tab-sync.js',
]) {
  try {
    modules[name] = await import(new URL(name, base).href);
    check(`load ${name}`, true);
  } catch (error) {
    check(`load ${name}`, false, error.message);
  }
}

// Report load failures before anything can throw on a missing export.
if (failures.length) {
  console.log(JSON.stringify({ phase: 'load', total: checks.length, failures }, null, 2));
  process.exit(1);
}

// --- contract: classes the workbench attaches listeners to ------------------
for (const [label, value] of [
  ['NoteEditor', modules['note-pane.js']?.NoteEditor],
  ['AnnotationList', modules['annotations.js']?.AnnotationList],
  ['WorkspaceStateTracker', modules['workspace-state.js']?.WorkspaceStateTracker],
  ['TabCoordinator', modules['tab-sync.js']?.TabCoordinator],
  ['PdfBridge', modules['pdf-bridge.js']?.PdfBridge],
]) {
  check(`${label} is an EventTarget`, typeof value === 'function' && EventTarget.prototype.isPrototypeOf(value.prototype));
}

// --- contract: emit reaches a listener on the instance ----------------------
try {
  const { NoteEditor } = modules['note-pane.js'];
  const editor = new NoteEditor(new FakeElement(), {
    load: async () => ({ exists: false }),
    save: async () => ({ new_hash: 'h' }),
    create: async () => ({ hash: 'h', note_id: 'n' }),
  });
  let seen = null;
  editor.addEventListener('saved', (e) => { seen = e.detail; });
  editor._emit('saved', { hash: 'abc' });
  check('NoteEditor emits on the instance', seen && seen.hash === 'abc');
} catch (error) {
  check('NoteEditor emits on the instance', false, error.message);
}

try {
  const { AnnotationList } = modules['annotations.js'];
  const list = new AnnotationList(new FakeElement(), { list: async () => ({ annotations: [] }) });
  let fired = false;
  list.addEventListener('addrequest', () => { fired = true; });
  list._emit('addrequest', {});
  check('AnnotationList emits on the instance', fired);
} catch (error) {
  check('AnnotationList emits on the instance', false, error.message);
}

// --- contract: methods the workbench calls must exist ----------------------
const { WorkspaceStateTracker } = modules['workspace-state.js'];
const tracker = new WorkspaceStateTracker({ load: async () => ({}), save: async () => ({}) });
for (const method of ['loadFor', 'flush', 'notePdfPosition', 'noteMarkdownPosition', 'resolveRestore', 'dispose', '_reloadVersion']) {
  check(`WorkspaceStateTracker.${method} exists`, typeof tracker[method] === 'function');
}

const { NoteEditor } = modules['note-pane.js'];
const editor2 = new NoteEditor(new FakeElement(), {
  load: async () => ({}), save: async () => ({}), create: async () => ({}),
});
for (const method of ['mount', 'loadFor', 'load', 'flush', 'hasUnsavedWork', 'getText', 'dispose']) {
  check(`NoteEditor.${method} exists`, typeof editor2[method] === 'function');
}

const { MarkdownPane } = modules['markdown-pane.js'];
const pane = new MarkdownPane(new FakeElement());
for (const method of ['setContent', 'scrollToHeading', 'getSelection', 'currentHeadingPath', 'getScrollRatio']) {
  check(`MarkdownPane.${method} exists`, typeof pane[method] === 'function');
}

const { PdfBridge } = modules['pdf-bridge.js'];
const bridge = new PdfBridge(new FakeElement());
for (const method of ['open', 'goToPage', 'getCurrentPage', 'getPageCount', 'getSelection', 'dispose', 'hide', 'show']) {
  check(`PdfBridge.${method} exists`, typeof bridge[method] === 'function');
}

console.log(JSON.stringify({ total: checks.length, failures }));
