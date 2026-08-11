#!/usr/bin/env node
import { spawn, spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { tmpdir } from 'node:os';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '..');
const chrome = process.env.CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const magick = process.env.MAGICK || 'magick';
const sourceHtml = join(here, 'avalanche-diamond-threejs-spin-with-wordmark-export-start.html');

const outputs = {
  logo: join(root, 'avalanche-logo-3d.png'),
  transparentLogo: join(root, 'avalanche-logo-3d_transparent.png'),
  diamond1024: join(root, 'avalanche-diamond-3d-1024.png'),
};

if (!existsSync(chrome)) {
  throw new Error(`Chrome not found at ${chrome}. Set CHROME=/path/to/chrome.`);
}
if (!existsSync(sourceHtml)) {
  throw new Error(`Missing source HTML: ${sourceHtml}`);
}

function run(command, args) {
  const result = spawnSync(command, args, { stdio: 'inherit' });
  if (result.status !== 0) {
    throw new Error(`${command} ${args.join(' ')} failed with ${result.status}`);
  }
}

function wait(ms) {
  return new Promise((resolveWait) => setTimeout(resolveWait, ms));
}

async function removeTempDir(path) {
  for (let attempt = 0; attempt < 10; attempt += 1) {
    try {
      rmSync(path, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 });
      return;
    } catch (error) {
      if (attempt === 9) {
        throw error;
      }
      await wait(100);
    }
  }
}

async function readDevtoolsPort(userDataDir) {
  const portFile = join(userDataDir, 'DevToolsActivePort');
  for (let i = 0; i < 100; i += 1) {
    if (existsSync(portFile)) {
      const [port] = readFileSync(portFile, 'utf8').trim().split('\n');
      return port;
    }
    await wait(100);
  }
  throw new Error('Timed out waiting for Chrome DevToolsActivePort');
}

class CdpClient {
  constructor(url) {
    this.nextId = 1;
    this.pending = new Map();
    this.events = new Map();
    this.ws = new WebSocket(url);
  }

  async open() {
    await new Promise((resolveOpen, rejectOpen) => {
      this.ws.addEventListener('open', resolveOpen, { once: true });
      this.ws.addEventListener('error', rejectOpen, { once: true });
    });
    this.ws.addEventListener('message', (event) => this.onMessage(event));
  }

  onMessage(event) {
    const message = JSON.parse(event.data);
    if (message.id && this.pending.has(message.id)) {
      const { resolvePending, rejectPending } = this.pending.get(message.id);
      this.pending.delete(message.id);
      if (message.error) rejectPending(new Error(message.error.message));
      else resolvePending(message.result || {});
      return;
    }
    if (message.method && this.events.has(message.method)) {
      for (const resolveEvent of this.events.get(message.method)) resolveEvent(message.params || {});
      this.events.delete(message.method);
    }
  }

  send(method, params = {}) {
    const id = this.nextId;
    this.nextId += 1;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolvePending, rejectPending) => {
      this.pending.set(id, { resolvePending, rejectPending });
    });
  }

  once(method) {
    return new Promise((resolveEvent) => {
      if (!this.events.has(method)) this.events.set(method, []);
      this.events.get(method).push(resolveEvent);
    });
  }

  close() {
    this.ws.close();
  }
}

async function launchChrome(tmpRoot) {
  const userDataDir = join(tmpRoot, 'chrome-profile');
  mkdirSync(userDataDir, { recursive: true });
  const child = spawn(chrome, [
    '--headless=new',
    '--remote-debugging-port=0',
    `--user-data-dir=${userDataDir}`,
    '--enable-unsafe-swiftshader',
    '--use-gl=angle',
    '--use-angle=swiftshader',
    '--hide-scrollbars',
    '--no-first-run',
    '--no-default-browser-check',
    'about:blank',
  ], { stdio: ['ignore', 'ignore', 'pipe'] });

  let stderr = '';
  child.stderr.on('data', (chunk) => {
    stderr += chunk.toString();
  });

  const port = await readDevtoolsPort(userDataDir);
  return { child, port, stderr: () => stderr };
}

async function capturePage({ port, htmlPath, width, height, deviceScaleFactor, outPath }) {
  const tabs = await fetch(`http://127.0.0.1:${port}/json/list`).then((res) => res.json());
  const tab = tabs.find((item) => item.type === 'page');
  if (!tab) throw new Error('No Chrome page target found');

  const cdp = new CdpClient(tab.webSocketDebuggerUrl);
  await cdp.open();

  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');
  await cdp.send('Emulation.setDefaultBackgroundColorOverride', {
    color: { r: 0, g: 0, b: 0, a: 0 },
  });
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width,
    height,
    deviceScaleFactor,
    mobile: false,
    screenWidth: width,
    screenHeight: height,
  });

  const loaded = cdp.once('Page.loadEventFired');
  await cdp.send('Page.navigate', { url: pathToFileURL(htmlPath).href });
  await loaded;
  await cdp.send('Runtime.evaluate', {
    expression: 'document.fonts ? document.fonts.ready : Promise.resolve()',
    awaitPromise: true,
  });
  await wait(1000);
  const screenshot = await cdp.send('Page.captureScreenshot', {
    format: 'png',
    fromSurface: true,
    captureBeyondViewport: false,
    omitBackground: true,
  });
  writeFileSync(outPath, Buffer.from(screenshot.data, 'base64'));
  cdp.close();
}

function writeDiamondHtml(tmpRoot) {
  const html = readFileSync(sourceHtml, 'utf8')
    .replace('<div class="wordmark" role="img"', '<div class="wordmark" style="display:none" role="img"')
    .replace('const textEmWidth = 7.38;', 'const textEmWidth = 0.0;')
    .replace('const gapEm = 0.08;', 'const gapEm = 0.0;')
    .replace('const maxWordmarkSize = 40 * lockupScale;', 'const maxWordmarkSize = 237.1;');
  const path = join(tmpRoot, 'diamond-export.html');
  writeFileSync(path, html);
  return path;
}

function makeTransparentWithPadding(raw, out, { erode = false, resizeContent = null } = {}) {
  const cleaned = join(dirname(raw), `${out.split('/').pop()}.clean.png`);
  const mask = join(dirname(raw), `${out.split('/').pop()}.mask.png`);

  let input = raw;
  if (erode) {
    run(magick, [raw, '-alpha', 'extract', '-morphology', 'Erode', 'Disk:1', mask]);
    run(magick, [raw, mask, '-compose', 'CopyOpacity', '-composite', cleaned]);
    input = cleaned;
  }

  const args = [input, '-trim', '+repage'];
  if (resizeContent) args.push('-resize', `${resizeContent}!`);
  args.push(
    '-gravity',
    'center',
    '-background',
    'none',
    '-bordercolor',
    'none',
    '-border',
    '144',
    '-depth',
    '8',
    out,
  );
  run(magick, args);
}

function makeWhiteLogo(transparentLogo, out) {
  run(magick, [
    transparentLogo,
    '-trim',
    '+repage',
    '-background',
    'white',
    '-alpha',
    'remove',
    '-resize',
    '3537x1011',
    '-gravity',
    'center',
    '-background',
    'white',
    '-extent',
    '3537x1011',
    '-bordercolor',
    'white',
    '-border',
    '144',
    '-background',
    'none',
    '-bordercolor',
    'none',
    '-border',
    '144',
    '-depth',
    '8',
    out,
  ]);
}

function make1024Diamond(src, out) {
  run(magick, [
    src,
    '-trim', '+repage',
    '-resize', '960x960',
    '-gravity', 'center',
    '-background', 'none',
    '-extent', '1024x1024',
    '-depth', '8',
    out,
  ]);
}

const tmpRoot = mkdtempSync(join(tmpdir(), 'avalanche-3d-export-'));
let chromeProcess;
try {
  const logoHtml = join(tmpRoot, 'logo-export.html');
  writeFileSync(logoHtml, readFileSync(sourceHtml, 'utf8'));
  const diamondHtml = writeDiamondHtml(tmpRoot);
  const logoRaw = join(tmpRoot, 'logo-raw.png');
  const diamondRaw = join(tmpRoot, 'diamond-raw.png');
  const diamondFull = join(tmpRoot, 'avalanche-diamond-3d.png');

  const launched = await launchChrome(tmpRoot);
  chromeProcess = launched.child;

  await capturePage({
    port: launched.port,
    htmlPath: logoHtml,
    width: 1024,
    height: 397,
    deviceScaleFactor: 4,
    outPath: logoRaw,
  });

  await capturePage({
    port: launched.port,
    htmlPath: diamondHtml,
    width: 1200,
    height: 1600,
    deviceScaleFactor: 4,
    outPath: diamondRaw,
  });

  makeTransparentWithPadding(logoRaw, outputs.transparentLogo, {
    resizeContent: '3825x1299',
  });
  makeWhiteLogo(outputs.transparentLogo, outputs.logo);
  makeTransparentWithPadding(diamondRaw, diamondFull, { erode: true, resizeContent: '2600x3457' });
  make1024Diamond(diamondFull, outputs.diamond1024);

  console.log(`wrote ${outputs.logo}`);
  console.log(`wrote ${outputs.transparentLogo}`);
  console.log(`wrote ${outputs.diamond1024}`);
} finally {
  if (chromeProcess) {
    chromeProcess.kill('SIGKILL');
    chromeProcess.unref();
    chromeProcess.stderr.destroy();
    await wait(100);
  }
  await removeTempDir(tmpRoot);
}
