# Claude Agent SDK (Python) — verified API notes

Verified 2026-10-30 against the docs below **and** against the installed package
`claude-agent-sdk 0.2.175` (field/class existence checked with `dataclasses.fields` / `hasattr`).
Anything not confirmed is in "Not documented / unverified" and must not be relied on.

Sources:

- Python reference: https://code.claude.com/docs/en/agent-sdk/python
- Permissions: https://code.claude.com/docs/en/agent-sdk/permissions
- Approvals / `can_use_tool`: https://code.claude.com/docs/en/agent-sdk/user-input
- Hooks: https://code.claude.com/docs/en/agent-sdk/hooks
- Using Claude Code features (settings, hooks, MCP): https://code.claude.com/docs/en/agent-sdk/claude-code-features
- Quickstart (install, auth): https://code.claude.com/docs/en/agent-sdk/quickstart
- Permission modes / auto mode: https://code.claude.com/docs/en/permission-modes

## 1. `query()` vs `ClaudeSDKClient`

```python
async def query(*, prompt: str | AsyncIterable[dict[str, Any]],
                options: ClaudeAgentOptions | None = None,
                transport: Transport | None = None) -> AsyncIterator[Message]
```

| | `query()` | `ClaudeSDKClient` |
|---|---|---|
| Session | new session per call | same session across calls |
| Interrupts | not supported | `interrupt()` |
| Options | `options=` | constructor |

`Message = UserMessage | AssistantMessage | SystemMessage | ResultMessage | StreamEvent | RateLimitEvent |
ConversationResetMessage`.

**Decision for the runner:** one orchestrator step = one `query()` call = one fresh session (O2). We do not need
`ClaudeSDKClient`.

## 2. `can_use_tool` needs streaming input (documented workaround)

`can_use_tool` is honoured by `query()` only when `prompt` is an **async iterable** (streaming input), and the docs
call a no-op `PreToolUse` hook a "required workaround" that keeps the stream open:

```python
async def dummy_hook(input_data, tool_use_id, context):
    return {"continue_": True}


async def prompt_stream():
    yield {"type": "user", "message": {"role": "user", "content": "..."}}


query(
    prompt=prompt_stream(),
    options=ClaudeAgentOptions(
        can_use_tool=cb, hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[dummy_hook])]}
    ),
)
```

Callback: `async (tool_name: str, input_data: dict, context: ToolPermissionContext) -> PermissionResultAllow | PermissionResultDeny`.

```python
PermissionResultAllow(behavior="allow", updated_input=None, updated_permissions=None)
PermissionResultDeny(behavior="deny", message="", interrupt=False)
```

`ToolPermissionContext`: `signal` (reserved), `suggestions`, `tool_use_id`, `agent_id`, `blocked_path`,
`decision_reason`, `title`, `display_name`, `description`. The callback may stay pending indefinitely (no timeout).

### The callback does NOT see every call

`can_use_tool` runs only at step 6 of the evaluation order:
**hooks → deny rules → ask rules → permission mode → allow rules → `can_use_tool`**.
A call approved by `allowed_tools`, an allow rule, `acceptEdits`, `bypassPermissions`, or the `auto` classifier never
reaches it. The docs' own advice: *"For checks that must run on every tool call, use a PreToolUse hook"* — a hook
`deny` applies in every mode (even `bypassPermissions`), and scoped `disallowed_tools` rules (e.g. `"Bash(rm *)"`)
also hold in every mode.

**Decision for `policy.py`:** one `policy.decide()` function, wired in **two** places:

1. a `PreToolUse` hook (runs on every call; this is the enforcement point and doubles as the "dummy hook" the stream
   workaround needs);
2. `can_use_tool` (catches whatever falls through to a prompt — no human is there, so it must answer immediately).

Hard stops additionally return `{"continue_": False, "stopReason": ...}` from the hook (both fields are in
`SyncHookJSONOutput`) and set a flag on the policy context so the runner reports `BLOCKED`.

## 3. Hooks (Python)

```python
HookCallback = Callable[[HookInput, str | None, HookContext], Awaitable[HookJSONOutput]]  # (input, tool_use_id, context)
@dataclass class HookMatcher: matcher: str | None = None; hooks: list[HookCallback]; timeout: float | None = None
HookEvent = Literal["PreToolUse","PostToolUse","PostToolUseFailure","UserPromptSubmit","Stop","SubagentStop",
                    "PreCompact","Notification","SubagentStart","PermissionRequest"]
```

- `SessionStart` / `SessionEnd` are **not** available as Python callback hooks (TypeScript only).
- `PreToolUse` input: `tool_name`, `tool_input`, `hook_event_name`, plus `session_id`, `cwd`; `agent_id`/`agent_type`
  when fired inside a subagent.
- Deny: `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "..."}}`.
  Allow: `{}`. `permissionDecision` ∈ `allow|deny|ask|defer`. Several hooks: `deny` > `defer` > `ask` > `allow`.
- Control fields: `continue_`, `stopReason`, `systemMessage`, `suppressOutput`, `decision` (`"block"`), `reason`.
- A `PreToolUse` hook that exceeds its timeout **blocks** the call (v2.1.210+); default timeout 600 s.
- Matchers match tool names only (`"Bash"`, `"Write|Edit"`, regex `"^mcp__"`), never paths or arguments — inspect
  `tool_input` inside the callback.
- A Bash hook does not see file writes done through other tools, and `Write|Edit` matchers do not see Bash writes:
  the policy must register for **all** tools (`matcher=None`) and handle each by name.

## 4. `permission_mode`

```python
PermissionMode = Literal["default", "acceptEdits", "plan", "dontAsk", "bypassPermissions", "auto"]
```

- **`"auto"` exists in the Python SDK** (readme §6 is right). A model classifier reviews actions. Availability:
  all plans; Anthropic API needs Opus 4.6+/Sonnet 4.6+/Haiku 5.5/a Fable model; if unavailable the session silently
  starts in `default` (Manual) mode instead of failing. Repeated classifier blocks (3 in a row / 20 total) pause auto
  mode; in non-interactive runs the blocked action simply does not run and the agent keeps working.
- With Claude Code ≥ 2.1.302 an SDK run with no mode set starts in `default`, where any call needing approval and
  matching no allow rule is **denied** unless a hook / `can_use_tool` approves it.
- `bypassPermissions` — never (project rule). `allowed_tools` does **not** restrict it (every tool is approved).
- `dontAsk` denies anything not pre-approved without calling `can_use_tool`.
- Switching mode mid-session only on `ClaudeSDKClient.set_permission_mode()`.

**Decision:** default `permission_mode="auto"` per readme §6, with the two-point policy enforcement above (so we
never rely on the classifier for our hard rules). Because `auto` can degrade to `default` silently, the runner
reads the first `SystemMessage` and logs the effective mode (see "unverified": the init message shape).

## 5. `ClaudeAgentOptions` fields used (all confirmed present in 0.2.175)

`permission_mode`, `can_use_tool`, `allowed_tools`, `disallowed_tools`, `max_turns`, `max_budget_usd`,
`cwd`, `setting_sources`, `mcp_servers`, `strict_mcp_config`, `env`, `resume`, `session_id`, `hooks`,
`stderr`, `cli_path`, `extra_args`, `model`, `tools`, `system_prompt`, `add_dirs`.

- `max_budget_usd`: stops at a **client-side estimate**; it "can exceed it, so leave headroom". Maps to the per-call
  budget (O12: $2). Hitting it yields `ResultMessage.subtype == "error_max_budget_usd"`.
- `max_turns`: `subtype == "error_max_turns"`; hooks may not fire when it is hit.
- `disallowed_tools=["Bash"]` removes the tool entirely; `"Bash(rm *)"` denies matching calls in every mode.
- `session_id` must be a valid UUID; cannot combine with `resume`/`continue_conversation` without `fork_session`.
  A fresh step does not set it (the SDK assigns one and we read it from the messages).
- `setting_sources=None` (default) loads `user`, `project`, `local` → `~/.claude/settings.json` hooks (the production-DB
  guard), project `.claude/settings.json` hooks, CLAUDE.md files and skills all apply to SDK sessions. `[]` disables
  them. `~/.claude.json` (global config, where user-scope MCP servers live) is read **regardless**; `strict_mcp_config=True`
  ignores everything but `mcp_servers`.
- Auto memory (`~/.claude/projects/<project>/memory/`) is loaded regardless of `setting_sources`.

## 6. Result, cost, session id, termination

```python
ResultMessage(
    subtype,
    duration_ms,
    duration_api_ms,
    is_error,
    num_turns,
    session_id,
    stop_reason,
    total_cost_usd,
    usage,
    result,
    structured_output,
    model_usage,
    permission_denials,
    deferred_tool_use,
    errors,
    api_error_status,
    uuid,
    terminal_reason,
    origin,
)
```

- `subtype`: `success`, `error_during_execution`, `error_max_turns`, `error_max_budget_usd`,
  `error_max_structured_output_retries`. `is_error` is always true on `error_*` but can also be true on `success`
  (check `stop_reason`, e.g. `"refusal"`).
- `terminal_reason`: e.g. `completed`, `max_turns`, `api_error`, `aborted_streaming`, `aborted_tools`.
- `total_cost_usd` is **cumulative** across turns in streaming-input mode → take the last `ResultMessage`, never sum.
- `permission_denials`: list of denied calls (input to `PERMISSION_DENIED` detection).
- `session_id` is on `ResultMessage`, `AssistantMessage.session_id` and `RateLimitEvent.session_id`.
- Since 0.2.140 a single-shot `query()` that ends in an error result **raises** `ResultError(ProcessError)` with
  `subtype`, `errors`, `result`, `api_error_status`, `terminal_reason`, `session_id`, `data`. Order to branch:
  `data["stop_reason"]` → `terminal_reason` → `subtype`. Other exceptions: `CLINotFoundError`, `CLIConnectionError`,
  `ProcessError(exit_code, stderr)`, `CLIJSONDecodeError`.
- `AssistantMessage.error: Literal["authentication_failed","billing_error","rate_limit","invalid_request","server_error","unknown"] | None`
  (the CLI may emit values outside this set, e.g. `max_output_tokens` → treat as `unknown`).

## 7. Quota / rate-limit signals (for the 3-layer detector)

```python
RateLimitStatus = Literal["allowed","allowed_warning","rejected"]
RateLimitType   = Literal["five_hour","seven_day","seven_day_opus","seven_day_sonnet","overage"]
RateLimitInfo(status, resets_at: int|None, rate_limit_type, utilization, overage_status,
              overage_resets_at, overage_disabled_reason, raw)
RateLimitEvent(rate_limit_info, uuid, session_id)            # a Message in the stream
```

Layers (Ralph order, structured first):

1. **Structured:** `RateLimitEvent.rate_limit_info.status == "rejected"` (use `resets_at` to size the wait),
   or `AssistantMessage.error == "rate_limit"`.
2. **Error text:** `ResultMessage.errors` / `result` / `ResultError` text matching rate-limit phrases, and
   `api_error_status == 429`.
3. **Timeout is never quota:** our own `asyncio` timeout (and `terminal_reason` of `aborted_*`) → `FAILED`
   with reason `timeout`.

`allowed_warning` is not a stop; record it as an event.

## 8. Authentication — **user decision taken in S3: `.env.local` key; see §11 for the billing blocker**

- The quickstart authenticates with `ANTHROPIC_API_KEY` (or Bedrock / Vertex / Foundry env vars) and states that
  *"Anthropic does not allow third party developers to offer claude.ai login or rate limits for their products,
  including agents built on the Claude Agent SDK. Use the API key authentication methods."*
- This machine: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, Bedrock and Vertex vars are
  **all unset**; `claude auth status` reports a claude.ai login. So the first real SDK call would either use the
  claude.ai login (against the stated guidance) or fail with an auth error.
- The SDK wheel bundles the Claude Code binary (`cli_path` overrides; sdist installs need `claude` on PATH).

## 9. Not documented / unverified (do not rely on)

- ~~The `SystemMessage` `init` subtype~~ — superseded by §11: `claude-agent-sdk 0.2.175` ships `SystemInitData`
  (`permissionMode`, `apiKeySource`, `mcp_servers`, `tools`) and the runner now logs it. `session_id` is still
  taken from `ResultMessage`/`AssistantMessage`.
- A Python equivalent of TypeScript `permissionPrompts: 'none'` / CLI `--permission-prompts` (readme §6). Not in the
  Python reference, nor on the permission-modes page. `ClaudeAgentOptions.permission_prompt_tool_name` and
  `extra_args` exist but their semantics are not documented here. **Not used.**
- Whether `api_error_status` is an HTTP status code (assumed, not stated).
- Whether the user-scope `memory` MCP server in `~/.claude.json` actually starts in an SDK session (docs say
  `~/.claude.json` is read regardless; behaviour is for the S3 pilot to confirm — readme §10.5).
- Exact wording of rate-limit error text → layer 2 patterns are heuristics and must be tested against real output
  before being trusted.

## 10. Observed in real runs (2026-10-30, toy repo only)

claude-agent-sdk 0.2.175 with Claude Code 2.1.283, authenticated by the machine's existing claude.ai login
(no API key; see §8 for the docs' stance). Three real `run_step` calls, `setting_sources=[]`,
`strict_mcp_config=True`, `permission_mode="auto"`, cap $0.50 per call:

| Scenario | Outcome | Reported cost | Wall time |
|---|---|---|---|
| Fix `calc.divide` per TASK.md, write `handoff.md` | `DONE`; gates passed (`python3 test_calc.py` exit 0, `STATUS: DONE`) | $0.159 | ~18 s |
| Prompt asks for `git push origin main` | policy deny recorded; the denial text reached the model; step `DONE` | $0.043 | ~7 s |
| Prompt asks for `yarn migrate:production` | `BLOCKED` ("migrate:production ... hard stop"); command never ran | $0.038 | ~7 s |

Confirmed in practice: streaming `prompt` + `can_use_tool` + the catch-all `PreToolUse` hook coexist; the hook runs
under `permission_mode="auto"`; a hook deny is visible to the model; `ResultMessage.session_id` and
`total_cost_usd` are populated.

Still not observed: which mode was effective (`auto` vs the silent fallback to `default`), whether
`permission_denials` lists hook denials (the runner therefore only treats entries it did not issue as foreign),
the `RateLimitEvent` / error-text shapes for a genuine quota stop (all three quota layers are tested with scripted
SDK messages only), and `ResultError` behaviour in streaming mode.

## 11. Observed in S3 (2026-10-30; claude-agent-sdk 0.2.175, Claude Code 2.1.283, codex-cli 0.147.0)

All of this is from real calls on the toy repo / a scratch directory; nothing touched qinno.

**Effective permission mode is observable**: `SystemMessage(subtype="init").data["permissionMode"]`
(`SystemInitData`). The runner stores it as a `session_init` event and adds a `notice` if it differs from the
request.

| configuration | requested -> effective |
|---|---|
| no `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`, claude.ai login | `plan`->`plan`, `auto`->`auto`, `acceptEdits`->`acceptEdits` |
| `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` (any other options, with or without `allowed_tools`) | **everything -> `default`** |

The CLI says why: *"Permission mode forced to default - CLAUDE_CODE_SUBPROCESS_ENV_SCRUB is set
(allowed_non_write_users hardening)"*. So `auto` exists and works; the silent fallback seen earlier was caused by the
scrub flag. Decision: the flag is set only when `.env.local` supplies an API key (`envfile.sdk_env`), and the plan
step is made read-only by `PolicyContext.read_only` instead of relying on SDK `plan` mode.

**`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` works for credential-class variables**: with a fake `AWS_SECRET_ACCESS_KEY`
and a plain variable in `ClaudeAgentOptions.env`, the agent's `Bash` saw only the plain one with the flag and both
without it. It was **not** possible to check `ANTHROPIC_API_KEY` itself this way: see the billing note below.

**API-key auth**: with `.env.local`'s key the init message reports `apiKeySource: "ANTHROPIC_API_KEY"` (a name, not
the value), and the CLI prints that claude.ai connectors are disabled while a key is set. The first call then fails
with `AssistantMessage.error == "billing_error"`, `ResultMessage(subtype="success", is_error=True,
api_error_status=400, result="Credit balance is too low")`, followed by a raised `ResultError`. The runner already
maps `billing_error` to `FAILED` ("retrying cannot help"). **A successful key-authenticated call has not been
observed.** The same prompt on the claude.ai login worked.

**A trivial call costs ~$0.08-0.10** (system prompt + tools), so `max_budget_usd=0.05` ends with
`error_max_budget_usd` after one reply (cost reported 0.098).

**MCP in headless sessions**: user-scope `mcpServers` in `~/.claude.json` is empty on this machine; `memory`,
`codebase-memory`, `codex`, `mcplens` are configured **per project path** (`projects["<cwd>"].mcpServers`). A session
with `cwd` = a project that has them and `setting_sources=None` lists `memory` in `init.mcp_servers` (status
`pending` at init) and a real `mcp__memory__open_nodes` call succeeded and ran. A temp-dir cwd (the toy repo) gets no
`memory` server; `setting_sources=[]` gets none at all. Plugin servers (slack, linear, ...) show `needs-auth` /
`failed` and are denied by the policy anyway. The count of `server-memory` processes (22, all from other sessions) was the same before and after these probes.
**Not observed**: a session with `cwd` = the qinno path (that is the pilot).

**Real quota stop** (captured from an interactive Claude Code 2.1.295 transcript of 2026-10-11, not from an SDK run):
the assistant entry has `error: "rate_limit"`, `apiError: "usage_limit_reached"`, `apiErrorStatus: 429`, text
`"You've hit your session limit · resets 8pm (Asia/Ulaanbaatar)"` and
`rate_limit_info = {status: "rejected", resetsAt: 1791720000, rateLimitType: "five_hour", overageStatus:
"rejected", overageDisabledReason: "out_of_credits", isUsingOverage: false}` (camelCase on the wire; the SDK parser
maps it to `RateLimitInfo`). Layer 2's regex did **not** match that text before S3; `hit your (\w+ )?limit` and
`usage_limit_reached` were added and tests use these exact shapes. Still unseen: the final `ResultMessage`/exception
text of a quota stop in an SDK run.

**Codex `--json`**: one event per line, see `orchestrator/codex.py` docstring; no dollar cost, only token `usage`.
An untracked build artefact written by Codex (`__pycache__/*.pyc` in a repo without `.gitignore`) counts as a change
outside the allowlist, which is correct; the toy fixture now has a `.gitignore`.

**Policy findings from real agent behaviour** (events in the toy run): the model tries heredocs (`cat > f <<'EOF'`)
a lot and is denied (it falls back to the Write tool; costs a turn each time), and `for f in plan/*.md; do ...`
is denied by the wildcard heuristic ("may touch protected paths"), also costing turns. Both are conservative
behaviours inherited from S2, not bugs.
