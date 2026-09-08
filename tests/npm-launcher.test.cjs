"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const launcher = require("../bin/zeus-code.cjs");

const WORK = path.resolve(__dirname, "..", ".work");

function digest(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function temporaryDirectory(t) {
  fs.mkdirSync(WORK, { recursive: true });
  const directory = fs.mkdtempSync(path.join(WORK, "npm-launcher-"));
  t.after(() => fs.rmSync(directory, { force: true, recursive: true }));
  return directory;
}

function writeFixturePackage(root, options = {}) {
  const version = options.version || "1.2.3";
  const schemaVersion = options.schemaVersion === undefined ? 7 : options.schemaVersion;
  const bundle = options.bundle || Buffer.from("verified zipapp fixture\n");
  const dist = path.join(root, "dist");
  fs.mkdirSync(dist, { recursive: true });
  fs.writeFileSync(
    path.join(root, "package.json"),
    JSON.stringify({ name: "@kanterlabs/zeus-code", version }),
  );
  fs.writeFileSync(path.join(dist, "zeus-code.pyz"), bundle);
  const metadata = Buffer.from(
    JSON.stringify({ python_requires: "3.11", schema_version: schemaVersion, version }) + "\n",
  );
  fs.writeFileSync(path.join(dist, "release.json"), metadata);
  fs.writeFileSync(
    path.join(dist, "SHA256SUMS"),
    `${digest(bundle)}  zeus-code.pyz\n${digest(metadata)}  release.json\n`,
  );
  return { bundle, metadata };
}

function successfulSpawnRecorder(calls) {
  return (executable, args, options) => {
    calls.push({ args: [...args], executable, options });
    if (args[0] === "-c") {
      return { signal: null, status: 0, stderr: "", stdout: "3.12.2\n" };
    }
    return { signal: null, status: 0 };
  };
}

function runFixture(t, argv, extra = {}) {
  const root = temporaryDirectory(t);
  const packageRoot = path.join(root, "package");
  const cacheRoot = path.join(root, "cache");
  const homedir = path.join(root, "home");
  const cwd = path.join(root, "caller workspace");
  fs.mkdirSync(packageRoot, { recursive: true });
  fs.mkdirSync(homedir);
  fs.mkdirSync(cwd);
  writeFixturePackage(packageRoot);
  const calls = [];
  const notices = [];
  const result = launcher.runLauncher({
    argv,
    cacheRoot,
    cwd,
    env: { ZEUS_CODE_PYTHON: "/chosen/python" },
    homedir,
    packageRoot,
    platform: "linux",
    spawnSync: successfulSpawnRecorder(calls),
    stderr: { write: (value) => notices.push(value) },
    ...extra,
  });
  return { cacheRoot, calls, cwd, homedir, notices, packageRoot, result, root };
}

test("verified bundles are cached at a stable content-addressed path", (t) => {
  const root = temporaryDirectory(t);
  const packageRoot = path.join(root, "package");
  const cacheRoot = path.join(root, "cache");
  fs.mkdirSync(packageRoot);
  const fixture = writeFixturePackage(packageRoot);

  const release = launcher.verifyBundledRelease(packageRoot);
  const cached = launcher.ensureCachedBundle(release, { cacheRoot });

  assert.equal(
    cached,
    path.join(cacheRoot, "zeus-code", "npm", `1.2.3-${digest(fixture.bundle)}`, "zeus-code.pyz"),
  );
  assert.deepEqual(fs.readFileSync(cached), fixture.bundle);
  assert.equal(launcher.ensureCachedBundle(release, { cacheRoot }), cached);
});

test("source checksum corruption is rejected before caching", (t) => {
  const root = temporaryDirectory(t);
  const packageRoot = path.join(root, "package");
  fs.mkdirSync(packageRoot);
  writeFixturePackage(packageRoot);
  fs.appendFileSync(path.join(packageRoot, "dist", "zeus-code.pyz"), "tampered");

  assert.throws(
    () => launcher.verifyBundledRelease(packageRoot),
    /checksum mismatch for bundled zeus-code\.pyz/,
  );
});

test("an existing corrupt cache entry is never overwritten", (t) => {
  const root = temporaryDirectory(t);
  const packageRoot = path.join(root, "package");
  const cacheRoot = path.join(root, "cache");
  fs.mkdirSync(packageRoot);
  writeFixturePackage(packageRoot);
  const release = launcher.verifyBundledRelease(packageRoot);
  const cached = launcher.ensureCachedBundle(release, { cacheRoot });
  fs.chmodSync(cached, 0o600);
  fs.writeFileSync(cached, "occupied by different bytes");

  assert.throws(
    () => launcher.ensureCachedBundle(release, { cacheRoot }),
    /refusing to overwrite a possibly running bundle/,
  );
  assert.equal(fs.readFileSync(cached, "utf8"), "occupied by different bytes");
});

test("package and release versions must match and schema_version must be an integer", (t) => {
  const root = temporaryDirectory(t);
  const packageRoot = path.join(root, "package");
  fs.mkdirSync(packageRoot);
  writeFixturePackage(packageRoot, { schemaVersion: 7.5 });
  assert.throws(() => launcher.verifyBundledRelease(packageRoot), /schema_version must be an integer/);

  writeFixturePackage(packageRoot);
  fs.writeFileSync(
    path.join(packageRoot, "package.json"),
    JSON.stringify({ name: "@kanterlabs/zeus-code", version: "1.2.4" }),
  );
  assert.throws(() => launcher.verifyBundledRelease(packageRoot), /does not match package\.json version/);
});

test("findPython tries python3 then python and requires the probe to pass", () => {
  const calls = [];
  const found = launcher.findPython({
    env: {},
    platform: "linux",
    spawnSync(executable) {
      calls.push(executable);
      if (executable === "python3") {
        return { signal: null, status: 42, stderr: "found Python 3.10.9; 3.11 or newer is required" };
      }
      return { signal: null, status: 0, stderr: "", stdout: "3.11.8\n" };
    },
  });
  assert.equal(found, "python");
  assert.deepEqual(calls, ["python3", "python"]);
});

test("an invalid ZEUS_CODE_PYTHON override fails without a fallback", () => {
  const calls = [];
  assert.throws(
    () => launcher.findPython({
      env: { ZEUS_CODE_PYTHON: "/missing/python" },
      platform: "linux",
      spawnSync(executable) {
        calls.push(executable);
        const error = Object.assign(new Error("spawn ENOENT"), { code: "ENOENT" });
        return { error, signal: null, status: null };
      },
    }),
    /ZEUS_CODE_PYTHON=.*not a usable Python 3\.11\+ executable with curses.*not found/,
  );
  assert.deepEqual(calls, ["/missing/python"]);
});

test("explicit CLI arguments and caller cwd reach Python unchanged", (t) => {
  const argv = ["send", "thread id", "literal $() and `ticks`", "--data-dir", "state dir"];
  const run = runFixture(t, argv);
  const executions = run.calls.filter((call) => call.args[0] !== "-c");

  assert.equal(executions.length, 1);
  assert.deepEqual(executions[0].args.slice(1), argv);
  assert.equal(executions[0].options.cwd, run.cwd);
  assert.equal(executions[0].options.stdio, "inherit");
  assert.equal(run.result.status, 0);
});

test("bare local launch bootstraps the matching data directory before the TUI", (t) => {
  const dataDir = "state with spaces";
  const argv = ["--data-dir", dataDir];
  const run = runFixture(t, argv);
  const executions = run.calls.filter((call) => call.args[0] !== "-c");
  const cachedBundle = executions[0].args[0];

  assert.deepEqual(executions.map((call) => call.args), [
    [cachedBundle, "--data-dir", dataDir, "serve", "--background"],
    [cachedBundle, ...argv],
  ]);
});

test("bootstrap is limited to local default and local connect launches", () => {
  const cases = [
    { argv: [], expected: true },
    { argv: ["--data-dir=state"], expected: true },
    { argv: ["connect", "--data-dir", "state"], expected: true },
    { argv: ["status", "--data-dir", "state"], expected: false },
    { argv: ["--help"], expected: false },
    { argv: ["--version"], expected: false },
    { argv: ["connect", "remote"], expected: false },
    { argv: ["connect", "--host", "remote"], expected: false },
    { argv: ["--host=remote"], expected: false },
  ];
  for (const { argv, expected } of cases) {
    assert.equal(launcher.analyzeArguments(argv).shouldBootstrap, expected, JSON.stringify(argv));
  }
});

test("npx update defaults to ~/.local/bin without changing an explicit target", () => {
  const argv = ["--data-dir", "state", "update", "--check"];
  const analysis = launcher.analyzeArguments(argv);
  const rewritten = launcher.rewriteUpdateArguments(argv, analysis, {
    homedir: "/home/tester",
    packageName: "@kanterlabs/zeus-code",
    version: "1.2.3",
  });
  assert.deepEqual(argv, ["--data-dir", "state", "update", "--check"]);
  assert.deepEqual(rewritten.argv, [...argv, "--install-dir", "/home/tester/.local/bin"]);
  assert.equal(rewritten.notice, null);

  const installing = ["update"];
  const installingRewrite = launcher.rewriteUpdateArguments(
    installing,
    launcher.analyzeArguments(installing),
    {
      homedir: "/home/tester",
      packageName: "@kanterlabs/zeus-code",
      version: "1.2.3",
    },
  );
  assert.match(installingRewrite.notice, /npx @kanterlabs\/zeus-code@latest/);
  assert.match(installingRewrite.notice, /npx github:KanterLabs\/zeus-code/);

  const explicit = ["update", "--install-dir=/opt/zeus"];
  assert.deepEqual(
    launcher.rewriteUpdateArguments(explicit, launcher.analyzeArguments(explicit), {
      homedir: "/home/tester",
    }),
    { argv: explicit, notice: null },
  );
});

test("native Windows is rejected with WSL guidance", () => {
  assert.throws(() => launcher.checkPlatform("win32"), /native Windows.*WSL/);
});
