// e2e_browser.js — drive the real page in headless Chromium against the REAL
// server + REAL Frank (`claude -p`), with the browser's speech APIs swapped for
// scriptable fakes (headless Chromium has no speech recognizer and no speaker).
//
// Proves: voice transcript -> debounce -> turn -> streamed reply -> spoken
// sentence-by-sentence -> back to listening; multi-turn memory; barge-in stops
// Frank mid-reply; Frank's own words echoed back through the mic do NOT barge in.
//
// Env: URL (default http://127.0.0.1:8791)  CHROME  SHOT (screenshot path)
// Needs playwright-core on NODE_PATH.
const { chromium } = require('playwright-core');
const URL = process.env.URL || 'http://127.0.0.1:8791';
const CHROME = process.env.CHROME || `${process.env.HOME}/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome`;

const FAKES = () => {
  window.__spoken = []; window.__cancels = 0;
  let timers = [];
  window.SpeechSynthesisUtterance = class { constructor(t) { this.text = t; } };
  const synth = {
    speaking: false,
    getVoices: () => [{ name: 'Test Voice', lang: 'en-US' }],
    speak(u) {
      if (!u.text) return;
      window.__spoken.push(u.text);
      const ms = 120 * u.text.split(/\s+/).length;      // "talks" for a while, like real TTS
      timers.push(setTimeout(() => u.onstart && u.onstart(), 10));
      timers.push(setTimeout(() => u.onend && u.onend(), ms));
    },
    cancel() { window.__cancels++; timers.forEach(clearTimeout); timers = []; },
  };
  Object.defineProperty(window, 'speechSynthesis', { value: synth });
  window.webkitSpeechRecognition = window.SpeechRecognition = class {
    constructor() { window.__rec = this; }
    start() { this.started = true; }
    stop() { this.started = false; }
  };
  window.__emit = (transcript, isFinal) => {
    const r = [{ transcript }]; r.isFinal = isFinal;
    window.__rec.onresult({ resultIndex: 0, results: [r] });
  };
};

const log = (...a) => console.log('[e2e]', ...a);
let fails = 0;
const check = (ok, what) => { log(ok ? 'PASS' : 'FAIL', what); if (!ok) fails++; };

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME,
    args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'] });
  const page = await browser.newPage({ viewport: { width: 1100, height: 760 } });
  globalThis.__page = page;
  page.on('pageerror', e => { log('PAGE ERROR', e.message); fails++; });
  await page.addInitScript(FAKES);
  await page.goto(URL);
  await page.click('#reset');

  const state = () => page.evaluate(() => window.__frank.state);
  const waitState = (s, ms = 60000) => page.waitForFunction(s => window.__frank.state === s, s, { timeout: ms });

  await page.click('#orb');
  check(await state() === 'listening', 'tap orb -> listening');

  // Turn 1 — by voice: interim then final, as Chrome delivers it
  let t0 = Date.now();
  await page.evaluate(() => { window.__emit('hey Frank in one', false); window.__emit('Hey Frank, in one short sentence, what is your job on the team?', true); });
  await waitState('thinking', 5000);
  check(true, 'final transcript -> turn sent (thinking)');
  await page.waitForFunction(() => window.__spoken.length > 0, null, { timeout: 60000 });
  log(`first audio after ${Date.now() - t0} ms`);
  await waitState('listening');
  const r1 = await page.evaluate(() => window.__frank.reply);
  log('Frank:', r1);
  check(/operat|ops|monitor|steady|frank/i.test(r1), 'turn 1 reply is Frank describing his job');
  check(!/[*#`]/.test(r1), 'reply has no markdown');

  // Turn 2 — memory across turns
  await page.evaluate(() => window.__emit('What did I just ask you, in a few words?', true));
  await waitState('thinking', 5000);
  await page.waitForFunction(() => window.__frank.state === 'listening' && window.__frank.reply.length > 0, null, { timeout: 60000 });
  const r2 = await page.evaluate(() => window.__frank.reply);
  log('Frank:', r2);
  check(/job|role|team|do|what you/i.test(r2), 'turn 2 remembers turn 1');

  // Turn 3 — longer answer, then echo test, then barge-in
  await page.evaluate(() => window.__emit('Tell me in four or five sentences how your weekly rituals work.', true));
  await waitState('speaking', 60000);
  const cancelsBefore = await page.evaluate(() => window.__cancels);
  await page.evaluate(() => { const w = window.__frank.reply.split(/\s+/).slice(0, 5).join(' '); window.__emit(w, false); });
  await page.waitForTimeout(300);
  check(await state() === 'speaking' && await page.evaluate(() => window.__cancels) === cancelsBefore,
        'echo of Frank\'s own words does NOT barge in');
  await page.evaluate(() => window.__emit('wait hold on a second', false));
  await page.waitForTimeout(200);
  check(await state() === 'listening' && await page.evaluate(() => window.__cancels) > cancelsBefore,
        'user speech barges in: Frank stops, back to listening');
  // let the debounce of that interim lapse without a final; nothing should be sent
  await page.waitForTimeout(1800);
  check(await state() === 'listening', 'interim-only speech does not trigger a turn');

  // Typed fallback
  await page.fill('#typedText', 'Just say the word ok.');
  await page.press('#typedText', 'Enter');
  await waitState('thinking', 5000);
  await page.waitForFunction(() => window.__frank.state === 'listening' && window.__frank.reply.length > 0, null, { timeout: 60000 });
  log('Frank:', await page.evaluate(() => window.__frank.reply));
  check(true, 'typed turn works');

  if (process.env.SHOT) await page.screenshot({ path: process.env.SHOT });
  await page.click('#orb', { force: true });   // orb size tracks the (fake) mic level, so never 'stable'
  check(await state() === 'off', 'tap orb again -> off');
  await browser.close();
  log(fails ? `E2E_RESULT=FAIL (${fails})` : 'E2E_RESULT=PASS');
  process.exit(fails ? 1 : 0);
})().catch(async e => { log('E2E_RESULT=FAIL', e.message);
  try { log('debug', JSON.stringify(await globalThis.__page.evaluate(() => ({ s: window.__frank.state, r: window.__frank.reply, st: document.getElementById('status').textContent, spoken: window.__spoken.slice(-3) })))); } catch {}
  process.exit(1); });
