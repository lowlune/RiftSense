'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

let failures = 0;

function assert(cond, msg) {
  if (cond) {
    console.log('ok - ' + msg);
  } else {
    failures++;
    console.error('FAIL - ' + msg);
  }
}

const html = fs.readFileSync(path.join(__dirname, 'overlay.html'), 'utf8');
const match = html.match(/<script[^>]*>([\s\S]*?)<\/script>/i);
if (!match) {
  console.error('FAIL - overlay.html has no inline script');
  process.exit(1);
}

function makeEl(id) {
  const classes = new Set();
  const style = {
    setProperty(name, value) { this[name] = String(value); },
    removeProperty(name) { delete this[name]; },
  };
  return {
    id,
    innerHTML: '',
    textContent: '',
    title: '',
    className: '',
    disabled: false,
    dataset: {},
    style,
    children: [],
    classList: {
      add() { for (const c of arguments) classes.add(c); },
      remove() { for (const c of arguments) classes.delete(c); },
      toggle(c, force) {
        const on = force === undefined ? !classes.has(c) : !!force;
        if (on) classes.add(c); else classes.delete(c);
        return on;
      },
      contains(c) { return classes.has(c); },
    },
    setAttribute(k, v) { this[k] = String(v); },
    getAttribute(k) { return this[k] == null ? null : String(this[k]); },
    addEventListener() {},
    appendChild(c) { this.children.push(c); return c; },
  };
}

const ids = ['clock', 'conn', 'sources', 'donow', 'donowText', 'death', 'deathText',
  'objDragon', 'dragonVal', 'objBaron', 'baronVal'];
const els = {};
for (const id of ids) els[id] = makeEl(id);

const body = makeEl('body');

const pending = new Promise(() => {});
const sandbox = {
  console,
  performance: { now: () => Date.now() },
  setTimeout: () => 0,
  clearTimeout: () => {},
  setInterval: () => 0,
  clearInterval: () => {},
  localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  URLSearchParams,
  location: { search: '?bg=transparent&scale=2&poll=3000' },
  AbortController: undefined,
  document: {
    body,
    documentElement: makeEl('html'),
    getElementById: id => els[id] || null,
    querySelector: () => null,
    createElement: tag => makeEl(tag),
    addEventListener() {},
  },
  fetch: () => pending,
};
sandbox.window = sandbox;

vm.createContext(sandbox);
vm.runInContext(match[1], sandbox, { filename: 'overlay.html' });

const T = sandbox.window.__riftOverlay;
assert(T && typeof T === 'object', 'overlay test hook exposed');

if (T) {
  assert(T.config.bg === 'transparent', 'query param bg=transparent applied');
  assert(T.config.scale === 2, 'query param scale=2 applied');
  assert(T.config.poll === 3000, 'query param poll=3000 applied');
  assert(T.esc('<img src=x onerror=alert(1)>') === '&lt;img src=x onerror=alert(1)&gt;', 'esc encodes tags');
  assert(T.esc('"x" & \'y\'') === '&quot;x&quot; &amp; &#39;y&#39;', 'esc encodes quotes and ampersand');
  assert(T.tidy('\u00d4\u00c7\u00f6') === '\u2014', 'tidy repairs mojibake');
  assert(T.fmt('nope') === '--:--', 'fmt rejects non-numeric text');
  assert(T.fmt(-5) === '--:--', 'fmt rejects negative values');
  assert(T.fmt(65) === '1:05', 'fmt formats 65s as 1:05');
  assert(T.fmt('125.9') === '2:05', 'fmt floors numeric strings');

  T.applyGame({ status: 'live', inGame: true, time: 125, dragonKills: [{ t: 60, type: 'Fire' }], baronKills: [{ t: 400 }] });
  assert(T.src.game === 'ok' && T.inGame === true, 'applyGame marks live source ok');
  assert(/^\d{1,2}:\d{2}$/.test(els.clock.textContent), 'game clock rendered');
  assert(/^\d{1,2}:\d{2}$/.test(els.dragonVal.textContent), 'dragon timer rendered');
  assert(/^\d{1,2}:\d{2}$/.test(els.baronVal.textContent), 'baron timer rendered');
  assert(els.conn.textContent === 'live', 'connection shows live');

  T.applyCoach({ text: 'THREAT: care\nDO NOW: <b>collapse mid</b>', age: 1, status: 'ok' });
  assert(els.donow.style.display === 'flex', 'DO NOW shown');
  assert(els.donowText.innerHTML.indexOf('&lt;b&gt;collapse mid&lt;/b&gt;') >= 0, 'DO NOW escaped');
  assert(els.donowText.innerHTML.indexOf('<b>') < 0, 'DO NOW contains no raw tags');

  T.applyCoach({ text: 'THREAT: only a threat', age: 1, status: 'ok' });
  assert(els.donow.style.display === 'none', 'DO NOW hidden when absent');

  T.applyDeath({ text: 'PENDING|12:34|<img src=x onerror=alert(1)>', age: 2, status: 'ok' });
  assert(els.death.style.display === 'flex', 'pending death shown');
  assert(els.deathText.innerHTML.indexOf('12:34') >= 0, 'pending death clock shown');
  assert(els.deathText.innerHTML.indexOf('<img') < 0, 'pending killer neutralized');
  assert(els.deathText.innerHTML.toLowerCase().indexOf('onerror') < 0, 'pending killer identity not rendered');

  T.applyDeath({ text: '===DEATH===\nDIED: 07:20 to Faker\nWHY: overextended', age: 3, status: 'ok' });
  assert(els.deathText.innerHTML.indexOf('07:20') >= 0, 'report death clock shown');
  assert(els.deathText.innerHTML.indexOf('Faker') < 0, 'report killer identity not rendered');

  T.applyDeath({ text: 'DEATH REPORT', age: 2, status: 'ok', structured: { doNow: 'reset and ward', died: '09:01 to Zed', killedByChampion: true } });
  assert(els.deathText.innerHTML.indexOf('09:01') >= 0, 'structured death clock shown');
  assert(els.deathText.innerHTML.indexOf('Zed') < 0, 'structured killer identity not rendered');
  assert(els.donow.style.display === 'flex' && els.donowText.innerHTML.indexOf('reset and ward') >= 0, 'death DO NOW fallback shown');

  T.applyDeath({ text: 'PENDING|40:00|x', age: 999, status: 'ok' });
  assert(els.death.style.display === 'none', 'expired death hidden');

  T.applyGame({ status: 'no_game', inGame: false });
  assert(els.conn.textContent === 'no game', 'connection shows no game');
  assert(els.clock.textContent === '--:--', 'clock blank with no game');

  T.applyGame({ status: 'live', inGame: true, time: 'not-a-number', dragonKills: 'bad', baronKills: [{ t: 'nope' }] });
  assert(els.clock.textContent === '--:--', 'invalid game time rejected');
  assert(els.dragonVal.textContent === '--:--', 'invalid dragon kills rejected');
  assert(els.baronVal.textContent === '--:--', 'invalid baron kills rejected');

  T.applyGame({ status: 'live', inGame: true, time: 42, dragonKills: [], baronKills: [] });
  assert(els.sources.textContent.indexOf('game ok') >= 0, 'source health line includes game');
  assert(els.sources.textContent.indexOf('timeline') >= 0, 'source health line includes timeline');
}

if (failures) {
  console.error(failures + ' assertion(s) failed');
  process.exit(1);
}
console.log('overlay harness: all assertions passed');
