// Dependency-free behavioral checks for the browser controller. The lightweight
// DOM supports its actual event handlers; API responses and sockets stay local.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag;
    this.children = [];
    this.dataset = {};
    this.style = {};
    this.attributes = {};
    this.listeners = {};
    this.queries = new Map();
    this.value = '';
    this.hidden = false;
    this.disabled = false;
    this.textContent = '';
    this.className = '';
    this.classList = {
      contains: (name) => this.className.split(' ').includes(name),
      add: (...names) => { this.className = [...new Set([...this.className.split(' '), ...names])].join(' ').trim(); },
      remove: (...names) => { this.className = this.className.split(' ').filter((name) => !names.includes(name)).join(' '); },
      toggle: (name, force) => {
        const add = force === undefined ? !this.classList.contains(name) : force;
        this.classList[add ? 'add' : 'remove'](name);
        return add;
      },
    };
  }
  set innerHTML(value) { this.html = value; this.children = []; this.queries.clear(); }
  get innerHTML() { return this.html || ''; }
  addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); }
  async fire(name) { for (const handler of this.listeners[name] || []) await handler({ target: this, stopPropagation() {}, preventDefault() {} }); }
  click() { return this.fire('click'); }
  focus() { this.focused = true; }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  appendChild(child) { this.children.push(child); child.parent = this; return child; }
  querySelector(selector) {
    if (!this.queries.has(selector)) this.queries.set(selector, new Element(selector));
    return this.queries.get(selector);
  }
  querySelectorAll() { return []; }
  replaceWith(child) {
    if (!this.parent) return;
    this.parent.children[this.parent.children.indexOf(this)] = child;
    child.parent = this.parent;
  }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((child) => child !== this); }
  contains() { return false; }
}

function harness(saved = {}) {
  const elements = new Map();
  const $ = (id) => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const selectors = new Map();
  const modes = ['build', 'chat', 'research', 'analysis'].map((mode) => {
    const item = new Element('button'); item.dataset.mode = mode; return item;
  });
  const modelItems = ['auto', 'gpt-4o', 'claude-3-7-sonnet', 'gemini-2-5-pro', 'local'].map((model) => {
    const item = new Element('button'); item.dataset.model = model; item.className = 'model-list-item'; return item;
  });
  const document = {
    getElementById: $,
    createElement: (tag) => new Element(tag),
    querySelector: (selector) => {
      if (!selectors.has(selector)) selectors.set(selector, new Element());
      return selectors.get(selector);
    },
    querySelectorAll: (selector) => selector === '.mode-dropdown-item' ? modes
      : selector === '.model-list-item' ? modelItems : [],
    addEventListener: (name, handler) => { if (name === 'DOMContentLoaded') handler(); },
    body: new Element('body'),
  };
  const storage = new Map(Object.entries(saved));
  const timers = new Map();
  const sockets = [];
  class WebSocket {
    static OPEN = 1;
    constructor(url) { this.url = url; this.readyState = 1; sockets.push(this); }
    close() { this.closed = true; this.onclose?.(); }
    message(event) { this.onmessage?.({ data: JSON.stringify(event) }); }
  }
  const window = {
    location: { pathname: '/workspace', search: '', href: 'http://localhost/workspace', protocol: 'http:' },
    addEventListener() {},
  };
  const context = vm.createContext({
    document, window, console, URL, URLSearchParams, FormData, WebSocket,
    localStorage: { getItem: (key) => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) },
    setTimeout: (callback) => { const id = Symbol(); timers.set(id, callback); return id; },
    clearTimeout: (id) => timers.delete(id),
    requestAnimationFrame: (callback) => callback(),
    fetch: () => { throw new Error('Unexpected network call'); },
  });
  const source = fs.readFileSync(path.join(__dirname, 'app.js'), 'utf8');
  assert.ok(source.includes('\n  boot();'));
  vm.runInContext(source.replace('\n  boot();', `
    window.controller = {
      api, openConversation, resetConversation, runAgentTask, renderHistoryDrawer,
      loadHistory, loadRecentConversations, restoreConversation, renderConversationRows,
      setSelectedModel, syncSelectedModelUI,
      getState: () => ({ conversationId, currentMode, sessionRevision, runStarting, views: [...activeRunViews] }),
      setUser: (user) => { currentUser = user; },
      key: conversationStorageKey,
    };`), context);
  const controller = window.controller;
  controller.api.get = async () => ({});
  controller.api.post = async () => ({});
  return { ...controller, $, storage, sockets, modes, modelItems, timers };
}

function deferred() { let resolve; let reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
const conversation = (id, extra = {}) => ({
  id, title: `Conversation ${id}`, mode: 'chat', messages: [
    { id: 'u', role: 'user', content: 'First question', created_at: '2026-10-20T10:00:00Z' },
    { id: 'a', role: 'assistant', content: '**First answer**', created_at: '2026-10-20T10:00:01Z', run_id: 'old' },
  ], runs: [{ id: 'old', status: 'done' }], ...extra,
});

test('a fresh workspace opens in Chat mode', () => {
  const h = harness();
  assert.equal(h.getState().currentMode, 'chat');
});

test('opening a completed conversation replaces the transcript, restores mode, and safely renders ordered messages', async () => {
  const h = harness();
  h.$('streamContainer').appendChild(new Element());
  const calls = [];
  h.api.get = async (url) => { calls.push(url); return conversation('saved', {
    mode: 'analysis', title: '<img src=x onerror=alert(1)>', messages: [
      { role: 'assistant', content: '**Answer** <script>alert(1)</script>', created_at: '2026-10-20T10:00:02Z' },
      { role: 'user', content: '<img src=x>', created_at: '2026-10-20T10:00:01Z' },
    ],
  }); };
  await h.openConversation('saved');
  assert.equal(h.getState().conversationId, 'saved');
  assert.equal(h.getState().currentMode, 'analysis');
  assert.equal(h.$('streamContainer').children.length, 2);
  assert.match(h.$('streamContainer').children[0].innerHTML, /&lt;img src=x&gt;/);
  assert.match(h.$('streamContainer').children[1].innerHTML, /<strong>Answer<\/strong> &lt;script&gt;/);
  assert.equal(h.$('conversationContext').hidden, false);
  assert.equal(h.$('conversationTitle').textContent, '<img src=x onerror=alert(1)>');
  assert.equal(h.$('agentInput').placeholder, 'Continue this conversation…');
  assert.equal(h.$('sendBtn').disabled, false);
  assert.equal(h.storage.get(h.key()), 'saved');
  assert.deepEqual(calls, ['/api/conversations/saved']);
  assert.equal(h.sockets.length, 0);
});

test('new runs capture conversation_id before completion and continuation posts to the same conversation', async () => {
  const h = harness();
  const calls = [];
  h.api.post = async (url, body) => { calls.push({ url, body }); return { id: `run-${calls.length}`, conversation_id: 'new-id', status: 'running' }; };
  await h.runAgentTask('Hello', []);
  assert.equal(h.getState().conversationId, 'new-id');
  assert.equal(h.storage.get(h.key()), 'new-id');
  assert.equal(h.$('sendBtn').disabled, true);
  const view = h.getState().views[0];
  view.finalize({ status: 'done', final_output: 'Hello back', conversation_id: 'untrusted-stale-id' });
  assert.equal(h.getState().conversationId, 'new-id');
  assert.equal(h.$('sendBtn').disabled, false);
  await h.runAgentTask('Follow-up', []);
  assert.equal(calls[0].body.conversation_id, undefined);
  assert.equal(calls[1].body.conversation_id, 'new-id');
  assert.ok(calls.every(({ url }) => url === '/api/runs'));
});

test('New task detaches streams and ignores delayed POST responses and terminal events', async () => {
  const h = harness();
  h.api.post = async () => ({ id: 'old-run', conversation_id: 'old-id', status: 'running' });
  await h.runAgentTask('Old task', []);
  const oldView = h.getState().views[0];
  const socket = h.sockets[0];
  h.resetConversation();
  assert.equal(socket.closed, true);
  oldView.finalize({ status: 'done', conversation_id: 'old-id', mode: 'chat', final_output: 'Stale output' });
  socket.message({ type: 'token.delta', data: { text: 'Stale delta' } });
  assert.equal(h.getState().conversationId, null);
  assert.equal(h.$('streamContainer').innerHTML, '');
  const pending = deferred();
  h.api.post = () => pending.promise;
  const posting = h.runAgentTask('Another old task', []);
  h.resetConversation();
  pending.resolve({ id: 'delayed', conversation_id: 'delayed-id', status: 'running' });
  await posting;
  assert.equal(h.getState().conversationId, null);
  assert.equal(h.getState().views.length, 0);
  assert.equal(h.storage.has(h.key()), false);
  assert.equal(h.$('sendBtn').disabled, false);
});

test('racing conversation opens and New task cannot be overwritten by a slow detail response', async () => {
  const h = harness();
  const slow = deferred();
  h.api.get = (url) => url.endsWith('/slow') ? slow.promise : Promise.resolve(conversation('latest'));
  const opening = h.openConversation('slow');
  assert.equal(h.$('sendBtn').disabled, true);
  await h.openConversation('latest');
  slow.resolve(conversation('slow'));
  await opening;
  assert.equal(h.getState().conversationId, 'latest');
  const second = deferred();
  h.api.get = () => second.promise;
  const reopening = h.openConversation('slow-again');
  h.resetConversation();
  second.resolve(conversation('slow-again'));
  await reopening;
  assert.equal(h.getState().conversationId, null);
  assert.equal(h.$('streamContainer').innerHTML, '');
});

test('opening an unfinished run attaches its stream and preserves the run tree', async () => {
  const h = harness();
  h.api.get = async (url) => url.includes('/conversations/') ? conversation('active', { runs: [{ id: 'live-run', status: 'running' }] }) : {};
  await h.openConversation('active');
  assert.equal(h.sockets.length, 1);
  assert.match(h.sockets[0].url, /\/api\/runs\/live-run\/stream/);
  const view = h.getState().views[0];
  h.sockets[0].message({ type: 'agent.started', call_id: 'planner', data: { role: 'planner' }, seq: 1 });
  h.sockets[0].message({ type: 'token.delta', data: { text: 'Working…' }, seq: 2 });
  assert.equal(view.nodes.size, 1);
  assert.equal(view.streamEl.textContent, 'Working…');
  assert.equal(view.treeBox.style.display, '');
  assert.equal(h.$('sendBtn').disabled, true);
  h.resetConversation();
  assert.equal(h.sockets[0].closed, true);
});

test('conversation history searches, filters, paginates, sorts, escapes, and ignores stale results', async () => {
  const h = harness();
  const calls = [];
  const item = (id, updated_at) => ({ id, title: `<b>${id}</b>`, mode: 'chat', message_count: 2, updated_at });
  h.api.get = async (url) => { calls.push(url); return { items: [item('older', '2026-10-01'), item('newer', '2026-10-21')], next_cursor: 'page/2' }; };
  await h.renderHistoryDrawer();
  const rows = h.$('historyList').children.filter((element) => element.dataset.conversationId);
  assert.deepEqual(rows.map((row) => row.dataset.conversationId), ['newer', 'older']);
  assert.match(rows[0].innerHTML, /&lt;b&gt;newer&lt;\/b&gt;/);
  assert.equal(h.$('historyMore').hidden, false);
  h.$('historyQuery').value = 'design & build';
  h.$('historyMode').value = 'chat';
  await h.loadHistory(true);
  const params = new URL(calls.at(-1), 'http://localhost').searchParams;
  assert.equal(params.get('limit'), '50');
  assert.equal(params.get('q'), 'design & build');
  assert.equal(params.get('mode'), 'chat');
  assert.equal(params.get('cursor'), 'page/2');
  const stale = deferred();
  h.api.get = () => stale.promise;
  const oldSearch = h.loadHistory(false);
  h.api.get = async () => ({ items: [item('current', '2026-10-22')], next_cursor: null });
  await h.loadHistory(false);
  stale.resolve({ items: [item('stale', '2026-10-22')], next_cursor: null });
  await oldSearch;
  assert.equal(h.$('historyList').children.find((row) => row.dataset.conversationId)?.dataset.conversationId, 'current');
  assert.ok(calls.every((url) => url.startsWith('/api/conversations?')));
});

test('boot restoration is account/workspace scoped, skips user navigation, and clears missing conversations', async () => {
  const h = harness({ 'jevora_active_conversation:user-1:/workspace': 'persisted' });
  h.setUser({ id: 'user-2' });
  let requests = 0;
  h.api.get = async () => { requests++; return conversation('persisted'); };
  await h.restoreConversation(0);
  assert.equal(requests, 0);
  h.setUser({ id: 'user-1' });
  await h.restoreConversation(0);
  assert.equal(h.getState().conversationId, 'persisted');
  h.resetConversation();
  h.storage.set(h.key(), 'persisted');
  await h.restoreConversation(0);
  assert.equal(h.getState().conversationId, null);
  h.api.get = async () => { const error = new Error('Not found'); error.status = 404; throw error; };
  await h.restoreConversation(h.getState().sessionRevision);
  assert.equal(h.storage.has(h.key()), false);
  assert.match(h.$('streamContainer').innerHTML, /Could not restore your conversation/);
  assert.equal(h.$('sendBtn').disabled, false);
});

test('a terminal fetch that resolves after switching conversations cannot finalize the new view', async () => {
  const h = harness();
  const final = deferred();
  h.api.post = async () => ({ id: 'running', conversation_id: 'old', status: 'running' });
  await h.runAgentTask('Task', []);
  h.api.get = () => final.promise;
  h.sockets[0].message({ type: 'run.status', data: { status: 'done' } });
  h.resetConversation();
  final.resolve({ status: 'done', mode: 'chat', conversation_id: 'old', final_output: 'Late finish' });
  await flush();
  assert.equal(h.getState().conversationId, null);
  assert.equal(h.$('streamContainer').innerHTML, '');
  assert.equal(h.$('sendBtn').disabled, false);
});


test('a terminal stream event releases the composer when final reconciliation fails', async () => {
  const h = harness();
  h.api.post = async () => ({ id: 'run-1', conversation_id: 'conversation-1', status: 'running' });
  await h.runAgentTask('Task', []);
  const view = h.getState().views[0];
  h.api.get = async (url) => {
    if (url.includes('/api/runs/run-1')) throw new Error('temporary reconciliation failure');
    if (url === '/api/agents/status') return { agents_running: 0, subagents_running: 0 };
    return {};
  };

  h.sockets[0].message({ type: 'run.status', data: { status: 'done' } });
  await flush();

  assert.equal(view.finished, true);
  assert.equal(h.$('sendBtn').disabled, false);
  assert.notEqual(h.$('agentInput').placeholder, 'The agent is working…');
});

test('a first message takes priority over boot restoration while its POST is pending', async () => {
  const h = harness({ 'jevora_active_conversation:local:/workspace': 'previous' });
  const pending = deferred();
  h.api.post = () => pending.promise;
  let restored = false;
  h.api.get = async () => { restored = true; return conversation('previous'); };
  const posting = h.runAgentTask('New first message', []);
  await h.restoreConversation(0);
  assert.equal(restored, false);
  pending.resolve({ id: 'first', conversation_id: 'new', status: 'running' });
  await posting;
  assert.equal(h.getState().conversationId, 'new');
  assert.equal(h.storage.get(h.key()), 'new');
});

test('selecting a provider model clears Auto selection and highlights the provider model', () => {
  const h = harness();
  const auto = h.modelItems[0];
  auto.className = 'model-list-item active';
  h.setSelectedModel('provider-1:step-5-preview', 'step-5-preview');
  assert.equal(auto.classList.contains('active'), false);
});
