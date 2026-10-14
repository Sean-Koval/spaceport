import { readFileSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
const temporary = mkdtempSync(join(tmpdir(), 'publisher-openapi-'));
try {
  const output = join(temporary, 'api.ts');
  const result = spawnSync(
    process.execPath,
    [
      'node_modules/openapi-typescript/bin/cli.js',
      '../docs/compatibility/openapi.json',
      '-o',
      output,
    ],
    { stdio: 'inherit' },
  );
  if (result.status !== 0) process.exitCode = 1;
  else if (!readFileSync(output).equals(readFileSync('src/generated/api.ts'))) {
    console.error('Generated client is out of date. Run npm run generate and review the diff.');
    process.exitCode = 1;
  }
} finally {
  rmSync(temporary, { recursive: true, force: true });
}
