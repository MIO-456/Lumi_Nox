'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

class FakeNode {
  constructor(tagName = 'div') {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.className = '';
    this.hidden = false;
    this.disabled = false;
    this.value = '';
    this.dataset = {};
    this.attributes = new Map();
    this.listeners = new Map();
    this.style = {setProperty(name, value) { this[name] = value; }};
    this.classList = {
      contains: name => this.className.split(/\s+/).includes(name),
      add: (...names) => {
        this.className = [...new Set([...this.className.split(/\s+/).filter(Boolean), ...names])].join(' ');
      },
      remove: (...names) => {
        this.className = this.className.split(/\s+/).filter(name => name && !names.includes(name)).join(' ');
      },
      toggle: (name, force) => {
        const enabled = force === undefined ? !this.classList.contains(name) : !!force;
        this.classList[enabled ? 'add' : 'remove'](name);
        return enabled;
      },
    };
    this.scrollHeight = 0;
    this.scrollTop = 0;
    this.clientHeight = 0;
    this._text = '';
    if (this.tagName === 'CANVAS') {
      this.draws = [];
      this.context = {};
      for (const method of ['clearRect', 'fillRect', 'beginPath', 'moveTo', 'lineTo', 'stroke', 'arc', 'fill', 'save', 'restore']) {
        this.context[method] = (...args) => this.draws.push({method, args});
      }
      this.context.createLinearGradient = () => ({addColorStop() {}});
    }
  }

  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(value) { this._text = ''; this.children = []; this._html = value; }
  get innerHTML() { return this._html || ''; }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  get options() { return this.children; }
  append(...nodes) {
    this.children.push(...nodes.map(node => {
      if (typeof node !== 'string') return node;
      const child = new FakeNode('#text'); child.textContent = node; return child;
    }));
  }
  replaceChildren(...nodes) { this._text = ''; this.children = []; this.append(...nodes); }
  add(node) { this.append(node); }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  addEventListener(name, listener) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(listener);
  }
  click() {
    this.clicked = true;
    for (const listener of this.listeners.get('click') || []) listener({target: this});
  }
  querySelectorAll(selector) {
    const matches = node => selector.startsWith('.')
      ? node.classList.contains(selector.slice(1))
      : node.tagName === selector.toUpperCase();
    return this.children.flatMap(child => [...(matches(child) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  getContext(kind) { assert.equal(kind, '2d'); return this.context; }
}

function environment() {
  const ids = new Map();
  const created = [];
  const blobs = [];
  const revoked = [];
  const document = {
    createElement(tag) { const node = new FakeNode(tag); created.push(node); return node; },
    createTextNode(value) { const node = new FakeNode('#text'); node.textContent = value; return node; },
    getElementById(id) {
      if (!ids.has(id)) ids.set(id, new FakeNode());
      return ids.get(id);
    },
    querySelectorAll(selector) {
      return [...ids.values()].flatMap(node => node.querySelectorAll(selector));
    },
  };
  class FakeURL extends URL {
    static createObjectURL(blob) { blobs.push(blob); return `blob:test/${blobs.length}`; }
    static revokeObjectURL(url) { revoked.push(url); }
  }
  const window = {setTimeout() { return 1; }, clearTimeout() {}};
  const context = vm.createContext({
    document, window, Blob, URL: FakeURL,
    Option: function (label, value) {
      const node = new FakeNode('option'); node.textContent = label; node.value = value; return node;
    },
    location: {origin: 'http://127.0.0.1'},
    fetch() { throw new Error('Tests must not access the network'); },
  });
  return {context, document, window, created, blobs, revoked};
}

function source(name) {
  return fs.readFileSync(path.join(__dirname, '..', 'public_host', 'web', name), 'utf8');
}

function room() {
  const env = environment();
  vm.runInContext(source('room.js'), env.context, {filename: 'room.js'});
  const container = new FakeNode();
  const view = env.window.RoomView.init(container);
  return {...env, container, view};
}

const roles = [{name: '示例A'}, {name: '示例B'}];
function playing(overrides = {}) {
  return {
    run_id: 'test-run', mode: 'demo', running: true, roles,
    current_request: {request_id: 'request-1', speaker: '示例A', phase: 'playing', text: '离线测试字幕'},
    ...overrides,
  };
}

function assertSilent(container) {
  assert.equal(container.querySelector('.subtitle-box').hidden, true, 'subtitle must be hidden');
  assert.equal(container.querySelectorAll('.speaking').length, 0, 'no character should be speaking');
  assert.equal(container.querySelectorAll('.talking').length, 0, 'no avatar should animate speech');
  assert.equal(container.querySelectorAll('.queued').length, 0, 'no cancelled character should remain queued');
}

test('RoomView initializes and renders its initial status without throwing', () => {
  let stage;
  assert.doesNotThrow(() => {
    stage = room();
    stage.view.applyStatus({roles, mode: 'demo', current_request: null, program: null});
  });
  assert.equal(stage.container.querySelectorAll('.character-card').length, 2);
  assert.equal(stage.container.querySelectorAll('.character-level').length, 2);
  assertSilent(stage.container);
});

test('playing requests show subtitles and activate only their speaker', () => {
  const {view, container} = room();
  const cards = container.querySelectorAll('.character-card');
  view.applyStatus(playing());
  assert.equal(container.querySelector('.subtitle-box').hidden, false);
  assert.equal(container.querySelector('.subtitle-speaker').textContent, '示例A');
  assert.equal(container.querySelector('.subtitle-text').textContent, '离线测试字幕');
  assert.equal(cards[0].classList.contains('speaking'), true);
  assert.equal(cards[0].querySelector('.avatar').classList.contains('talking'), true);
  assert.equal(cards[1].classList.contains('speaking'), false);

  view.applyStatus(playing({current_request: {
    request_id: 'request-2', speaker: '示例B', phase: 'playing', text: '第二位角色接话',
  }}));
  assert.equal(container.querySelector('.subtitle-speaker').textContent, '示例B');
  assert.equal(container.querySelector('.subtitle-text').textContent, '第二位角色接话');
  assert.equal(cards[0].classList.contains('speaking'), false);
  assert.equal(cards[0].querySelector('.avatar').classList.contains('talking'), false);
  assert.equal(cards[1].classList.contains('speaking'), true);
  assert.equal(container.querySelectorAll('.speaking').length, 1);
});

test('cancellation, stop and disconnect clear visible speech', async t => {
  const transitions = {
    cancelling: view => view.applyStatus(playing({current_request: {...playing().current_request, phase: 'cancelling'}})),
    cancelled: view => view.applyStatus(playing({current_request: {...playing().current_request, phase: 'cancelled'}})),
    failed: view => view.applyStatus(playing({current_request: {...playing().current_request, phase: 'failed'}})),
    stopping: view => view.applyStatus(playing({stopping: true})),
    stopped: view => view.applyStatus({roles, running: false, current_request: null}),
    'program stopping': view => view.applyStatus(playing({program: {phase: 'stopping', running: true}})),
    'disconnected status': view => view.applyStatus(playing({__connected: false})),
    'connection lost': view => view.setConnection(false),
  };
  for (const [name, transition] of Object.entries(transitions)) {
    await t.test(name, () => {
      const {view, container} = room();
      view.applyStatus(playing());
      assert.equal(container.querySelector('.subtitle-box').hidden, false);
      transition(view);
      assertSilent(container);
    });
  }
});

test('one role renders a single visible character and can speak', () => {
  const {view, container} = room();
  const solo = {name: '单角色'};
  view.applyStatus(playing({roles: [solo], current_request: {
    request_id: 'solo-1', speaker: solo.name, phase: 'playing', text: '一个角色也能发言',
  }}));
  const visible = container.querySelectorAll('.character-card').filter(card => !card.hidden);
  assert.equal(visible.length, 1);
  assert.equal(visible[0].querySelector('.character-name').textContent, solo.name);
  assert.equal(visible[0].classList.contains('speaking'), true);
  assert.equal(container.querySelector('.subtitle-speaker').textContent, solo.name);
  assert.equal(container.querySelector('.subtitle-box').hidden, false);
});

function activityStatus(state, phase = 'activity') {
  return {roles, running: true, program: {running: true, phase, activity: {running: true, phase: 'active', state}}};
}

function boardState(board, scores = {'示例A': 0, '示例B': 0}) {
  return {size: board.length, board, scores, colors: {'示例A': 'black', '示例B': 'white'}, actor: '示例A'};
}

test('activity shows the board and returning to chat hides a retained activity snapshot', () => {
  const {view, container} = room();
  const state = boardState([[0, 0, 0], [0, 1, 0], [0, 0, 0]]);
  assert.equal(container.querySelector('.board-stage').hidden, true);
  view.applyStatus(activityStatus(state));
  assert.equal(container.querySelector('.board-stage').hidden, false);
  assert.equal(container.querySelector('.board-state').textContent, '示例A 落子');
  view.applyStatus(activityStatus(state, 'chat'));
  assert.equal(container.querySelector('.board-stage').hidden, true);
});

test('board snapshots repaint stones and update the displayed score', () => {
  const {view, container} = room();
  const canvas = container.querySelector('canvas');
  function draw(state) {
    const start = canvas.draws.length;
    view.applyStatus(activityStatus(state));
    const calls = canvas.draws.slice(start);
    assert.ok(calls.some(call => call.method === 'clearRect'), 'each snapshot clears the old drawing');
    assert.ok(calls.some(call => call.method === 'fill'), 'each snapshot paints the canvas');
    return calls.filter(call => call.method === 'arc');
  }
  const empty = draw(boardState([[0, 0, 0], [0, 0, 0], [0, 0, 0]]));
  const first = draw(boardState([[1, 0, 0], [0, 0, 0], [0, 0, 0]]));
  const second = draw(boardState([[1, 0, 0], [0, 0, 0], [0, 0, 2]], {'示例A': 1, '示例B': 2}));
  assert.equal(first.length, empty.length + 1, 'first stone appears');
  assert.equal(second.length, first.length + 1, 'next snapshot adds the second stone');
  assert.notDeepEqual(second.at(-1).args.slice(0, 2), first.at(-1).args.slice(0, 2), 'stones occupy different cells');
  assert.deepEqual(container.querySelectorAll('.score-number').map(node => node.textContent), ['1', '2']);
  const reset = draw(boardState([[0, 0, 0], [0, 0, 0], [0, 0, 0]]));
  assert.deepEqual(reset, empty, 'a new empty snapshot removes the old stones');
});

function app() {
  const env = environment();
  const script = source('app.js');
  const boot = script.search(/^\(async function boot\(\)/m);
  assert.ok(boot >= 0, 'locate automatic boot so tests remain offline');
  vm.runInContext(script.slice(0, boot), env.context, {filename: 'app.js'});
  env.render = status => {
    env.context.testStatus = status;
    vm.runInContext('renderStatus(testStatus)', env.context);
  };
  return env;
}

test('running ready accepts input; preview, stopping and disconnect disable it', async t => {
  const cases = [
    ['running ready', {running: true, phase: 'ready'}, true],
    ['chat program', {running: true, phase: 'ready', program: {running: true, phase: 'chat'}}, true],
    ['preview', {running: true, phase: 'ready', preview: true}, false],
    ['stopping', {running: true, phase: 'ready', stopping: true}, false],
    ['program stopping', {running: true, program: {running: true, phase: 'stopping'}}, false],
    ['idle', {running: false, phase: 'ready'}, false],
  ];
  for (const [name, status, expected] of cases) {
    await t.test(name, () => {
      const env = app();
      env.render(status);
      assert.equal(vm.runInContext('canSubmit()', env.context), expected);
      for (const id of ['input', 'send', 'say', 'speaker']) {
        assert.equal(env.document.getElementById(id).disabled, !expected, id);
      }
    });
  }
  await t.test('disconnected', () => {
    const env = app();
    env.render({running: true, phase: 'ready'});
    vm.runInContext('connected = false; updateControls()', env.context);
    assert.equal(vm.runInContext('canSubmit()', env.context), false);
    assert.equal(env.document.getElementById('send').disabled, true);
  });
});

test('debug download uses the metadata whitelist without input, text, config or speaker', async () => {
  const env = app();
  const secret = 'PRIVATE-CONTENT-MUST-NOT-BE-EXPORTED';
  env.render({
    run_id: 'debug-run', mode: 'demo', event_seq: 12,
    input: secret, text: secret, config: {api_key: secret}, speaker: secret,
    current_request: {text: secret, speaker: secret},
    program: {event_seq: 7, config: {prompt: secret}, events: [{data: {text: secret}}]},
    events: [{data: {input: secret, text: secret, speaker: secret}}],
    requests: [{
      request_id: 'request-debug', kind: 'chat', source: 'manual', phase: 'failed', outcome: 'failed',
      error: `audio error: ${secret}`, queued_at: 100, updated_at: 101,
      timings: {queued: 2, generating: 5}, elapsed_ms: 10,
      input: secret, text: secret, config: {api_key: secret}, speaker: secret,
    }],
  });
  env.document.getElementById('export-debug').click();
  assert.equal(env.blobs.length, 1);
  assert.equal(env.blobs[0].type, 'application/json');
  const serialized = await env.blobs[0].text();
  const payload = JSON.parse(serialized);
  assert.deepEqual(payload, {
    run_id: 'debug-run', mode: 'demo', event_seq: 12, program_event_seq: 7,
    requests: [{
      request_id: 'request-debug', kind: 'chat', source: 'manual', phase: 'failed', outcome: 'failed',
      error_code: 'audio_error', queued_at: 100, updated_at: 101,
      timings: {queued: 2, generating: 5}, elapsed_ms: 10,
    }],
  });
  function checkKeys(value) {
    if (!value || typeof value !== 'object') return;
    for (const [key, child] of Object.entries(value)) {
      assert.ok(!['input', 'text', 'config', 'speaker'].includes(key), `unexpected private field ${key}`);
      checkKeys(child);
    }
  }
  checkKeys(payload);
  assert.equal(serialized.includes(secret), false);
  const download = env.created.find(node => node.tagName === 'A' && node.clicked);
  assert.ok(download, 'export triggers a download');
  assert.match(download.download, /\.json$/);
  assert.deepEqual(env.revoked, [download.href]);
});
