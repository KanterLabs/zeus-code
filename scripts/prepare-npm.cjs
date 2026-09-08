'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { findPython } = require('../bin/zeus-code.cjs');

try {
  const root = path.resolve(__dirname, '..');
  const python = findPython();
  const result = spawnSync(python, ['scripts/build.py'], { cwd: root, stdio: 'inherit' });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error('Python release build failed');
  const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
  const release = JSON.parse(fs.readFileSync(path.join(root, 'dist/release.json'), 'utf8'));
  if (pkg.version !== release.version) throw new Error('npm and Python release versions must match');
} catch (error) {
  console.error(`Cannot prepare Zeus Code: ${error.message}`);
  process.exitCode = 1;
}
