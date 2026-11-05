// browser_test.js — drive the mic UI in a REAL headless Chromium and prove,
// conclusively, whether the agent's audio actually reaches + plays in a browser.
//
// Headless has no speaker, so "did you hear it" can't rely on playback alone.
// Instead we tap every inbound <audio> element's MediaStream with a Web Audio
// AnalyserNode and measure signal ENERGY over time. Non-zero peak energy =
// audio samples are genuinely flowing into the page's audio graph (i.e. a human
// with speakers would hear it). We also record play() promise outcomes and
// room.canPlaybackAudio to distinguish autoplay-block from no-audio.
//
// Env: CHROME=<chrome binary>  URL=<page>  WAV=<fake mic wav, optional>
const { chromium } = require('playwright-core');

const EXEC = process.env.CHROME
  || '/home/user/.cache/ms-playwright/chromium-1217/chrome-linux64/chrome';
const URL = process.env.URL || 'http://127.0.0.1:8790';
const WAV = process.env.WAV || '';

(async () => {
  const args = [
    '--no-sandbox',
    '--disable-dev-shm-usage',
    '--use-fake-ui-for-media-stream',
    '--use-fake-device-for-media-stream',
    // Force real autoplay gating so we actually test the startAudio() fix:
    '--autoplay-policy=document-user-activation-required',
  ];
  if (WAV) args.push(`--use-file-for-fake-audio-capture=${WAV}`);

  const browser = await chromium.launch({ executablePath: EXEC, headless: true, args });
  const page = await browser.newPage();

  const logs = [];
  page.on('console', m => logs.push(`[console.${m.type()}] ${m.text()}`));
  page.on('pageerror', e => logs.push(`[pageerror] ${e.message}`));

  // Install audio-energy tap + play() instrumentation before page scripts run.
  await page.addInitScript(() => {
    window.__diag = { plays: [], peak: 0, samples: 0, tapped: 0 };
    const AC = window.AudioContext || window.webkitAudioContext;
    const ctx = new AC();
    const tapped = new WeakSet();

    function tap(el) {
      if (!el.srcObject || tapped.has(el)) return;
      try {
        const src = ctx.createMediaStreamSource(el.srcObject);
        const an = ctx.createAnalyser();
        an.fftSize = 512;
        src.connect(an);
        tapped.add(el);
        window.__diag.tapped++;
        const buf = new Uint8Array(an.fftSize);
        setInterval(() => {
          an.getByteTimeDomainData(buf);
          let peak = 0;
          for (let i = 0; i < buf.length; i++) {
            const v = Math.abs(buf[i] - 128);
            if (v > peak) peak = v;
          }
          window.__diag.samples++;
          if (peak > window.__diag.peak) window.__diag.peak = peak;
        }, 100);
      } catch (e) { /* ignore */ }
    }

    // Continuously scan for attached audio elements and tap them.
    setInterval(() => document.querySelectorAll('audio').forEach(tap), 200);

    const op = HTMLMediaElement.prototype.play;
    HTMLMediaElement.prototype.play = function () {
      const r = op.apply(this, arguments);
      if (r && r.then) {
        r.then(() => window.__diag.plays.push({ ok: true }))
         .catch(e => window.__diag.plays.push({ ok: false, err: e.name }));
      }
      return r;
    };
  });

  await page.goto(URL, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1500);
  const status0 = await page.textContent('#statusText').catch(() => '?');

  await page.click('#connectBtn').catch(e => logs.push('[click error] ' + e.message));

  // Let connect + greeting happen; keep measuring energy across the window.
  await page.waitForTimeout(14000);

  const diag = await page.evaluate(() => {
    const els = Array.from(document.querySelectorAll('audio'));
    return {
      plays: window.__diag.plays,
      peak: window.__diag.peak,
      samples: window.__diag.samples,
      tapped: window.__diag.tapped,
      audioEls: els.length,
      canPlayback: (window.__lkRoom && window.__lkRoom.canPlaybackAudio),
      enableBtnShown: getComputedStyle(document.getElementById('enableSoundBtn')).display !== 'none',
      status: document.getElementById('statusText').textContent,
    };
  });

  console.log('=== STATUS initial :', status0, '| final:', diag.status);
  console.log('=== audio elements :', diag.audioEls, '| tapped:', diag.tapped);
  console.log('=== play() results :', JSON.stringify(diag.plays));
  console.log('=== enable-sound btn shown:', diag.enableBtnShown);
  console.log('=== AUDIO ENERGY   : peak=' + diag.peak + ' over ' + diag.samples + ' samples');
  console.log('=== PAGE CONSOLE ===');
  logs.forEach(l => console.log('   ' + l));

  // peak amplitude is 0..127 (centered at 128). >3 = real audio present.
  const ENERGY_THRESHOLD = 3;
  const heard = diag.peak >= ENERGY_THRESHOLD;
  const blocked = diag.plays.some(p => !p.ok && p.err === 'NotAllowedError') || diag.enableBtnShown;
  const verdict = heard ? 'AUDIO_HEARD'
    : blocked ? 'AUTOPLAY_BLOCKED'
    : diag.audioEls === 0 ? 'NO_AUDIO_TRACK'
    : 'NO_AUDIO_ENERGY';
  console.log('BROWSER_VERDICT=' + verdict + ' peak=' + diag.peak);

  await browser.close();
  process.exit(0);
})().catch(e => { console.error('TEST_ERROR', e && e.stack || e); process.exit(3); });
