// Original bounded DOM worker inspired by Jev's observed-target methodology.
// No model-generated selectors, coordinates, scripts, credentials or decisions.
import { createRequire } from 'node:module';
import { resolve } from 'node:path';
import { createHash } from 'node:crypto';
const require = createRequire(import.meta.url);
const modulePath = process.env.OUTCOME_PLAYWRIGHT_MODULE || resolve('.outcome/browser/node_modules/playwright');
const { chromium } = require(modulePath);
let raw = '';
for await (const part of process.stdin) { raw += part; if (raw.length > 150000) throw new Error('Job too large'); }
const job = JSON.parse(raw);
const origin = new URL(job.action.target_binding.origin);
if (origin.protocol !== 'http:' || origin.hostname !== '127.0.0.1' || origin.pathname !== '/' || origin.username || origin.password || origin.search || origin.hash) throw new Error('Only fixed loopback ERP origins accepted');
if (!/^action-[a-f0-9]{20}$/.test(job.action.action_id)) throw new Error('Invalid action ID');
const allowed = new Set(['/', '/login', '/suppliers', '/merge', '/receipts/' + job.action.action_id]);
const options = { headless: true };
if (process.env.OUTCOME_BROWSER_CHANNEL) options.channel = process.env.OUTCOME_BROWSER_CHANNEL;
const browser = await chromium.launch(options);
const deadline = setTimeout(() => { browser.close().catch(() => {}); }, 40000);
const trace = [];
try {
  const context = await browser.newContext({ acceptDownloads: false, serviceWorkers: 'block' });
  await context.route('**/*', route => {
    const url = new URL(route.request().url());
    return url.origin === origin.origin && allowed.has(url.pathname) && !url.search && route.request().resourceType() === 'document'
      ? route.continue() : route.abort('blockedbyclient');
  });
  const page = await context.newPage();
  page.setDefaultTimeout(7000);
  page.on('dialog', d => d.dismiss());
  page.on('popup', p => p.close());
  async function observe() {
    const url = new URL(page.url());
    if (url.origin !== origin.origin || !allowed.has(url.pathname)) throw new Error('Unexpected document');
    const handles = await page.$$('input:not([type=hidden]),textarea,button,a');
    const entries = [];
    for (let i = 0; i < handles.length; i++) {
      const info = await handles[i].evaluate(el => {
        const r = el.getBoundingClientRect(), s = getComputedStyle(el);
        return { tag: el.tagName.toLowerCase(), type: el.type || '', name: el.getAttribute('aria-label') || el.textContent.trim(),
          disabled: Boolean(el.disabled), visible: r.width > 0 && r.height > 0 && s.visibility === 'visible' && s.display !== 'none',
          form: el.form?.getAttribute('action') || '', href: el.getAttribute('href') || '' };
      });
      entries.push({ index: i, ...info });
    }
    return { url: page.url(), handles, entries };
  }
  async function act(kind, name, value) {
    const observed = await observe();
    const found = observed.entries.filter(e => e.name === name && e.visible && !e.disabled);
    if (found.length !== 1) throw new Error('Missing or ambiguous observed target');
    const target = found[0], handle = observed.handles[target.index];
    if (kind === 'fill' && !['input', 'textarea'].includes(target.tag)) throw new Error('Target is not a text control');
    if (kind === 'click' && !['button', 'a'].includes(target.tag)) throw new Error('Target is not a click control');
    if (target.form && !['/login', '/merge'].includes(target.form)) throw new Error('Unexpected form');
    if (target.href && target.href !== '/merge') throw new Error('Unexpected link');
    const fresh = await handle.evaluate((el, expected) => el.isConnected
      && (el.getAttribute('aria-label') || el.textContent.trim()) === expected.name
      && (el.form?.getAttribute('action') || '') === expected.form
      && (el.getAttribute('href') || '') === expected.href
      && !el.disabled, target);
    if (page.url() !== observed.url || !fresh) throw new Error('Stale DOM reference');
    // Playwright checks visibility, enabled state and occlusion before acting.
    if (kind === 'fill') await handle.fill(value);
    else await handle.click();
    trace.push({ step: trace.length + 1, kind, control: name, index: target.index,
      observed_dom_hash: createHash('sha256').update(JSON.stringify(observed.entries)).digest('hex') });
    await Promise.all(observed.handles.map(h => h.dispose()));
  }
  await page.goto(origin.origin + '/login');
  await act('fill', 'Username', 'operator');
  await act('fill', 'Password', job.password);
  await act('click', 'Sign in');
  await page.waitForURL(origin.origin + '/suppliers');
  const receiptURL = origin.origin + '/receipts/' + job.action.action_id;
  const existing = await page.goto(receiptURL);
  let recovered = false;
  if (existing.status() === 404) {
    await page.goto(origin.origin + '/suppliers');
    await act('click', 'Open approved supplier sync');
    await page.waitForURL(origin.origin + '/merge');
    await act('fill', 'Approved payload', JSON.stringify(job.action));
    await act('fill', 'Approval capability', job.signature);
    await act('click', 'Apply approved supplier sync');
    await page.waitForURL(receiptURL);
  } else if (existing.status() === 200) recovered = true;
  else throw new Error('Existing receipt failed its ERP postcondition');
  // Independent document reload verifies actual destination state after submit.
  const verified = await page.goto(receiptURL);
  if (verified.status() !== 200) throw new Error('ERP postcondition not verified');
  const receipt = JSON.parse(await page.locator('pre[aria-label="Verified receipt"]').innerText());
  process.stdout.write(JSON.stringify({ receipt, trace, recovered, adapter: 'mock-erp-dom-v1' }));
} catch (_) {
  // Never print source records, tokens, browser diagnostics or credentials.
  process.stderr.write('Browser workflow failed; no verified completion recorded.');
  process.exitCode = 1;
} finally { clearTimeout(deadline); await browser.close(); }
