// Vendored Airframe stays byte-identical. Only the local runtime derivative drops
// the Google Fonts import: Spaceport's fonts must be served from the same origin.
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const check = process.argv.includes('--check');
const manifest = JSON.parse(readFileSync(resolve(root, 'vendor/airframe/manifest.json'), 'utf8'));
const source = (path) => readFileSync(resolve(root, 'vendor/airframe/source', path));
for (const entry of manifest.files) {
  const bytes = source(entry.path);
  const sha256 = createHash('sha256').update(bytes).digest('hex');
  const gitBlob = createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex');
  if (sha256 !== entry.sha256 || gitBlob !== entry.gitBlob) {
    throw new Error(`Airframe upstream integrity mismatch: ${entry.path}`);
  }
}

const components = source('theme/airframe-components.css').toString('utf8');
const fontImport =
  "@import url('https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,100..900&family=DM+Mono:wght@400;500&family=Red+Hat+Text:ital,wght@0,400..700;1,400..700&display=swap');";
if (components.split(fontImport).length !== 2) {
  throw new Error('Review upstream font transport before updating Airframe.');
}
const localComponents = components.replace(
  fontImport,
  '/* Spaceport modification: Google Fonts import removed; airframe-fonts.css serves local fonts. */',
);
if (/@import|url\(\s*["']?(?:https?:|\/\/)/i.test(localComponents)) {
  throw new Error('Airframe runtime CSS must not fetch external resources.');
}

const outputs = new Map([
  ['src/generated/airframe/airframe.css', source('theme/airframe.css')],
  ['src/generated/airframe/airframe-components.css', Buffer.from(localComponents)],
  ['public/airframe/airframe-icons.svg', source('theme/airframe-icons.svg')],
  ['public/airframe/favicon.svg', source('brand/spaceport/favicon.svg')],
  ['public/airframe/LICENSE', source('LICENSE')],
]);
const mark = JSON.parse(source('airframe/icons/marks.json').toString('utf8')).spaceport;
outputs.set(
  'public/airframe/spaceport-glyph.svg',
  Buffer.from(
    `<svg xmlns="http://www.w3.org/2000/svg"><symbol id="spaceport" viewBox="0 0 24 24">${mark.glyph}</symbol></svg>\n`,
  ),
);
for (const { path } of manifest.files.filter(({ path }) => path.startsWith('airframe/fonts/'))) {
  outputs.set(`public/${path}`, source(path));
}
for (const [path, bytes] of outputs) {
  const destination = resolve(root, path);
  if (check) {
    if (!readFileSync(destination).equals(bytes)) {
      throw new Error(`Stale Airframe output: ${path}. Run npm run generate:airframe.`);
    }
  } else {
    mkdirSync(dirname(destination), { recursive: true });
    writeFileSync(destination, bytes);
  }
}
console.log(
  `Airframe ${manifest.tag}: ${manifest.files.length} exact upstream files; ${outputs.size} local outputs ${check ? 'verified' : 'generated'}.`,
);
