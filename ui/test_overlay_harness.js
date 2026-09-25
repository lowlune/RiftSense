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

const IDS = ['clock', 'conn', 'sources', 'donow', 'donowText', 'death', 'deathText',
  'objDragon', 'dragonVal', 'dragonState', 'objBaron', 'baronVal', 'baronState'];

function loadOverlay(search) {
  const els = {};
  for (const id of IDS) els[id] = makeEl(id);
  els.donow.style.display = 'none';
  els.death.style.display = 'none';
  const body = makeEl('body');
  let clockMs = 1000;
  const pending = new Promise(() => {});
  const sandbox = {
    console,
    performance: { now: () => clockMs },
    setTimeout: () => 0,
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    URLSearchParams,
    location: { search: search || '' },
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
  return {
    T: sandbox.window.__riftOverlay,
    els,
    body,
    advance(ms) { clockMs += ms; },
  };
}

const main = loadOverlay('?bg=transparent&scale=2&poll=3000');
const T = main.T;
const els = main.els;
assert(T && typeof T === 'object', 'overlay test hook exposed');

if (T) {
  assert(T.config.bg === 'transparent', 'query param bg=transparent applied');
  assert(T.config.scale === 2, 'query param scale=2 applied');
  assert(T.config.poll === 3000, 'query param poll=3000 applied');
  assert(T.config.preset === 'default', 'default preset applied');
  assert(T.config.clockScale === 2 && T.config.actionScale === 2 && T.config.timerScale === 2, 'global scale feeds independent scales');
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
  assert(els.deathText.innerHTML.toLowerCase().indexOf('onerror') < 0, 'pending killer name not rendered');

  T.applyDeath({ text: '===DEATH===\nDIED: 07:20 to Faker\nWHY: overextended', age: 3, status: 'ok' });
  assert(els.deathText.innerHTML.indexOf('07:20') >= 0, 'report death clock shown');
  assert(els.deathText.innerHTML.indexOf('Faker') < 0, 'report killer name not rendered');

  T.applyDeath({ text: 'DEATH REPORT', age: 2, status: 'ok', structured: { doNow: 'reset and ward', died: '09:01 to Zed', killedByChampion: true } });
  assert(els.deathText.innerHTML.indexOf('09:01') >= 0, 'structured death clock shown');
  assert(els.deathText.innerHTML.indexOf('Zed') < 0, 'structured killer name not rendered');
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
  assert(els.sources.textContent.indexOf('action') >= 0, 'source health line includes action endpoint');

  assert(T.parsePreset('corner') === 'corner', 'parsePreset corner');
  assert(T.parsePreset('STRIP') === 'strip', 'parsePreset is case-insensitive');
  assert(T.parsePreset('second-monitor') === 'second-monitor', 'parsePreset second-monitor');
  assert(T.parsePreset('secondmonitor') === 'second-monitor', 'parsePreset alias secondmonitor');
  assert(T.parsePreset('bogus') === 'default', 'parsePreset rejects unknown names');
  assert(T.parsePreset(null) === 'default', 'parsePreset handles missing value');

  assert(T.privacyText('Faker#EUW is here', false).indexOf('Faker#EUW') >= 0, 'normal privacy keeps Name#TAG');
  assert(T.privacyText('word '.repeat(200), false).length <= 400, 'normal text capped at 400');
}

const strip = loadOverlay('?preset=strip&clockScale=1.5&privacy=strict&bg=chroma&scale=1&poll=5000');
assert(strip.T.config.preset === 'strip', 'preset=strip parsed');
assert(strip.T.config.clockScale === 1.5, 'clockScale override parsed');
assert(strip.T.config.actionScale === 0.7, 'preset supplies default action scale');
assert(strip.T.config.timerScale === 0.7, 'preset supplies default timer scale');
assert(strip.T.config.privacy === 'strict', 'privacy=strict parsed');
assert(strip.body.className.indexOf('preset-strip') >= 0, 'body gets preset class');
assert(strip.body.className.indexOf('privacy-strict') >= 0, 'body gets privacy class');
assert(strip.body.className.indexOf('bg-chroma') >= 0, 'preset load keeps bg mode');

const corner = loadOverlay('?preset=corner');
assert(corner.T.config.preset === 'corner', 'preset=corner parsed');
assert(corner.T.config.clockScale === 0.8 && corner.T.config.actionScale === 0.85, 'corner preset independent scales');

const second = loadOverlay('?preset=second-monitor');
assert(second.T.config.preset === 'second-monitor', 'preset=second-monitor parsed');
assert(second.T.config.clockScale === 1.2, 'second-monitor preset scales up the clock');

const priv = strip.T;
assert(priv.privacyText('dive Faker#EUW now', true).indexOf('[name]') >= 0, 'strict privacy inserts name marker');
assert(priv.privacyText('dive Faker#EUW now', true).indexOf('Faker') < 0, 'strict privacy strips Name#TAG');
assert(priv.privacyText('server #EUW down', true).indexOf('[tag]') >= 0, 'strict privacy strips bare #TAG');
assert(priv.privacyText('word '.repeat(200), true).length <= 120, 'strict text capped at 120');
assert(priv.privacyText('a\u0000b\u0007c', true) === 'a b c', 'control characters removed');

priv.applyGame({ status: 'live', inGame: true, time: 100, sessionId: 's1' });
priv.applyCoach({ text: 'DO NOW: dive Faker#EUW now <script>alert(1)</script>', age: 1, status: 'ok' });
assert(strip.els.donow.style.display === 'flex', 'strict coach action shown');
assert(strip.els.donowText.innerHTML.indexOf('Faker#EUW') < 0, 'strict strips Name#TAG from rendered action');
assert(strip.els.donowText.innerHTML.indexOf('[name]') >= 0, 'strict rendered action keeps marker');
assert(strip.els.donowText.innerHTML.indexOf('<script>') < 0, 'strict still escapes markup');

const ttl = loadOverlay('?bg=dark');
const TT = ttl.T;
const TE = ttl.els;
TT.applyGame({ status: 'live', inGame: true, time: 300, sessionId: 's1' });
TT.applyCoach({ text: 'DO NOW: old coach call', age: 300, status: 'ok' });
assert(TE.donow.style.display === 'none', 'aged coach action hidden by TTL');
TT.applyCoach({ text: 'DO NOW: fresh coach call', age: 1, status: 'ok' });
assert(TE.donow.style.display === 'flex' && TE.donowText.innerHTML.indexOf('fresh coach call') >= 0, 'fresh coach action shown');
TT.applyDeath({ text: '===DEATH===\nDIED: 05:00', age: 1, status: 'ok', structured: { died: '05:00', doNow: 'death priority action' } });
assert(TE.donowText.innerHTML.indexOf('death priority action') >= 0, 'fresh death beats coach in local fallback');
assert(TE.death.style.display === 'flex', 'fresh death headline shown');
ttl.advance(61000);
TT.tick();
assert(TE.death.style.display === 'none', 'death hidden after 60s TTL');
assert(TE.donowText.innerHTML.indexOf('fresh coach call') >= 0, 'coach action returns after death expires');
ttl.advance(40000);
TT.tick();
assert(TE.donow.style.display === 'none', 'coach hidden after 90s TTL');

const api = loadOverlay('');
const AT = api.T;
const AE = api.els;
AT.applyGame({ status: 'live', inGame: true, time: 100, sessionId: 's1' });
AT.applyCoach({ text: 'DO NOW: local coach', age: 1, status: 'ok' });
assert(AE.donow.style.display === 'flex', 'local coach fallback shown before action endpoint');
AT.applyAction({ ok: true, action: null });
assert(AE.donow.style.display === 'none', 'authoritative empty action hides local fallback');
AT.applyAction({ ok: true, action: { kind: 'objective', text: 'objective window', priority: 2, ageSec: 1, sessionId: 's1' } });
assert(AE.donowText.innerHTML.indexOf('objective window') >= 0, 'api objective action shown');
AT.applyAction({ ok: true, action: { kind: 'coach', text: 'api coach', priority: 1, ageSec: 300, sessionId: 's1' } });
assert(AE.donow.style.display === 'none', 'expired api action hidden without local fallback');
AT.applyAction({ ok: true, action: { kind: 'coach', text: 'api coach', priority: 1, ageSec: 1, sessionId: 'other' } });
assert(AE.donow.style.display === 'none', 'session-mismatched api action hidden');
AT.applyAction({ ok: true, action: { kind: 'death', text: '<b>urgent</b>', priority: 3, ageSec: 1, sessionId: 's1' } });
assert(AE.donowText.innerHTML.indexOf('&lt;b&gt;urgent&lt;/b&gt;') >= 0 && AE.donowText.innerHTML.indexOf('<b>') < 0, 'api action text escaped');
const past = Math.floor(Date.now() / 1000) - 10;
AT.applyAction({ ok: true, action: { kind: 'coach', text: 'expired by expiresAt', priority: 1, ageSec: 1, expiresAt: past, sessionId: 's1' } });
assert(AE.donow.style.display === 'none', 'expiresAt in the past hides action');
const future = Math.floor(Date.now() / 1000) + 3600;
AT.applyAction({ ok: true, action: { kind: 'coach', text: 'valid by expiresAt', priority: 1, ageSec: 1, expiresAt: future, sessionId: 's1' } });
assert(AE.donow.style.display === 'flex', 'future expiresAt keeps action');
AT.applyAction({ ok: false });
assert(AE.donow.style.display === 'flex' && AE.donowText.innerHTML.indexOf('local coach') >= 0, 'rejected action envelope falls back to local coach');

const ses = loadOverlay('');
const ST = ses.T;
const SE = ses.els;
ST.applyGame({ status: 'live', inGame: true, time: 100, sessionId: 's1' });
ST.applyCoach({ text: 'DO NOW: s1 call', age: 1, status: 'ok' });
ST.applyDeath({ text: '===DEATH===\nDIED: 01:40', age: 1, status: 'ok', structured: { died: '01:40', doNow: 's1 death call' } });
assert(SE.donow.style.display === 'flex' && SE.death.style.display === 'flex', 's1 action and death visible');
const before = ST.refreshCount;
ST.applyGame({ status: 'live', inGame: true, time: 120, sessionId: 's2' });
assert(ST.gameSession === 's2', 'game session tracked');
assert(SE.donow.style.display === 'none' && SE.death.style.display === 'none', 'session change clears action and death');
assert(ST.refreshCount > before, 'session change triggers immediate refresh');
ST.applyCoach({ text: 'DO NOW: s2 call', age: 1, status: 'ok' });
assert(SE.donowText.innerHTML.indexOf('s2 call') >= 0, 'new session coach shown');
ST.applyDeath({ text: '===DEATH===\nDIED: 02:00', age: 1, status: 'ok', sessionId: 's1', structured: { died: '02:00', doNow: 'stale death call' } });
assert(SE.death.style.display === 'none', 'death from an older session hidden');
assert(SE.donow.style.display === 'flex', 'session-mismatched death does not displace current action');
const mid = ST.refreshCount;
ST.applyGame({ status: 'no_game', inGame: false });
assert(SE.donow.style.display === 'none', 'game end clears action');
assert(ST.refreshCount > mid, 'game end triggers immediate refresh');
ST.applyGame({ status: 'live', inGame: true, time: 10, sessionId: 's3' });
assert(ST.refreshCount > mid + 1, 'game start triggers immediate refresh');

const stale = loadOverlay('');
const XT = stale.T;
const XE = stale.els;
XT.applyGame({ status: 'live', inGame: true, time: 250, dragonKills: [{ t: 0, type: 'Fire' }], baronKills: [{ t: 0 }] });
assert(XE.objDragon.classList.contains('soon'), 'dragon soon class while clock fresh');
assert(XE.dragonState.textContent === 'SOON', 'dragon soon text state present');
stale.advance(7000);
XT.tick();
assert(XE.clock.classList.contains('stale'), 'clock marked stale after fresh window');
assert(!XE.objDragon.classList.contains('soon') && !XE.objDragon.classList.contains('up'), 'no up/soon color while clock stale');
assert(XE.objDragon.classList.contains('stale'), 'dragon timer marked stale');
assert(XE.dragonState.textContent === 'STALE', 'dragon timer text state stale');
assert(XT.connInfo().text === 'stale', 'connection reports stale');
XT.applyGame({ status: 'live', inGame: true, time: 251, dragonKills: [{ t: 0, type: 'Fire' }], baronKills: [{ t: 0 }] });
assert(!XE.objDragon.classList.contains('stale') && XE.dragonState.textContent === 'SOON', 'fresh game data restores timer state');
XT.applyGame({ status: 'live', inGame: true, time: 320, dragonKills: [{ t: 0, type: 'Fire' }], baronKills: [{ t: 0 }] });
assert(XE.dragonVal.textContent === 'UP' && XE.dragonState.textContent === 'UP', 'dragon up shown with text state');
assert(XE.objDragon.classList.contains('up') && !XE.objDragon.classList.contains('soon'), 'dragon up class only when up');


if (failures) {
  console.error(failures + ' assertion(s) failed');
  process.exit(1);
}
console.log('overlay harness: all assertions passed');
