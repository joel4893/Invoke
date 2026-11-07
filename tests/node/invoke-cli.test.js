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

test("top-level help leads with the gateway path, then groups by layer", () => {
  const result = spawnSync(process.execPath, [binPath, "--help"], {
    cwd: repoRoot,
    env: isolatedEnv(),
    encoding: "utf8",
  });

  assert.equal(result.status, 0, result.stderr);
  // The gateway path is the headline, before the layer grouping.
  const start = result.stdout.indexOf("start here");
  const byLayer = result.stdout.indexOf("commands, by layer:");
  assert.ok(start >= 0 && byLayer > start, "gateway path should precede the layer grouping");
  for (const cmd of ["login", "gateway create", "gateway connect", "gateway url", "gateway tools",
                     "gateway call", "gateway receipts", "gateway logs"]) {
    assert.ok(result.stdout.slice(start, byLayer).includes(cmd), `${cmd} missing from the gateway path`);
  }
  // How an agent connects, with both auth headers.
  assert.match(result.stdout, /X-API-Key/);
  assert.match(result.stdout, /Authorization: Bearer/);
  for (const layer of ["Identity", "Context", "Coordination", "Execution", "Observability"]) {
    assert.match(result.stdout, new RegExp(`${layer} —`));
  }
  // No deprecated tier in the help any more, and the console link is one that resolves.
  assert.doesNotMatch(result.stdout, /deprecated \(still work/);
  assert.match(result.stdout, /console\.invokehq\.run\/sign-in/);
  assert.doesNotMatch(result.stdout, /exactly-once/);
});

test("retired and deprecated commands are hidden from the command list", () => {
  const result = spawnSync(process.execPath, [binPath, "--help"], {
    cwd: repoRoot,
    env: isolatedEnv(),
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  const listing = result.stdout.split("start here")[0];
  // e.g. "    run   Run an agent..." must NOT appear in the argparse command list.
  for (const cmd of ["approvals", "preflight", "execute", "workflow", "init", "run", "logs", "inspect",
                     "receipts", "graph", "replay", "deploy", "dev", "agents"]) {
    assert.doesNotMatch(listing, new RegExp(`\\n\\s+${cmd}\\s`), cmd);
  }
  // ...while what still works stays listed.
  for (const cmd of ["login", "status", "doctor", "config", "gateway", "wrap", "layers"]) {
    assert.match(listing, new RegExp(`\\n\\s+${cmd}\\s`), cmd);
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

// A dead port: a command that tried the network would fail with a connection error, not
// with the retired-command answer these tests expect.
function offlineEnv() {
  return { ...envWithKey(), INVOKE_BASE_URL: "http://127.0.0.1:9" };
}

test("retired commands answer with their replacement and never reach the network", () => {
  const replacements = {
    init: /invoke gateway create/,
    run: /invoke gateway url/,
    logs: /invoke gateway logs/,
    inspect: /invoke gateway receipts/,
    receipts: /invoke gateway receipts/,
    graph: /web console: https:\/\/console\.invokehq\.run/,
    replay: /--idempotency-key/,
  };
  for (const [cmd, replacement] of Object.entries(replacements)) {
    for (const argv of [[cmd], [cmd, "--help"], [cmd, "x", "--workspace", "ws_deadbeef"]]) {
      const result = spawnSync(process.execPath, [binPath, ...argv], {
        cwd: repoRoot,
        env: offlineEnv(),
        encoding: "utf8",
      });
      const said = `${argv.join(" ")}: ${result.stdout}${result.stderr}`;
      assert.equal(result.status, 2, said);
      assert.match(result.stderr, new RegExp("`" + cmd + "` was retired in 0\\.6"), said);
      assert.match(result.stderr, replacement, said);
      assert.doesNotMatch(result.stderr, /Connection refused|URLError|Could not reach/i, said);
      assert.equal(result.stdout, "", said);
    }
  }
});

test("gateway receipts --verify exposes the receipt verification flag", () => {
  const result = spawnSync(process.execPath, [binPath, "gateway", "receipts", "--help"], {
    cwd: repoRoot,
    env: isolatedEnv(),
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /--verify/);
});

test("trace and watch still alias onto inspect and logs, which say what replaced them", () => {
  for (const [alias, target] of [["trace", "inspect"], ["watch", "logs"]]) {
    const result = spawnSync(process.execPath, [binPath, alias], {
      cwd: repoRoot,
      env: offlineEnv(),
      encoding: "utf8",
    });
    assert.equal(result.status, 2, `${alias}: ${result.stderr}`);
    assert.match(result.stderr, new RegExp("`" + target + "` was retired in 0\\.6"));
  }
});

test("layers lists the gateway commands and none of the retired ones", () => {
  const result = spawnSync(process.execPath, [binPath, "layers"], {
    cwd: repoRoot,
    env: isolatedEnv(),
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  for (const cmd of ["gateway create", "gateway url", "gateway connect", "gateway call", "gateway receipts"]) {
    assert.ok(result.stdout.includes(cmd), cmd);
  }
  for (const cmd of ["init", "inspect", "graph", "replay", "deploy"]) {
    assert.doesNotMatch(result.stdout, new RegExp(`(commands: |, )${cmd}\\b`), cmd);
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

test("deprecated project commands still run, with a pointer to the gateway", () => {
  const result = spawnSync(process.execPath, [binPath, "agents", "list"], {
    cwd: repoRoot,
    env: isolatedEnv(),
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stderr, /note: .*invoke gateway status/);
  assert.match(result.stdout, /No Invoke projects deployed/);

  const deploy = spawnSync(process.execPath, [binPath, "deploy", os.tmpdir()], {
    cwd: repoRoot,
    env: offlineEnv(),
    encoding: "utf8",
  });
  assert.equal(deploy.status, 2, deploy.stderr);
  assert.match(deploy.stderr, /note: the local project flow/);
  assert.match(deploy.stderr, /invoke gateway connect/);
  assert.doesNotMatch(deploy.stderr, /invoke init/);
});

test("status points at the gateway, never at invoke init", () => {
  const result = spawnSync(process.execPath, [binPath, "status"], {
    cwd: repoRoot,
    env: envWithKey(),
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /invoke gateway create/);
  assert.doesNotMatch(result.stdout + result.stderr, /invoke init/);

  const pinned = spawnSync(process.execPath, [binPath, "status"], {
    cwd: repoRoot,
    env: { ...envWithKey(), INVOKE_GATEWAY_WORKSPACE: "ws_pinned" },
    encoding: "utf8",
  });
  assert.equal(pinned.status, 0, pinned.stderr);
  assert.match(pinned.stdout, /Workspace: ws_pinned/);
  assert.match(pinned.stdout, /invoke gateway status/);
});

test("gateway url tells you how to send the key, without touching the network", () => {
  const result = spawnSync(process.execPath, [binPath, "gateway", "url", "--workspace", "ws_test"], {
    cwd: repoRoot,
    env: offlineEnv(),
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout.trim(), "http://127.0.0.1:9/v1/workspaces/ws_test/mcp");
  assert.match(result.stderr, /X-API-Key/);
  assert.match(result.stderr, /Authorization: Bearer/);
  assert.match(result.stderr, /claude mcp add --transport http invoke http:\/\/127\.0\.0\.1:9\/v1\/workspaces\/ws_test\/mcp/);
});

test("--version reports 0.6.0", () => {
  const result = spawnSync(process.execPath, [binPath, "--version"], {
    cwd: repoRoot,
    env: isolatedEnv(),
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout + result.stderr, /0\.6\.0/);
});

test("gateway --help lists the context and coordination subcommands", () => {
  const result = spawnSync(process.execPath, [binPath, "gateway", "--help"], {
    cwd: repoRoot,
    env: isolatedEnv(),
    encoding: "utf8",
  });

  assert.equal(result.status, 0, result.stderr);
  for (const sub of ["context", "subscribe", "broadcast", "inbox"]) {
    assert.match(result.stdout, new RegExp(sub), sub);
  }
});

test("layers now attributes commands to Context and Coordination", () => {
  const result = spawnSync(process.execPath, [binPath, "layers"], {
    cwd: repoRoot,
    env: isolatedEnv(),
    encoding: "utf8",
  });

  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /Context[\s\S]*commands: gateway context/);
  assert.match(result.stdout, /Coordination[\s\S]*gateway subscribe/);
});

test("gateway broadcast rejects a non-JSON payload with actionable usage", () => {
  const result = spawnSync(
    process.execPath,
    [binPath, "gateway", "broadcast", "research", "not-json", "--workspace", "ws_x"],
    { cwd: repoRoot, env: envWithKey(), encoding: "utf8" }
  );

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /payload must be valid JSON/);
});

test("gateway --help lists the shared-memory subcommand", () => {
  const result = spawnSync(process.execPath, [binPath, "gateway", "--help"], {
    cwd: repoRoot, env: isolatedEnv(), encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /memory/);
});

test("gateway memory --set without a key prints actionable usage", () => {
  const result = spawnSync(
    process.execPath,
    [binPath, "gateway", "memory", "--set", "hello", "--workspace", "ws_x"],
    { cwd: repoRoot, env: envWithKey(), encoding: "utf8" }
  );
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /Provide a key/);
  assert.match(result.stderr, /gateway memory <key> --set/);
});

test("layers attributes gateway memory to the Context layer", () => {
  const result = spawnSync(process.execPath, [binPath, "layers"], {
    cwd: repoRoot, env: isolatedEnv(), encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /Context[\s\S]*gateway memory/);
});

// A fake gateway that records the JSON-RPC calls the CLI sends. The CLI runs with an async
// spawn: spawnSync would block this process, and with it the server.
async function againstFakeGateway(argv) {
  const http = require("node:http");
  const seen = [];
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (chunk) => { body += chunk; });
    req.on("end", () => {
      const msg = JSON.parse(body || "{}");
      seen.push({ headers: req.headers, msg });
      res.setHeader("content-type", "application/json");
      res.end(JSON.stringify({ jsonrpc: "2.0", id: msg.id ?? 1,
        result: { content: [{ type: "text", text: "ok" }] } }));
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const env = { ...envWithKey(), INVOKE_BASE_URL: `http://127.0.0.1:${server.address().port}` };
  const result = await new Promise((resolve) => {
    const child = require("node:child_process").spawn(process.execPath, [binPath, ...argv], { cwd: repoRoot, env });
    let stdout = ""; let stderr = "";
    child.stdout.on("data", (d) => { stdout += d; });
    child.stderr.on("data", (d) => { stderr += d; });
    child.on("close", (status) => resolve({ status, stdout, stderr }));
  });
  server.close();
  return { result, calls: seen.filter((s) => s.msg.method === "tools/call") };
}

test("gateway call names no agent unless asked, so an agent's own key works", async () => {
  const plain = await againstFakeGateway(["gateway", "call", "invoke_recall", "{}", "--workspace", "ws_test"]);
  assert.equal(plain.result.status, 0, plain.result.stderr);
  assert.equal(plain.calls.length, 1);
  assert.equal(plain.calls[0].msg.params.name, "invoke_recall");
  assert.ok(!("_agent_id" in plain.calls[0].msg.params.arguments), JSON.stringify(plain.calls[0].msg));
  assert.equal(plain.calls[0].headers["x-api-key"], "inv_test_offline_key");

  const named = await againstFakeGateway(["gateway", "call", "invoke_recall", "{}", "--workspace", "ws_test",
                                          "--agent-id", "coder"]);
  assert.equal(named.result.status, 0, named.result.stderr);
  assert.equal(named.calls[0].msg.params.arguments._agent_id, "coder");
});
