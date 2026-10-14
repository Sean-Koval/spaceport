const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const publisherRequire = createRequire(path.resolve(__dirname, '../../../publisher/package.json'));
const { JSDOM } = publisherRequire('jsdom');
const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
const dom = new JSDOM(html, {
  runScripts: 'dangerously', url: 'https://preview.invalid/',
  beforeParse(window) { window.scrollTo = () => {}; window.HTMLElement.prototype.scrollIntoView = () => {}; },
});
const w = dom.window;
const d = w.document;
const $ = s => d.querySelector(s);
let passed = 0;
function check(name, fn) { fn(); passed++; console.log(`PASS ${name}`); }
function input(selector, value) { $(selector).value = value; $(selector).dispatchEvent(new w.Event('input', { bubbles: true })); }
function route(hash) { w.location.hash = hash; w.dispatchEvent(new w.HashChangeEvent('hashchange')); }
check('catalog displays six synthetic products', () => assert.equal(d.querySelectorAll('.card').length, 6));
check('visual language uses SVG icons rather than decorative glyphs', () => {
  assert.ok(!/[\p{Extended_Pictographic}\uFE0F\u{1F1E6}-\u{1F1FF}✓◇◐↗←→↓▦]/u.test(html));
  for (const selector of ['#theme', '.status', '.card h2 a', '.check-icon', '#copy']) {
    assert.ok($(selector).querySelector('svg'), selector);
  }
  for (const icon of d.querySelectorAll('svg')) {
    assert.equal(icon.getAttribute('aria-hidden'), 'true');
    assert.equal(icon.getAttribute('focusable'), 'false');
  }
});
check('headings describe the current task directly', () => {
  assert.equal($('#catalog-title').textContent, 'Data catalog');
  assert.equal($('#review-title').textContent, 'Review publication');
  assert.equal($('#overview h2').textContent, 'Product definition');
});
check('search filters by meaning', () => { input('#search', 'refunds'); assert.equal(d.querySelectorAll('.card').length, 2); });
check('empty state safely handles markup-shaped input', () => { input('#search', '<img src=x onerror=alert(1)>'); assert.equal(d.querySelectorAll('.card').length, 0); assert.ok($('#clear')); assert.equal(d.querySelectorAll('img').length, 0); });
check('clear filters restores catalog', () => { $('#clear').click(); assert.equal(d.querySelectorAll('.card').length, 6); });
check('domain filter returns finance products', () => { $('#domain').value = 'Finance'; $('#domain').dispatchEvent(new w.Event('change')); assert.equal(d.querySelectorAll('.card').length, 2); });
check('product selection updates contract and reference', () => { $('[data-product="finance.refund_ledger"]').click(); route('product'); assert.equal($('#product-title').textContent, 'Refund ledger'); assert.equal($('#detail-id').textContent, 'finance.refund_ledger'); assert.equal($('#field-count').textContent, '3 explicit fields'); assert.equal($('#catalog').hidden, true); assert.equal($('#product').hidden, false); });
check('sample run presents matching synthetic product fields', () => { $('#run-form').dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true })); assert.equal($('#run-result').hidden, false); assert.equal(d.querySelectorAll('#results-table tbody tr').length, 3); assert.equal($('#results-table th').textContent, 'refund_id'); });
check('date validation rejects reversed range', () => { input('#start', '2026-10-10'); input('#end', '2026-10-01'); $('#run-form').dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true })); assert.equal($('#end').validity.customError, true); input('#end', '2026-10-12'); assert.equal($('#end').validity.customError, false); });
check('light and dark theme button labels stay accurate', () => { $('#theme').click(); assert.equal(d.documentElement.dataset.theme, 'light'); assert.equal($('#theme').getAttribute('aria-label'), 'Switch to dark theme'); $('#theme').click(); assert.equal(d.documentElement.dataset.theme, 'dark'); });
check('review is navigable and never claims real publication', () => { route('review'); assert.equal($('#review').hidden, false); assert.equal($('[data-nav="review"]').getAttribute('aria-current'), 'page'); $('#publish').click(); assert.match($('#publish-status').textContent, /Nothing was published/); });
check('in-page navigation preserves product route', () => { route('contract'); assert.equal($('#product').hidden, false); assert.equal($('#review').hidden, true); });
check('skip link preserves each page and its history hash', () => { for (const page of ['catalog','product','review']) { route(page); $('.skip').focus(); $('.skip').click(); assert.equal(w.location.hash, `#${page}`); assert.equal(d.activeElement.id, 'main'); assert.equal($(`#${page}`).hidden, false); } });
check('page headings receive route focus', () => { route('catalog'); assert.equal(d.activeElement.id, 'catalog-title'); });
check('document has no external assets or network calls', () => { assert.equal(d.querySelectorAll('script[src],link[href],img[src],iframe').length, 0); assert.ok(!/\bfetch\s*\(|XMLHttpRequest|WebSocket/.test(html)); });
check('all controls have accessible labels', () => { for (const el of d.querySelectorAll('button,input,select')) { assert.ok(el.textContent.trim() || el.getAttribute('aria-label') || d.querySelector(`label[for="${el.id}"]`), el.outerHTML); } });
check('IDs are unique', () => { const ids = [...d.querySelectorAll('[id]')].map(el => el.id); assert.equal(ids.length, new Set(ids).size); });
console.log(`${passed} preview checks passed. DOM tests do not validate browser layout.`);
w.close();
