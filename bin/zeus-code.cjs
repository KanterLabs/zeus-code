#!/usr/bin/env node
"use strict";

const childProcess = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const REQUIRED_CHECKSUMS = new Set(["release.json", "zeus-code.pyz"]);
const STABLE_VERSION = /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/;
const CHECKSUM_LINE = /^([0-9a-fA-F]{64})[ \t]+\*?([A-Za-z0-9][A-Za-z0-9._-]*)[ \t]*$/;
const PYTHON_PROBE = [
  "import sys",
  "if sys.version_info < (3, 11):",
  "    print('found Python ' + '.'.join(str(part) for part in sys.version_info[:3]) + '; 3.11 or newer is required', file=sys.stderr)",
  "    raise SystemExit(42)",
  "try:",
  "    import curses",
  "except Exception as exc:",
  "    print('curses is unavailable: ' + str(exc), file=sys.stderr)",
  "    raise SystemExit(43)",
  "print('.'.join(str(part) for part in sys.version_info[:3]))",
].join("\n");

class LauncherError extends Error {
  constructor(message, options) {
    super(message, options);
    this.name = "LauncherError";
  }
}

function checkPlatform(platform = process.platform) {
  if (platform === "win32") {
    throw new LauncherError(
      "native Windows is not supported; run Zeus Code inside WSL with Python 3.11 or newer",
    );
  }
}

function compactDiagnostic(value) {
  return String(value || "")
    .trim()
    .replace(/\s+/g, " ")
    .slice(0, 500);
}

/**
 * Locate a usable Python interpreter. The optional seams keep this dependency-free
 * helper usable by package preparation and focused unit tests.
 */
function findPython(options = {}) {
  const env = options.env || process.env;
  const platform = options.platform || process.platform;
  const spawnSync = options.spawnSync || childProcess.spawnSync;
  checkPlatform(platform);

  const override = typeof env.ZEUS_CODE_PYTHON === "string" && env.ZEUS_CODE_PYTHON.length > 0
    ? env.ZEUS_CODE_PYTHON
    : null;
  const candidates = override === null ? ["python3", "python"] : [override];
  const failures = [];

  for (const executable of candidates) {
    let result;
    try {
      result = spawnSync(executable, ["-c", PYTHON_PROBE], {
        encoding: "utf8",
        env,
        windowsHide: true,
      });
    } catch (error) {
      failures.push(`${executable}: ${compactDiagnostic(error && error.message) || "could not be started"}`);
      continue;
    }
    if (result && !result.error && result.status === 0) {
      return executable;
    }
    if (result && result.error) {
      const detail = result.error.code === "ENOENT"
        ? "not found"
        : compactDiagnostic(result.error.message) || "could not be started";
      failures.push(`${executable}: ${detail}`);
    } else if (result && result.signal) {
      failures.push(`${executable}: probe ended with ${result.signal}`);
    } else {
      const detail = compactDiagnostic(result && result.stderr);
      failures.push(`${executable}: ${detail || `probe exited ${result ? result.status : "without a result"}`}`);
    }
  }

  const checked = failures.join("; ");
  if (override !== null) {
    throw new LauncherError(
      `ZEUS_CODE_PYTHON=${JSON.stringify(override)} is not a usable Python 3.11+ executable with curses (${checked})`,
    );
  }
  throw new LauncherError(
    "Python 3.11 or newer with the curses module is required. " +
      `Checked python3 and python (${checked}). Install Python 3.11+ with curses, ` +
      "or set ZEUS_CODE_PYTHON to its executable.",
  );
}

function readRegularFile(filePath, label, maximumBytes) {
  let stats;
  try {
    stats = fs.lstatSync(filePath);
  } catch (error) {
    throw new LauncherError(`cannot read ${label} at ${filePath}: ${error.message}`, { cause: error });
  }
  if (stats.isSymbolicLink() || !stats.isFile()) {
    throw new LauncherError(`${label} is not a regular file: ${filePath}`);
  }
  if (stats.size > maximumBytes) {
    throw new LauncherError(`${label} is unexpectedly large (${stats.size} bytes)`);
  }
  try {
    return fs.readFileSync(filePath);
  } catch (error) {
    throw new LauncherError(`cannot read ${label} at ${filePath}: ${error.message}`, { cause: error });
  }
}

function parseJson(buffer, label) {
  try {
    const value = JSON.parse(buffer.toString("utf8"));
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
      throw new Error("the top-level value must be an object");
    }
    return value;
  } catch (error) {
    throw new LauncherError(`invalid ${label}: ${error.message}`, { cause: error });
  }
}

function parseChecksums(buffer) {
  const text = buffer.toString("ascii");
  const lines = text.split(/\n/);
  if (lines.length > 0 && lines[lines.length - 1] === "") {
    lines.pop();
  }
  const checksums = new Map();
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index].endsWith("\r") ? lines[index].slice(0, -1) : lines[index];
    const match = CHECKSUM_LINE.exec(line);
    if (!match) {
      throw new LauncherError(`invalid SHA256SUMS line ${index + 1}`);
    }
    const [, digest, name] = match;
    if (checksums.has(name)) {
      throw new LauncherError(`duplicate SHA256SUMS entry for ${name}`);
    }
    checksums.set(name, digest.toLowerCase());
  }
  if (
    checksums.size !== REQUIRED_CHECKSUMS.size ||
    [...REQUIRED_CHECKSUMS].some((name) => !checksums.has(name))
  ) {
    throw new LauncherError("SHA256SUMS must cover exactly release.json and zeus-code.pyz");
  }
  return checksums;
}

function sha256Buffer(buffer) {
  return crypto.createHash("sha256").update(buffer).digest("hex");
}

function sha256File(filePath) {
  const hash = crypto.createHash("sha256");
  const descriptor = fs.openSync(filePath, "r");
  const chunk = Buffer.allocUnsafe(1024 * 1024);
  try {
    for (;;) {
      const size = fs.readSync(descriptor, chunk, 0, chunk.length, null);
      if (size === 0) {
        break;
      }
      hash.update(chunk.subarray(0, size));
    }
  } finally {
    fs.closeSync(descriptor);
  }
  return hash.digest("hex");
}

function verifyBundledRelease(packageRoot = path.resolve(__dirname, "..")) {
  const packagePath = path.join(packageRoot, "package.json");
  const dist = path.join(packageRoot, "dist");
  const metadataPath = path.join(dist, "release.json");
  const manifestPath = path.join(dist, "SHA256SUMS");
  const bundlePath = path.join(dist, "zeus-code.pyz");

  const packageDocument = parseJson(
    readRegularFile(packagePath, "package.json", 256 * 1024),
    "package.json",
  );
  const manifest = parseChecksums(
    readRegularFile(manifestPath, "SHA256SUMS", 256 * 1024),
  );
  const metadataBytes = readRegularFile(metadataPath, "release.json", 64 * 1024);
  const expectedMetadataHash = manifest.get("release.json");
  if (sha256Buffer(metadataBytes) !== expectedMetadataHash) {
    throw new LauncherError("checksum mismatch for bundled release.json");
  }
  const metadata = parseJson(metadataBytes, "release.json");

  if (typeof packageDocument.version !== "string" || !STABLE_VERSION.test(packageDocument.version)) {
    throw new LauncherError(`package.json has an invalid stable version: ${JSON.stringify(packageDocument.version)}`);
  }
  if (metadata.version !== packageDocument.version) {
    throw new LauncherError(
      `release.json version ${JSON.stringify(metadata.version)} does not match package.json version ${JSON.stringify(packageDocument.version)}`,
    );
  }
  if (!Number.isSafeInteger(metadata.schema_version)) {
    throw new LauncherError("release.json schema_version must be an integer");
  }
  if (metadata.python_requires !== "3.11") {
    throw new LauncherError("release.json python_requires must be exactly \"3.11\"");
  }

  readRegularFile(bundlePath, "zeus-code.pyz", 128 * 1024 * 1024);
  const expectedBundleHash = manifest.get("zeus-code.pyz");
  let actualBundleHash;
  try {
    actualBundleHash = sha256File(bundlePath);
  } catch (error) {
    throw new LauncherError(`cannot hash bundled zeus-code.pyz: ${error.message}`, { cause: error });
  }
  if (actualBundleHash !== expectedBundleHash) {
    throw new LauncherError("checksum mismatch for bundled zeus-code.pyz");
  }

  const packageName = typeof packageDocument.name === "string" && !/[\r\n\0]/.test(packageDocument.name)
    ? packageDocument.name
    : "@kanterlabs/zeus-code";
  return Object.freeze({
    bundlePath,
    packageName,
    schemaVersion: metadata.schema_version,
    sha256: expectedBundleHash,
    version: metadata.version,
  });
}

function cacheHome(options = {}) {
  const env = options.env || process.env;
  const homedir = options.homedir || os.homedir();
  if (env.XDG_CACHE_HOME && path.isAbsolute(env.XDG_CACHE_HOME)) {
    return env.XDG_CACHE_HOME;
  }
  if (!homedir || !path.isAbsolute(homedir)) {
    throw new LauncherError("cannot determine the user home directory for the Zeus Code runtime cache");
  }
  return path.join(homedir, ".cache");
}

function verifyExistingCache(target, expectedHash) {
  let stats;
  try {
    stats = fs.lstatSync(target);
  } catch (error) {
    if (error.code === "ENOENT") {
      return false;
    }
    throw new LauncherError(`cannot inspect cached bundle ${target}: ${error.message}`, { cause: error });
  }
  if (stats.isSymbolicLink() || !stats.isFile()) {
    throw new LauncherError(`cached bundle is not a regular file; refusing to replace it: ${target}`);
  }
  let actualHash;
  try {
    actualHash = sha256File(target);
  } catch (error) {
    throw new LauncherError(`cannot verify cached bundle ${target}: ${error.message}`, { cause: error });
  }
  if (actualHash !== expectedHash) {
    throw new LauncherError(
      `cached bundle checksum mismatch; refusing to overwrite a possibly running bundle: ${target}`,
    );
  }
  return true;
}

function ensureCachedBundle(release, options = {}) {
  const root = options.cacheRoot || cacheHome(options);
  const runtimeDirectory = path.join(
    root,
    "zeus-code",
    "npm",
    `${release.version}-${release.sha256}`,
  );
  const target = path.join(runtimeDirectory, "zeus-code.pyz");
  try {
    fs.mkdirSync(runtimeDirectory, { mode: 0o700, recursive: true });
  } catch (error) {
    throw new LauncherError(`cannot create Zeus Code runtime cache ${runtimeDirectory}: ${error.message}`, { cause: error });
  }
  if (verifyExistingCache(target, release.sha256)) {
    return target;
  }

  const temporary = path.join(
    runtimeDirectory,
    `.zeus-code.pyz.${process.pid}.${crypto.randomBytes(8).toString("hex")}.tmp`,
  );
  try {
    fs.copyFileSync(release.bundlePath, temporary, fs.constants.COPYFILE_EXCL);
    if (sha256File(temporary) !== release.sha256) {
      throw new LauncherError("bundled zeus-code.pyz changed while it was copied; cache was not published");
    }
    fs.chmodSync(temporary, 0o500);
    const descriptor = fs.openSync(temporary, "r");
    try {
      fs.fsyncSync(descriptor);
    } finally {
      fs.closeSync(descriptor);
    }
    try {
      // A hard link publishes the fully written file atomically and cannot replace
      // an entry another launcher (or a running daemon) already owns.
      fs.linkSync(temporary, target);
    } catch (error) {
      if (error.code !== "EEXIST") {
        throw error;
      }
      verifyExistingCache(target, release.sha256);
    }
  } catch (error) {
    if (error instanceof LauncherError) {
      throw error;
    }
    throw new LauncherError(`cannot populate Zeus Code runtime cache ${target}: ${error.message}`, { cause: error });
  } finally {
    try {
      fs.unlinkSync(temporary);
    } catch (error) {
      if (error.code !== "ENOENT") {
        // Cache publication already succeeded or a more useful error is in flight.
      }
    }
  }
  verifyExistingCache(target, release.sha256);
  return target;
}

/** Mirror the Python CLI's global-option normalization for launch decisions. */
function analyzeArguments(argv) {
  const globals = [];
  const dataDirArguments = [];
  const rest = [];
  let hasHost = false;
  let index = 0;
  while (index < argv.length) {
    const argument = argv[index];
    if (argument === "--") {
      rest.push(...argv.slice(index));
      break;
    }
    if (argument === "--data-dir" || argument === "--host") {
      if (index + 1 >= argv.length) {
        rest.push(argument);
      } else {
        const pair = [argument, argv[index + 1]];
        globals.push(...pair);
        if (argument === "--data-dir") {
          dataDirArguments.push(...pair);
        } else {
          hasHost = true;
        }
        index += 1;
      }
    } else if (argument.startsWith("--data-dir=")) {
      globals.push(argument);
      dataDirArguments.push(argument);
    } else if (argument.startsWith("--host=")) {
      globals.push(argument);
      hasHost = true;
    } else {
      rest.push(argument);
    }
    index += 1;
  }
  const terminator = rest.indexOf("--");
  const beforeTerminator = terminator === -1 ? rest : rest.slice(0, terminator);
  const command = beforeTerminator.length > 0 && !beforeTerminator[0].startsWith("-")
    ? beforeTerminator[0]
    : null;
  const localDefault = rest.length === 0;
  const localConnect = rest.length === 1 && command === "connect";
  return Object.freeze({
    beforeTerminator,
    command,
    dataDirArguments,
    globals,
    hasHost,
    shouldBootstrap: !hasHost && (localDefault || localConnect),
  });
}

function bootstrapArguments(analysis) {
  return [...analysis.dataDirArguments, "serve", "--background"];
}

function rewriteUpdateArguments(argv, analysis, options = {}) {
  if (analysis.command !== "update") {
    return { argv: [...argv], notice: null };
  }
  const commandArguments = analysis.beforeTerminator.slice(1);
  const hasInstallDirectory = commandArguments.some(
    (argument) => argument === "--install-dir" || argument.startsWith("--install-dir="),
  );
  if (hasInstallDirectory) {
    return { argv: [...argv], notice: null };
  }
  const homedir = options.homedir || os.homedir();
  if (!homedir || !path.isAbsolute(homedir)) {
    throw new LauncherError("cannot determine ~/.local/bin for the standalone update");
  }
  const installDirectory = path.join(homedir, ".local", "bin");
  const packageName = options.packageName || "@kanterlabs/zeus-code";
  const version = options.version || "the packaged version";
  const checkOnly = commandArguments.includes("--check");
  return {
    argv: [...argv, "--install-dir", installDirectory],
    notice: checkOnly ? null :
      `zeus-code update from npx will install the standalone command in ${installDirectory}; ` +
      `this immutable npx bundle remains ${version}. Later npx launches use their packaged version. ` +
      `Use "npx ${packageName}@latest" or "npx github:KanterLabs/zeus-code" for the latest wrapper.\n`,
  };
}

function spawnPython(executable, bundle, argv, options = {}) {
  const spawnSync = options.spawnSync || childProcess.spawnSync;
  let result;
  try {
    result = spawnSync(executable, [bundle, ...argv], {
      cwd: options.cwd || process.cwd(),
      env: options.env || process.env,
      stdio: "inherit",
      windowsHide: false,
    });
  } catch (error) {
    throw new LauncherError(`could not start ${executable}: ${error.message}`, { cause: error });
  }
  if (result && result.error) {
    throw new LauncherError(`could not start ${executable}: ${result.error.message}`, { cause: result.error });
  }
  if (!result || (result.status === null && !result.signal)) {
    throw new LauncherError(`${executable} ended without an exit status`);
  }
  return { signal: result.signal || null, status: result.status };
}

function runLauncher(options = {}) {
  const argv = options.argv || process.argv.slice(2);
  if (!Array.isArray(argv) || argv.some((argument) => typeof argument !== "string")) {
    throw new TypeError("argv must be an array of strings");
  }
  const platform = options.platform || process.platform;
  const env = options.env || process.env;
  const homedir = options.homedir || os.homedir();
  const spawnSync = options.spawnSync || childProcess.spawnSync;
  const cwd = options.cwd || process.cwd();
  const stderr = options.stderr || process.stderr;
  checkPlatform(platform);

  const release = verifyBundledRelease(options.packageRoot || path.resolve(__dirname, ".."));
  const python = findPython({ env, platform, spawnSync });
  const bundle = ensureCachedBundle(release, {
    cacheRoot: options.cacheRoot,
    env,
    homedir,
  });
  const analysis = analyzeArguments(argv);

  if (analysis.shouldBootstrap) {
    const bootstrap = spawnPython(python, bundle, bootstrapArguments(analysis), {
      cwd,
      env,
      spawnSync,
    });
    if (bootstrap.status !== 0 || bootstrap.signal) {
      return bootstrap;
    }
  }

  const rewritten = rewriteUpdateArguments(argv, analysis, {
    homedir,
    packageName: release.packageName,
    version: release.version,
  });
  if (rewritten.notice) {
    stderr.write(rewritten.notice);
  }
  return spawnPython(python, bundle, rewritten.argv, { cwd, env, spawnSync });
}

function finishLikeChild(result) {
  if (result.signal) {
    try {
      process.kill(process.pid, result.signal);
      return;
    } catch (error) {
      const signalNumber = os.constants.signals[result.signal];
      process.exitCode = signalNumber ? 128 + signalNumber : 1;
      return;
    }
  }
  process.exitCode = result.status === null ? 1 : result.status;
}

function main() {
  try {
    finishLikeChild(runLauncher());
  } catch (error) {
    const detail = error && error.message ? error.message : String(error);
    process.stderr.write(`zeus-code: ${detail}\n`);
    process.exitCode = 1;
  }
}

module.exports = {
  LauncherError,
  analyzeArguments,
  bootstrapArguments,
  cacheHome,
  checkPlatform,
  ensureCachedBundle,
  findPython,
  finishLikeChild,
  main,
  parseChecksums,
  rewriteUpdateArguments,
  runLauncher,
  sha256File,
  spawnPython,
  verifyBundledRelease,
};

if (require.main === module) {
  main();
}
