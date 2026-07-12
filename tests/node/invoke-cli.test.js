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

test("top-level help leads with the builder path, then groups by layer", () => {
  const result = spawnSync(process.execPath, [binPath, "--help"], {
    cwd: repoRoot,
    env: isolatedEnv(),
    encoding: "utf8",
  });

  assert.equal(result.status, 0, result.stderr);
  // The builder path is the headline: eight commands in install -> prove order.
  const path = result.stdout.indexOf("the builder path");
  const byLayer = result.stdout.indexOf("commands, by layer:");
  assert.ok(path >= 0 && byLayer > path, "builder path should precede the layer grouping");
  for (const cmd of ["login", "init", "deploy", "run", "inspect", "receipts", "graph", "replay"]) {
    assert.match(result.stdout, new RegExp(`\\b${cmd}\\b`));
  }
  // Layers with commands are shown; Context/Coordination are runtime-only now.
  for (const layer of ["Identity", "Execution", "Observability"]) {
    assert.match(result.stdout, new RegExp(`${layer} —`));
  }
  // The deprecated tier collapses to one line and points at the web console.
  assert.match(result.stdout, /deprecated \(still work/);
  assert.match(result.stdout, /invokehq\.run\/dashboard/);
});

test("deprecated commands are hidden from the command list but still parse", () => {
  const result = spawnSync(process.execPath, [binPath, "--help"], {
    cwd: repoRoot,
    env: isolatedEnv(),
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  const listing = result.stdout.split("the builder path")[0];
  // e.g. "    approvals   List..." must NOT appear in the argparse command list.
  for (const cmd of ["approvals", "preflight", "execute", "workflow"]) {
    assert.doesNotMatch(listing, new RegExp(`\\n\\s+${cmd}\\s`));
  }
});

test("a deprecated command prints where it moved, then still runs", () => {
  // `call` (no tool) fails at its own usage check before any network, so we see both
  // the demotion note and the original handler running — proof it is demoted, not removed.
  const result = spawnSync(process.execPath, [binPath, "call"], {
    cwd: repoRoot,
    env: envWithKey(),
    encoding: "utf8",
  });
  assert.equal(result.status, 2);
  assert.match(result.stderr, /note: call one tool through the governed gateway/);
  assert.match(result.stderr, /Missing tool/);
});

test("the four builder-path commands are registered and parse", () => {
  for (const cmd of ["inspect", "receipts", "graph", "replay"]) {
    const result = spawnSync(process.execPath, [binPath, cmd, "--help"], {
      cwd: repoRoot,
      env: isolatedEnv(),
      encoding: "utf8",
    });
    assert.equal(result.status, 0, `${cmd} --help failed: ${result.stderr}`);
    assert.match(result.stdout, new RegExp(`usage: invoke ${cmd}`));
  }
});

test("receipts --verify exposes the ledger/receipt verification flag", () => {
  const result = spawnSync(process.execPath, [binPath, "receipts", "--help"], {
    cwd: repoRoot,
    env: isolatedEnv(),
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /--verify/);
});

test("trace and watch alias onto inspect and logs", () => {
  // trace -> inspect, watch -> logs; both resolve a workspace, so with a key but no
  // workspace they hit the same "select a workspace" error as their targets.
  for (const alias of ["trace", "watch"]) {
    const result = spawnSync(process.execPath, [binPath, alias], {
      cwd: repoRoot,
      env: envWithKey(),
      encoding: "utf8",
    });
    assert.equal(result.status, 2, `${alias} should reach its target handler`);
    assert.match(result.stderr, /No Invoke workspace selected/);
  }
});

test("layers command includes the new builder-path commands", () => {
  const result = spawnSync(process.execPath, [binPath, "layers"], {
    cwd: repoRoot,
    env: isolatedEnv(),
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  for (const cmd of ["inspect", "receipts", "graph", "replay"]) {
    assert.match(result.stdout, new RegExp(`\\b${cmd}\\b`));
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

test("generated wrapper server.py is valid Python (real booleans, not JSON true/false)", () => {
  // Regression guard: server_template() once embedded json.dumps() output, so the
  // generated TOOLS table held bare true/false/null and every server.py raised
  // NameError on import. py_compile passed (valid syntax) and missed it, so this
  // parses the TOOLS literal with ast.literal_eval — which rejects true/false/null
  // but accepts True/False/None — without needing fastapi/httpx installed.
  const output = fs.mkdtempSync(path.join(os.tmpdir(), "invoke-wrap-py-"));
  for (const args of [
    ["wrap", "postgresql", "--query", "SELECT 1", "--name", "probe", "--output", output],
    ["wrap", "github", "--output", output],
  ]) {
    const gen = spawnSync(process.execPath, [binPath, ...args], { cwd: repoRoot, encoding: "utf8" });
    assert.equal(gen.status, 0, gen.stderr);
  }

  const assertScript = [
    "import ast, glob, os, sys",
    "files = glob.glob(os.path.join(sys.argv[1], '**', 'server.py'), recursive=True)",
    "assert files, 'no server.py generated'",
    "for f in files:",
    "    src = open(f, encoding='utf-8').read()",
    "    block = src.split('TOOLS = ', 1)[1].split(chr(10) + 'app = FastAPI', 1)[0].strip()",
    "    tools = ast.literal_eval(block)  # true/false/null -> ValueError here",
    "    assert isinstance(tools, dict) and tools, f'empty TOOLS in {f}'",
    "    assert 'True' in repr(tools) or 'False' in repr(tools), f'no python bool in {f}'",
    "print('OK', len(files), 'wrapper(s) parsed')",
  ].join("\n");
  const scriptPath = path.join(output, "_assert_tools.py");
  fs.writeFileSync(scriptPath, assertScript);

  const python = process.env.INVOKE_PYTHON || (process.platform === "win32" ? "python" : "python3");
  const check = spawnSync(python, [scriptPath, output], { encoding: "utf8" });
  assert.equal(check.status, 0, check.stderr || check.stdout);
  assert.match(check.stdout, /OK 2 wrapper\(s\) parsed/);
});

test("wrap postgres shorthand routes to the PostgreSQL wrapper, not generic HTTP", () => {
  // A bare `wrap postgres` must reach the postgres path (which requires --query),
  // not silently fall through to the generic HTTP wrapper.
  const result = spawnSync(process.execPath, [binPath, "wrap", "postgres"], {
    cwd: repoRoot,
    env: isolatedEnv(),
    encoding: "utf8",
  });
  assert.equal(result.status, 2);
  assert.match(result.stderr, /postgresql wrappers require --query/);
});

test("wrap refuses a data-modifying CTE as read-only", () => {
  // WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x starts with WITH but writes;
  // it must not be classified read-only (requires --allow-write).
  const result = spawnSync(
    process.execPath,
    [binPath, "wrap", "postgresql", "--query",
      "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x", "--name", "cte"],
    { cwd: repoRoot, env: isolatedEnv(), encoding: "utf8" }
  );
  assert.equal(result.status, 2);
  assert.match(result.stderr, /read-only by default|allow-write/);
});

test("gateway --help lists the governed MCP-gateway subcommands", () => {
  const result = spawnSync(process.execPath, [binPath, "gateway", "--help"], {
    cwd: repoRoot,
    env: isolatedEnv(),
    encoding: "utf8",
  });

  assert.equal(result.status, 0, result.stderr);
  for (const sub of ["create", "use", "url", "connect", "tools", "call", "status"]) {
    assert.match(result.stdout, new RegExp(`\\b${sub}\\b`));
  }
});

test("gateway call without a tool prints actionable usage", () => {
  const result = spawnSync(process.execPath, [binPath, "gateway", "call"], {
    cwd: repoRoot,
    env: envWithKey(),
    encoding: "utf8",
  });

  assert.equal(result.status, 2);
  assert.match(result.stderr, /Missing tool/);
  assert.match(result.stderr, /invoke gateway call <tool>/);
});

test("gateway url without an active gateway workspace explains how to select one", () => {
  // Credentials are present (envWithKey) but no gateway workspace is set, so the
  // gateway-workspace resolver — not the runtime one — must complain.
  const result = spawnSync(process.execPath, [binPath, "gateway", "url"], {
    cwd: repoRoot,
    env: { ...envWithKey(), INVOKE_GATEWAY_WORKSPACE: "" },
    encoding: "utf8",
  });

  assert.equal(result.status, 2);
  assert.match(result.stderr, /No active gateway workspace/);
  assert.match(result.stderr, /invoke gateway create/);
});

test("gateway --help lists the new observability subcommands (receipts, logs)", () => {
  const result = spawnSync(process.execPath, [binPath, "gateway", "--help"], {
    cwd: repoRoot,
    env: isolatedEnv(),
    encoding: "utf8",
  });

  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /receipts/);
  assert.match(result.stdout, /logs/);
});

test("login rejects a duplicated/garbled API key instead of silently saving it", () => {
  const result = spawnSync(
    process.execPath,
    [binPath, "login", "--base-url", "https://x.test", "--api-key", "ag_live_ABC\x16ag_live_ABC"],
    { cwd: repoRoot, env: isolatedEnv(), encoding: "utf8" }
  );

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /duplicated|garbled/i);
});

test("login strips stray control characters from a single pasted key", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "invoke-home-"));
  const env = { ...process.env, INVOKE_HOME: home, INVOKE_BASE_URL: "", INVOKE_API_KEY: "" };
  const login = spawnSync(
    process.execPath,
    [binPath, "login", "--base-url", "https://x.test", "--api-key", "  ag_live_CLEAN123\t"],
    { cwd: repoRoot, env, encoding: "utf8" }
  );
  assert.equal(login.status, 0, login.stderr);
  const stored = JSON.parse(fs.readFileSync(path.join(home, "config.json"), "utf8"));
  assert.equal(stored.apiKey, "ag_live_CLEAN123");
});

test("config api-key masks by default and --reveal prints it in full", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "invoke-home-"));
  const env = { ...process.env, INVOKE_HOME: home, INVOKE_BASE_URL: "", INVOKE_API_KEY: "" };
  spawnSync(process.execPath, [binPath, "login", "--base-url", "https://x.test", "--api-key", "ag_live_SECRET1234567890"],
    { cwd: repoRoot, env, encoding: "utf8" });

  const masked = spawnSync(process.execPath, [binPath, "config", "api-key"], { cwd: repoRoot, env, encoding: "utf8" });
  const revealed = spawnSync(process.execPath, [binPath, "config", "api-key", "--reveal"], { cwd: repoRoot, env, encoding: "utf8" });

  assert.equal(masked.status, 0, masked.stderr);
  assert.match(masked.stdout, /ag_live_\.\.\.7890/);
  assert.doesNotMatch(masked.stdout, /SECRET/);
  assert.match(revealed.stdout, /ag_live_SECRET1234567890/);
});

test("runtime observability commands redirect a gateway (ws_) workspace id", () => {
  for (const cmd of ["receipts", "logs", "graph", "inspect", "replay"]) {
    const result = spawnSync(process.execPath, [binPath, cmd, "--workspace", "ws_deadbeef"], {
      cwd: repoRoot,
      env: envWithKey(),
      encoding: "utf8",
    });
    assert.equal(result.status, 2, `${cmd}: ${result.stdout}${result.stderr}`);
    assert.match(result.stderr, /gateway \(effect-ledger\) workspace/, cmd);
    assert.match(result.stderr, /invoke gateway/, cmd);
  }
});
