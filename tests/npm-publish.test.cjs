'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { publish, integrity, registryIntegrity } = require('../scripts/publish-npm.cjs');
function fixture(t) {
  const base = path.resolve(__dirname, '../.work');
  fs.mkdirSync(base, { recursive: true });
  const root = fs.mkdtempSync(path.join(base, 'npm-publish-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.mkdirSync(path.join(root, 'dist'));
  fs.mkdirSync(path.join(root, 'src/zeus_code'), { recursive: true });
  const pkg = { name: '@kanterlabs/zeus-code', version: '1.0.4', publishConfig: { access: 'public', registry: 'https://registry.npmjs.org/' } };
  fs.writeFileSync(path.join(root, 'package.json'), JSON.stringify(pkg));
  fs.writeFileSync(path.join(root, 'dist/release.json'), JSON.stringify({ version: pkg.version }));
  fs.writeFileSync(path.join(root, 'src/zeus_code/__init__.py'), '__version__ = "1.0.4"\n');
  const filename = 'kanterlabs-zeus-code-1.0.4.tgz';
  fs.writeFileSync(path.join(root, 'dist', filename), 'test archive');
  const hash = integrity(path.join(root, 'dist', filename));
  const calls = [];
  const run = args => { calls.push(args); return JSON.stringify([{ ...pkg, filename, integrity: hash }]); };
  return { root, env: { GITHUB_REF_NAME: 'v1.0.4', NODE_AUTH_TOKEN: 'test-only' }, run, calls, hash };
}
test('publishes once then verifies exact registry integrity', async t => {
  const f = fixture(t); let count = 0;
  assert.match(await publish({ ...f, lookup: async () => count++ ? f.hash : null }), /Published and verified/);
  assert.equal(f.calls[2][0], 'publish');
  assert.ok(f.calls[2].includes('--ignore-scripts'));
});
test('matching registry bytes are idempotent without authentication', async t => {
  const f = fixture(t); delete f.env.NODE_AUTH_TOKEN;
  assert.match(await publish({ ...f, lookup: async () => f.hash }), /already published/);
  assert.equal(f.calls.length, 2);
});
test('conflicting bytes and missing token prevent publication', async t => {
  const f = fixture(t);
  await assert.rejects(publish({ ...f, lookup: async () => 'different' }), /different bytes/);
  delete f.env.NODE_AUTH_TOKEN;
  await assert.rejects(publish({ ...f, lookup: async () => null }), /NPM_TOKEN/);
  assert.ok(f.calls.every(args => args[0] !== 'publish'));
});
test('tag mismatch prevents packing and publishing', async t => {
  const f = fixture(t); f.env.GITHUB_REF_NAME = 'v1.0.3';
  await assert.rejects(publish(f), /must match/);
  assert.equal(f.calls.length, 0);
});
test('registry errors are not interpreted as an unpublished package', async () => {
  assert.equal(await registryIntegrity('@kanterlabs/zeus-code', '1.0.4', async () => ({ status: 404 })), null);
  await assert.rejects(registryIntegrity('@kanterlabs/zeus-code', '1.0.4', async () => ({ status: 503, ok: false })), /503/);
});
