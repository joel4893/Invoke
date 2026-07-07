#!/usr/bin/env node

const { spawn, spawnSync } = require("node:child_process");
const path = require("node:path");

const cliPath = path.resolve(__dirname, "..", "agentify.py");
const packageJson = require(path.resolve(__dirname, "..", "package.json"));

function candidates() {
  if (process.env.INVOKE_PYTHON) {
    return [{ command: process.env.INVOKE_PYTHON, args: [] }];
  }
  if (process.env.AGENTGATE_PYTHON) {
    return [{ command: process.env.AGENTGATE_PYTHON, args: [] }];
  }

  if (process.platform === "win32") {
    return [
      { command: "py", args: ["-3"] },
      { command: "python", args: [] },
      { command: "python3", args: [] },
    ];
  }

  return [
    { command: "python3", args: [] },
    { command: "python", args: [] },
  ];
}

function findPython() {
  for (const candidate of candidates()) {
    const check = spawnSync(candidate.command, [...candidate.args, "--version"], {
      encoding: "utf8",
      stdio: "pipe",
    });
    if (check.status === 0) {
      return candidate;
    }
  }
  return null;
}

function normalizeArgs(argv) {
  if (argv[0] === "agentify") {
    return argv.slice(1);
  }
  return argv;
}

function versionRequested(argv) {
  if (argv.includes("--version") || argv.includes("-V")) {
    return true;
  }

  // npm/npx consumes flags before the package command in calls like:
  //   npx -y @invokehq/cli@0.2.19 --version
  // It still executes the package with no argv, while exposing the consumed
  // flag through npm_config_version.
  return argv.length === 0 && process.env.npm_config_version === "true";
}

const args = normalizeArgs(process.argv.slice(2));

if (versionRequested(args)) {
  console.log(`invoke ${packageJson.version}`);
  process.exit(0);
}

const python = findPython();

if (!python) {
  console.error(
    "invoke: Python 3 is required. Install Python 3 or set INVOKE_PYTHON to its executable path."
  );
  process.exit(127);
}

const child = spawn(python.command, [...python.args, cliPath, ...args], {
  stdio: "inherit",
});

child.on("error", (error) => {
  console.error(`invoke: failed to start Python: ${error.message}`);
  process.exit(127);
});

child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code ?? 1);
});
