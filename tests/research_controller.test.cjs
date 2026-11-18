// Run with node tests/research_controller.test.cjs. No browser, database or models.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function runTests() {
  const elements = new Map(), listeners = {}, sent = [], layouts = [], requests = [], timers = new Map();
  let timerId = 0;
  function element(id) {
    if (!elements.has(id)) {
      const classes = new Set();
      elements.set(id, {value: '', textContent: '', disabled: false, events: {}, children: [],
        classList: {add: x => classes.add(x), remove: x => classes.delete(x), contains: x => classes.has(x),
          toggle(x, force) { const enabled = force === undefined ? !classes.has(x) : force; if (enabled) classes.add(x); else classes.delete(x); return enabled; }},
        addEventListener(type, callback) { this.events[type] = callback; },
        replaceChildren(...children) { this.children = children; }, add(child) { this.children.push(child); },
        setAttribute(key, value) { this[key] = value; }, removeAttribute(key) { delete this[key]; }, focus() { this.focused = true; }, scrollIntoView() { this.scrolled = true; }});
    }
    return elements.get(id);
  }
  const origin = 'http://127.0.0.1:8780';
  const frame = element('researchFrame');
  frame.contentWindow = {postMessage: (message, target) => (message.type === 'argus:research-layout' ? layouts : sent).push({message, target})};
  const projects = ['one', 'two'].map(id => ({id, name: id, target: 'Nd', competitor: 'La'}));
  const context = {
    document: {getElementById: element, querySelector: element, body: element('body')}, location: {origin, hash: '#existing-run'},
    window: {addEventListener: (type, callback) => { listeners[type] = callback; }, matchMedia: () => ({matches: true})},
    Option: function (text, value) { this.text = text; this.value = value; },
    fetch: async url => { requests.push(url); return {ok: true, json: async () => url === '/api/lanm-workspace' ? {available: true, embedded_url: 'https://untrusted.invalid/'} : {projects}}; },
    setTimeout: callback => { timers.set(++timerId, callback); return timerId; }, clearTimeout: id => timers.delete(id),
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../argus/web/research.js'), 'utf8'), context);
  const settle = () => new Promise(resolve => setImmediate(resolve));
  const state = (projectId, busy = false, extra = {}) => listeners.message({origin, source: frame.contentWindow,
    data: {type: 'lanm:research-state', ready: true, projectId, projectName: projectId, busy, view: 'chat'}, ...extra});
  await settle();
  assert.equal(frame.src, '/research/?embed=argus&compact=1');
  assert.deepEqual(requests, ['/api/lanm-workspace', '/api/lanm-projects']);
  assert.equal(sent.length, 0);
  // A new-project selection made before readiness supersedes a pending selection.
  context.window.argusResearch.selectProject('two');
  element('lanmProject').value = '';
  element('lanmProject').events.change();
  element('researchUnavailable').classList.add('hidden');
  [...timers.values()][0]();
  assert.equal(element('researchUnavailable').classList.contains('hidden'), false);
  state('one'); await settle();
  assert.equal(sent.length, 0);
  assert.equal(element('researchUnavailable').classList.contains('hidden'), true);
  assert.equal(layouts.at(-1).message.expanded, false);
  assert.equal(element('lanmProject').value, '');
  context.window.argusResearch.selectProject('one');
  state('one'); await settle();
  sent.length = 0;
  assert.equal(element('lanmProject').value, 'one');
  state('two', false, {origin: 'https://hostile.invalid'});
  state('two', false, {source: {}});
  assert.equal(element('lanmProject').value, 'one');
  state('one', true); await settle();
  context.window.argusResearch.selectProject('two', 'tools');
  assert.equal(element('lanmProject').disabled, true);
  assert.equal(sent.length, 0);
  state('one', false); await settle();
  assert.equal(sent.length, 1);
  assert.equal(sent[0].message.projectId, 'two');
  assert.equal(sent[0].message.view, 'tools');
  assert.equal(sent[0].target, origin);
  state('two'); await settle();
  assert.equal(element('lanmProject').value, 'two');
  // The latest choice of the current project supersedes an older queued switch.
  state('two', true); await settle();
  context.window.argusResearch.selectProject('one');
  context.window.argusResearch.selectProject('two');
  const afterLatest = sent.length;
  state('two', false); await settle();
  assert.equal(sent.length, afterLatest);
  state('two', true); await settle();
  context.window.argusResearch.selectProject('one');
  element('lanmProject').value = '';
  element('lanmProject').events.change();
  state('two'); await settle();
  assert.equal(element('lanmProject').value, '');
  assert.equal(sent.length, afterLatest);
  let prevented = false;
  element('researchWorkspaceLink').events.click({preventDefault() { prevented = true; }});
  assert.equal(prevented, true);
  assert.equal(context.location.hash, '#existing-run');
  assert.equal(element('researchWorkspace').scrolled, true);
  element('expandResearch').events.click();
  assert.equal(element('expandResearch')['aria-expanded'], 'true');
  assert.equal(layouts.at(-1).message.expanded, true);
  assert.equal(element('form').inert, true);
  assert.equal(element('researchWorkspace')['aria-modal'], 'true');
  assert.equal(frame.src, '/research/?embed=argus&compact=1', 'Expanding must not reload an active conversation.');
  element('researchBackdrop').events.click();
  assert.equal(layouts.at(-1).message.expanded, false);
  assert.equal(element('form').inert, false);
  element('researchBuild').events.click();
  assert.equal(sent.at(-1).message.view, 'build');
  listeners.message({origin, source: frame.contentWindow, data: {type: 'lanm:research-state', ready: true, projectId: 'two', projectName: 'two', busy: false, view: 'build'}});
  element('researchBrowse').value = 'database'; element('researchBrowse').events.change();
  assert.equal(layouts.at(-1).message.expanded, true);
  assert.equal(sent.at(-1).message.view, 'database');
  listeners.message({origin, source: frame.contentWindow, data: {type: 'lanm:research-state', ready: true, projectId: 'two', projectName: 'two', busy: true, view: 'database'}});
  context.window.argusResearch.selectProject('one');
  listeners.message({origin: 'https://hostile.invalid', source: frame.contentWindow, data: {type: 'lanm:research-collapse'}});
  assert.equal(element('researchWorkspace').classList.contains('expanded'), true);
  listeners.message({origin, source: frame.contentWindow, data: {type: 'lanm:research-collapse'}});
  assert.equal(sent.at(-1).message.view, 'build', 'Collapsing a browser view returns to the last conversation mode.');
  assert.equal(sent.at(-1).message.projectId, undefined, 'View changes must not wait for a queued project switch.');
  assert.equal(element('researchWorkspace').classList.contains('expanded'), false);
  listeners.message({origin, source: frame.contentWindow, data: {type: 'lanm:research-state', ready: true, projectId: 'two', projectName: 'two', busy: false, view: 'build'}});
  assert.equal(sent.at(-1).message.projectId, 'one', 'Collapsing must retain the queued project switch.');
  assert.equal(sent.at(-1).message.view, 'build');
  assert.ok(requests.every(url => ['/api/lanm-workspace', '/api/lanm-projects'].includes(url)));
  return 'Oracle controller passed: project sync, queued selections, mode controls, expand/collapse without reload, and trusted messaging.';
}
module.exports = {runTests};
if (require.main === module) runTests().then(console.log).catch(error => { console.error(error); process.exitCode = 1; });
