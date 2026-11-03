#!/usr/bin/env node
// Renders aja-annons.html frame by frame (via window.seek) into an MP4, or into stills.
//
//   node render.cjs                          -> aja-annons.mp4 (1080x1920, 30 fps)
//   node render.cjs --fps 60 --out x.mp4
//   node render.cjs --stills 0.5,3,9.5       -> frames/still-0.50.png ...
//
// CDN and Google Fonts requests are served from node_modules when installed
// (npm install), so rendering works offline; otherwise they go to the network.
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

function pkg(name) {
  // Walk up from this folder looking for node_modules/<name> (some packages, like three,
  // do not export their package.json, so require.resolve cannot be used here).
  for (let dir = __dirname; ; dir = path.dirname(dir)) {
    const candidate = path.join(dir, 'node_modules', name);
    if (fs.existsSync(path.join(candidate, 'package.json'))) return candidate;
    if (path.dirname(dir) === dir) return null;
  }
}
const LOCAL = { gsap: pkg('gsap'), three: pkg('three'), font: pkg('@fontsource-variable/archivo') };
const CORS = { 'access-control-allow-origin': '*' };

async function routeToLocal(page) {
  await page.route(/^https:\/\//, async (route) => {
    const url = route.request().url();
    let m;
    if (LOCAL.gsap && (m = url.match(/cdn\.jsdelivr\.net\/npm\/gsap@[^/]+\/([^?#]+)/)))
      return route.fulfill({ path: path.join(LOCAL.gsap, m[1]), contentType: 'text/javascript', headers: CORS });
    if (LOCAL.three && (m = url.match(/cdn\.jsdelivr\.net\/npm\/three@[^/]+\/([^?#]+)/)))
      return route.fulfill({ path: path.join(LOCAL.three, m[1]), contentType: 'text/javascript', headers: CORS });
    if (LOCAL.font && url.startsWith('https://fonts.googleapis.com/'))
      return route.fulfill({ contentType: 'text/css', headers: CORS, body:
        "@font-face{font-family:'Archivo';font-style:normal;font-weight:100 900;font-stretch:62% 125%;" +
        "src:url(https://fonts.gstatic.com/local/archivo.woff2) format('woff2-variations')}" });
    if (LOCAL.font && url.startsWith('https://fonts.gstatic.com/local/'))
      return route.fulfill({ path: path.join(LOCAL.font, 'files/archivo-latin-standard-normal.woff2'), contentType: 'font/woff2', headers: CORS });
    return route.continue();
  });
}

// Opens the ad in headless Chromium at 1080x1920 and returns helpers to grab exact frames.
async function openAd({ html = path.join(__dirname, 'aja-annons.html'), launchArgs = [] } = {}) {
  const browser = await chromium.launch({ args: launchArgs });
  const page = await browser.newPage({ viewport: { width: 1080, height: 1920 }, deviceScaleFactor: 1 });
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  await routeToLocal(page);
  await page.goto('file://' + path.resolve(html) + '?clean=1');
  await page.waitForFunction(() => window.ready, null, { timeout: 60000 });
  await page.evaluate(() => window.ready);
  const duration = await page.evaluate(() => window.DURATION);
  const shot = async (t, type = 'png') => {
    await page.evaluate((t) => window.seek(t), t);
    return page.screenshot({ type, clip: { x: 0, y: 0, width: 1080, height: 1920 } });
  };
  return { browser, page, errors, duration, shot };
}

async function main() {
  const argv = process.argv.slice(2);
  const arg = (name, def) => { const i = argv.indexOf('--' + name); return i >= 0 ? argv[i + 1] : def; };
  const out = path.resolve(__dirname, arg('out', 'aja-annons.mp4'));
  const fps = +arg('fps', 30);
  const stills = arg('stills', null);
  const { browser, errors, duration, shot } = await openAd({ html: path.resolve(__dirname, arg('html', 'aja-annons.html')) });

  if (stills) {
    const dir = path.join(__dirname, 'frames');
    fs.mkdirSync(dir, { recursive: true });
    for (const t of stills.split(',').map(Number)) {
      const file = path.join(dir, `still-${t.toFixed(2)}.png`);
      fs.writeFileSync(file, await shot(t));
      console.log(file);
    }
  } else {
    const total = Math.round(duration * fps);
    const ff = spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(fps), '-c:v', 'png', '-i', '-',
      '-c:v', 'libx264', '-preset', 'slow', '-crf', '17', '-pix_fmt', 'yuv420p', '-profile:v', 'high', '-movflags', '+faststart', out],
      { stdio: ['pipe', 'inherit', 'inherit'] });
    const started = Date.now();
    for (let f = 0; f < total; f++) {
      const buf = await shot(f / fps);
      if (!ff.stdin.write(buf)) await new Promise((r) => ff.stdin.once('drain', r));
      if (f % 30 === 0) {
        const per = (Date.now() - started) / (f + 1);
        console.log(`frame ${f}/${total}  ~${Math.round(per)} ms/frame  eta ${Math.round(((total - f) * per) / 1000)} s`);
      }
    }
    ff.stdin.end();
    await new Promise((r) => ff.on('close', r));
    console.log('wrote', out);
  }
  console.log('page errors:', errors.length ? errors : 'none');
  await browser.close();
}

module.exports = { openAd };
if (require.main === module) main().catch((e) => { console.error(e); process.exit(1); });
