'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');
const ROOT = path.resolve(__dirname, '..');
const REGISTRY = 'https://registry.npmjs.org/';

function run(args, root = ROOT) {
  const result = spawnSync('npm', args, { cwd: root, encoding: 'utf8', timeout: 120000 });
  if (result.error || result.status !== 0) {
    // Report only recognized codes/reasons, never raw npm output or credentials.
    const output = `${result.stderr || ''}\n${result.stdout || ''}`;
    const code = output.match(/(?:npm (?:ERR!|error) code )([A-Z][A-Z0-9_]+)/)?.[1];
    let reason = 'check npm trusted publisher settings, ci.yml and direct npm publish permission';
    if (/two.factor|bypass.?2fa|one.time pass/i.test(output)) {
      reason = 'npm requires interactive authentication or a configured trusted publisher with npm publish allowed';
    } else if (/expired|revoked/i.test(output)) {
      reason = 'npm reports an expired or revoked token';
    } else if (/ENEEDAUTH|E401|EOTP/.test(output)) {
      reason = 'npm did not accept the publishing credentials';
    }
    throw new Error(`npm ${args[0]} failed${code ? ` (${code})` : ''}; ${reason}`);
  }
  return result.stdout;
}

function validate(root, tag) {
  const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
  const release = JSON.parse(fs.readFileSync(path.join(root, 'dist/release.json'), 'utf8'));
  const source = fs.readFileSync(path.join(root, 'src/zeus_code/__init__.py'), 'utf8');
  if (pkg.name !== '@kanterlabs/zeus-code' || !/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.test(pkg.version) ||
      tag !== `v${pkg.version}` || release.version !== pkg.version ||
      !source.includes(`__version__ = "${pkg.version}"`)) {
    throw new Error('Release tag, npm package, Python source and bundled versions must match');
  }
  if (pkg.publishConfig?.access !== 'public' || pkg.publishConfig?.registry !== REGISTRY) {
    throw new Error('Package must publish publicly to registry.npmjs.org');
  }
  return pkg;
}

function integrity(file) {
  return `sha512-${crypto.createHash('sha512').update(fs.readFileSync(file)).digest('base64')}`;
}

async function registryIntegrity(name, version, fetchImpl = fetch) {
  const response = await fetchImpl(`${REGISTRY}${encodeURIComponent(name)}/${version}`, {
    signal: AbortSignal.timeout(20000), redirect: 'error',
  });
  if (response.status === 404) return null;
  if (!response.ok) throw new Error(`npm registry lookup failed (${response.status})`);
  const document = await response.json();
  if (document.name !== name || document.version !== version || !document.dist?.integrity) {
    throw new Error('npm registry returned invalid package metadata');
  }
  return document.dist.integrity;
}

async function publish(options = {}) {
  const root = options.root || ROOT;
  const env = options.env || process.env;
  const execute = options.run || run;
  const lookup = options.lookup || registryIntegrity;
  const pkg = validate(root, env.GITHUB_REF_NAME);
  const artifact = path.join(root, 'dist', `kanterlabs-zeus-code-${pkg.version}.tgz`);
  execute(['run', 'prepare'], root);
  const packed = JSON.parse(execute(['pack', '--ignore-scripts', '--json', '--pack-destination', 'dist'], root));
  if (packed.length !== 1 || packed[0].name !== pkg.name || packed[0].version !== pkg.version ||
      packed[0].filename !== path.basename(artifact)) throw new Error('Unexpected npm archive identity');
  const expected = integrity(artifact);
  if (packed[0].integrity !== expected) throw new Error('npm archive integrity mismatch');
  const existing = await lookup(pkg.name, pkg.version);
  if (existing !== null) {
    if (existing !== expected) throw new Error('This npm version already exists with different bytes; refusing to overwrite');
    return `Verified ${pkg.name}@${pkg.version} is already published with identical bytes`;
  }
  if (!(env.GITHUB_ACTIONS === 'true' && env.ACTIONS_ID_TOKEN_REQUEST_URL && env.ACTIONS_ID_TOKEN_REQUEST_TOKEN)) {
    throw new Error('npm release requires GitHub Actions OIDC; grant this job id-token: write and configure the npm trusted publisher for KanterLabs/zeus-code, ci.yml');
  }
  execute(['publish', artifact, '--access', 'public', '--registry', REGISTRY, '--ignore-scripts'], root);
  const published = await lookup(pkg.name, pkg.version);
  if (published !== expected) throw new Error('npm publication returned but registry integrity is not yet verified; rerun this job');
  return `Published and verified ${pkg.name}@${pkg.version}`;
}

module.exports = { validate, integrity, registryIntegrity, publish };
if (require.main === module) {
  publish().then(message => console.log(message)).catch(error => {
    console.error(`npm release: ${error.message}`);
    process.exitCode = 1;
  });
}
