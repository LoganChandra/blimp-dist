// browser_check.js — verify audio playback against the ALREADY-RUNNING stack.
// Does NOT start/kill the agent (won't disrupt a live session). Drives the page
// in headless Chromium under strict autoplay policy, clicks Connect, clicks the
// "enable sound" fallback if shown, then measures whether an <audio> element's
// currentTime ADVANCES (ungated by autoplay — the reliable headless signal).
//
// Env: CHROME, URL
const { chromium } = require('playwright-core');
const EXEC = process.env.CHROME || '/home/user/.cache/ms-playwright/chromium-1217/chrome-linux64/chrome';
const URL = process.env.URL || 'http://127.0.0.1:8790';
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const browser = await chromium.launch({
    executablePath: EXEC, headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage',
           '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream',
           '--autoplay-policy=document-user-activation-required'],
  });
  const page = await browser.newPage();
  const logs = [];
  page.on('console', m => logs.push(`[${m.type()}] ${m.text()}`));
  page.on('pageerror', e => logs.push(`[pageerror] ${e.message}`));

  const snap = () => page.evaluate(() => Array.from(document.querySelectorAll('audio')).map(e => ({
    paused: e.paused, rs: e.readyState, t: +(e.currentTime || 0).toFixed(3), hasStream: !!e.srcObject })));

  await page.goto(URL, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1200);
  await page.click('#connectBtn').catch(e => logs.push('[click connect] ' + e.message));
  await sleep(6000);

  const enableVisible = await page.isVisible('#enableSoundBtn').catch(() => false);
  if (enableVisible) { await page.click('#enableSoundBtn').catch(() => {}); await sleep(500); }

  const before = await snap();
  await sleep(4000);
  const after = await snap();
  const canPlayback = await page.evaluate(() => window.__lkRoom ? window.__lkRoom.canPlaybackAudio : null);
  const statusF = await page.textContent('#statusText').catch(() => '?');

  let advanced = false, maxDelta = 0;
  for (let i = 0; i < after.length; i++) {
    const d = after[i].t - ((before[i] || {}).t || 0);
    if (after[i].hasStream && !after[i].paused && d > 0.3) advanced = true;
    if (d > maxDelta) maxDelta = d;
  }

  console.log('STATUS_FINAL=' + statusF);
  console.log('ENABLE_SOUND_SHOWN=' + enableVisible);
  console.log('CAN_PLAYBACK=' + canPlayback);
  console.log('AUDIO_AFTER=' + JSON.stringify(after));
  console.log('CURRENTTIME_ADVANCED=' + advanced + ' MAXDELTA=' + maxDelta.toFixed(2));
  logs.filter(l => /Blimp|startAudio|enable|Error|error/.test(l)).slice(0, 20).forEach(l => console.log('  ' + l));
  console.log('VERDICT=' + (advanced ? 'AUDIO_PLAYING' : (after.length === 0 ? 'NO_AUDIO_TRACK' : (canPlayback === false ? 'STILL_BLOCKED' : 'NO_PROGRESS'))));

  await browser.close();
  process.exit(0);
})().catch(e => { console.error('TEST_ERROR', e && (e.stack || e)); process.exit(3); });
