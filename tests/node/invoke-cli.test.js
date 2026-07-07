const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const repoRoot = path.resolve(__dirname, "..", "..");
const binPath = path.join(repoRoot, "bin", "invoke.js");

function isolatedEnv() {
  return {
    ...process.env,
    INVOKE_HOME: fs.mkdtempSync(path.join(os.tmpdir(), "invoke-home-")),
    INVOKE_BASE_URL: "",
    INVOKE_API_KEY: "",
  };
}

test("invoke npm bin delegates to the Python wrapper generator", () => {
  const output = fs.mkdtempSync(path.join(os.tmpdir(), "invoke-npm-"));
  const result = spawnSync(
    process.execPath,
    [
      binPath,
      "wrap",
      "postgresql",
      "--query",
      "SELECT * FROM users WHERE id = :user_id",
      "--name",
      "user lookup",
      "--output",
      output,
    ],
    {
      cwd: repoRoot,
      encoding: "utf8",
    }
  );

  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /"success": true/);
  assert.ok(fs.existsSync(path.join(output, "user-lookup", "invoke.register.json")));
});

test("agentify compatibility subcommand is stripped before delegation", () => {
  const result = spawnSync(process.execPath, [binPath, "agentify", "--help"], {
    cwd: repoRoot,
    encoding: "utf8",
  });

  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /Execution reliability infrastructure/);
});

test("status shows local config context without requiring credentials", () => {
  const result = spawnSync(process.execPath, [binPath, "status"], {
    cwd: repoRoot,
    env: isolatedEnv(),
    encoding: "utf8",
  });

  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /Invoke CLI status/);
  assert.match(result.stdout, /Runtime: https:\/\/api.invokehq.run/);
  assert.match(result.stdout, /API key: \(not set\)/);
});

test("call without a tool prints actionable usage", () => {
  const result = spawnSync(process.execPath, [binPath, "call"], {
    cwd: repoRoot,
    env: isolatedEnv(),
    encoding: "utf8",
  });

  assert.equal(result.status, 2);
  assert.match(result.stderr, /Missing tool/);
  assert.match(result.stderr, /invoke call <tool>/);
});

test("tools without credentials explains the login fix", () => {
  const result = spawnSync(process.execPath, [binPath, "tools"], {
    cwd: repoRoot,
    env: isolatedEnv(),
    encoding: "utf8",
  });

  assert.equal(result.status, 2);
  assert.match(result.stderr, /Missing API key/);
  assert.match(result.stderr, /invoke login --api-key/);
});

test("config accepts launch-friendly hyphenated keys", () => {
  const env = isolatedEnv();
  const setBaseUrl = spawnSync(
    process.execPath,
    [binPath, "config", "base-url", "https://api.invokehq.run"],
    {
      cwd: repoRoot,
      env,
      encoding: "utf8",
    }
  );
  const setApiKey = spawnSync(process.execPath, [binPath, "config", "api-key", "ag_live_test_1234"], {
    cwd: repoRoot,
    env,
    encoding: "utf8",
  });
  const status = spawnSync(process.execPath, [binPath, "status"], {
    cwd: repoRoot,
    env,
    encoding: "utf8",
  });

  assert.equal(setBaseUrl.status, 0, setBaseUrl.stderr);
  assert.equal(setApiKey.status, 0, setApiKey.stderr);
  assert.match(status.stdout, /Runtime: https:\/\/api.invokehq.run/);
  assert.match(status.stdout, /API key: ag_live_...1234/);
});

test("common typo alias calls routes to call usage", () => {
  const result = spawnSync(process.execPath, [binPath, "calls"], {
    cwd: repoRoot,
    env: isolatedEnv(),
    encoding: "utf8",
  });

  assert.equal(result.status, 2);
  assert.match(result.stderr, /Missing tool/);
  assert.match(result.stderr, /invoke call <tool>/);
});

function envWithKey() {
  return { ...isolatedEnv(), INVOKE_API_KEY: "inv_test_offline_key", INVOKE_WORKSPACE: "" };
}

test("top-level help groups commands by the five layers", () => {
  const result = spawnSync(process.execPath, [binPath, "--help"], {
    cwd: repoRoot,
    env: isolatedEnv(),
    encoding: "utf8",
  });

  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /commands, by layer:/);
  for (const layer of ["Identity", "Context", "Coordination", "Execution", "Observability"]) {
    assert.match(result.stdout, new RegExp(`${layer} —`));
  }
});

test("layers command explains the five layers", () => {
  const result = spawnSync(process.execPath, [binPath, "layers"], {
    cwd: repoRoot,
    env: isolatedEnv(),
    encoding: "utf8",
  });

  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /1\. Identity/);
  assert.match(result.stdout, /3\. Coordination/);
  assert.match(result.stdout, /5\. Observability/);
});

test("auth is an alias for login", () => {
  const result = spawnSync(process.execPath, [binPath, "auth", "--help"], {
    cwd: repoRoot,
    env: isolatedEnv(),
    encoding: "utf8",
  });

  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /--api-key/);
});

test("run without a workspace explains how to provision one", () => {
  const result = spawnSync(process.execPath, [binPath, "run", "support-agent"], {
    cwd: repoRoot,
    env: envWithKey(),
    encoding: "utf8",
  });

  assert.equal(result.status, 2);
  assert.match(result.stderr, /No Invoke workspace selected/);
  assert.match(result.stderr, /invoke init/);
});

test("logs without a workspace explains how to provision one", () => {
  const result = spawnSync(process.execPath, [binPath, "logs"], {
    cwd: repoRoot,
    env: envWithKey(),
    encoding: "utf8",
  });

  assert.equal(result.status, 2);
  assert.match(result.stderr, /No Invoke workspace selected/);
});
