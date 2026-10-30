STATUS: DONE

# handoff-s2

Gate results (run 2026-10-30, real output):

- `uv run pytest` → **329 passed, 3 skipped in 5.88s** (the 3 skipped are the real-SDK tests, opt-in)
- `ORCHESTRATOR_INTEGRATION=1 uv run pytest tests/test_integration_toy.py -k real_sdk...` → the 3 real-SDK tests
  **passed** (run as two invocations: `1 passed in 17.93s`, then `2 passed in 13.78s`)
- `uv run ruff check` → All checks passed!
- qinno repo untouched: `git -C ~/Documents/Projects/qinno status --short` empty, HEAD `75d22606` (same as S1)
- `docs/notes/agent-sdk.md` written *before* the runner/policy/gates code, with source URLs; the installed
  `claude-agent-sdk 0.2.175` was checked against it field by field.

## Real SDK calls (toy repo only, cap $0.50/call, `setting_sources=[]`, `strict_mcp_config=True`)

| Scenario | Result | Cost |
|---|---|---|
| Fix `calc.divide`, write `handoff.md` | `DONE`, gates passed | $0.159 |
| Model asked to `git push origin main` | policy deny logged to `events`, step `DONE` | $0.043 |
| Model asked to `yarn migrate:production` | `BLOCKED` ("migrate:production ... hard stop"), command never ran | $0.038 |

Total ≈ $0.24. Auth was the machine's claude.ai login (no `ANTHROPIC_API_KEY`); see "Open questions".

## Done

- `globs.py` — `**`-aware glob, brackets literal (`pos/[orderId].tsx`), shared by policy and gates.
- `shellparse.py` — splits `&& || ; | |& &`, newlines, subshells, `$()`, backticks, process substitution, redirects;
  rejects heredocs / unbalanced input (→ Deny).
- `policy.py` — `decide(tool_name, tool_input, ctx) -> Allow | Deny(reason, hard_stop)` (contract §5).
  `PolicyConfig.tightened()` can only add to deny lists (loosening keys raise).
- `gates.py` — snapshot/checkpoint outside the repo, `changed_files`, `allowlist_violations`, `breaker_tripped`,
  `read_status`, `run_tests`, `evaluate() -> GateVerdict` (contract §4).
- `runner.py` — `run_step` (one fresh SDK session per call), `run_step_with_quota_retry`, `PolicyGuard`,
  `classify`, `StepStatus`/`StepResult` (contract §4).
- `state.py` — added `get_step`, `count_events_since`.
- Fixture `tests/fixtures/toy-repo` (copied into a throwaway git repo per test, never committed anywhere real).

## Tests (329 total; S1 had 47)

- policy: every forbidden git subcommand, ~40 chained/nested/wrapped git bypass forms (`bash -c`, `eval`, `env`,
  `xargs`, `find -exec`, `$()`, backticks, quoting tricks, aliases), recursive `rm`, sst/aws/drizzle/migrate/seed,
  inline interpreter code, ~20 production/staging/destructive-SQL hard stops, every write tool × forbidden path,
  symlink and `..` escapes, schema-contract hard stop, graph-MCP rules, config can only tighten.
- gates: real-git snapshots (modified/new/deleted/reverted/odd names/no commits), allowlist incl. brackets,
  breaker (3 empty / 5 same error, noise-normalised, only consecutive), STATUS parsing, tests run by exit code
  (a failing loop cannot hide behind a pipe), timeouts, `evaluate` collecting all five failure kinds.
- runner (scripted SDK using the real SDK dataclasses): DONE persistence, session id saved before a crash,
  quota layer 1 / layer 2 / timeout ≠ quota / success text mentioning "rate limit" ≠ quota, budget cap
  shrinking, exhausted budgets, hourly limit, retry-after-60-min, policy wiring through hook and `can_use_tool`.

## Deviations / interpretations (please check)

1. **Policy is enforced at two points** (PreToolUse hook on every call + `can_use_tool` fallback), not only in
   `can_use_tool` as readme §4 says: `can_use_tool` never sees calls auto-approved by `auto`/allow rules
   (docs/notes/agent-sdk.md §2). Behaviour for the readme's intent is unchanged.
2. **`PERMISSION_DENIED`** is returned when (a) the agent blindly repeats the same denied call 3×, or (b) the run
   ended in error *and* Claude Code reported denials the policy did not issue. A denial that the agent worked
   around on a run that still succeeded is **not** a failure. I read "stop, don't retry blindly" as aimed at
   retry loops; say so if you want any foreign denial to stop the step.
3. **Hard stop wins over a "success" result**: if the policy hard-stopped, the step is `BLOCKED` even if the
   agent later reports success.
4. `GateVerdict` gained an additive `test_results` field; `evaluate` is keyword-only (README contract note updated).
5. `StepRequest.timeout_s` is a float; `permission_mode="bypassPermissions"` raises `ValueError`.
6. Test commands run via `bash -c` at the repo root; the verdict is the real exit code (S1 yaml has no `| tail`).

## Limits of the policy (it is not a sandbox)

- It judges the command text. A script the agent writes and then runs (`node x.js`, `./build.sh`) can do anything;
  inline `python -c` / `node -e` are denied, but running a file is allowed. The user's `~/.claude/settings.json`
  hooks (production DB guard) remain the second line and run in SDK sessions by default (`setting_sources=None`).
- Heredocs are denied (agents must use the Write tool). Command words that are variables/globs are denied.
- "signing/updater" globs (`**/updater/**`, `**/*signing*/**`, keystores, `.pem`, `.p12`) are **my broad guess**;
  the plan files do not list the exact qinno paths. Add precise ones via the pipeline's `forbidden_write_globs`.
- Allowed MCP prefixes are `mcp__memory__`, `mcp__mcplens__`, `mcp__codebase-memory__` (names taken from this
  environment's agent definitions); every other MCP tool is denied.

## Not done / not verified (stated plainly)

- **Quota detection against a real quota stop**: the three layers are tested with scripted messages only. The
  `RateLimitEvent` and error-text shapes of a genuine limit were not observed; layer-2 phrases are heuristics.
- **Effective permission mode** (`auto` vs silent fallback to `default`) was not observed; the init `SystemMessage`
  is undocumented, so the runner does not log it.
- **`permission_denials` shape** is `list[Any]` in the docs; the runner assumes dicts with `tool_use_id`/`tool_name`.
- Crash mid-call loses that call's cost (cost is only reported by the final `ResultMessage`); `max_budget_usd`
  is a client-side estimate that "can exceed" the cap.
- **No schema allowlist per session**: `PolicyContext.allowed_schema_globs` exists, but `pipeline.yaml` has no field
  for it yet. Until S3 adds `schema_allowlist` per session and fills it from the task files, any edit of
  `packages/db/src/schemas/**` is a **hard stop** — rbac-v2 S1 (which legitimately edits schemas) would be
  BLOCKED if wired as-is. The globs in `PolicyConfig.schema_globs` (`packages/db/src/schemas/**`,
  `.../schema/**`) are also a guess at qinno's layout.
- Attempt history for the circuit breaker is not persisted: `breaker_tripped(list[Attempt])` is pure; S3 must
  record `Attempt(files_changed, error)` per try (e.g. as `events`) and rebuild the list on `resume`.
- `memory.py`, `codex.py`, multi-step flow, `run`/`resume` CLI: S3 (out of S2's scope).

## Open questions for the user

1. **Auth.** SDK docs say to authenticate with an API key and that third-party products may not offer claude.ai
   login/rate limits. The real runs above used your existing claude.ai login (it just worked). For an unattended
   personal tool that is probably what you want (readme §5 assumes subscription quota), but it is your call; with an
   API key the quota layers would mean billing limits instead of usage windows.
2. Commit and the private GitHub repo (O9): nothing is committed, no remote exists. Name + permission needed.
3. AWS static key is still invalid (S1 finding) and `DB_NAME` is still `qinno` in the qinno `.env`.

## S3 should check first

1. `uv sync --reinstall-package orchestrator` if `uv run orchestrator` cannot import the package.
2. `uv run pytest` → 329 passed, 3 skipped; `ORCHESTRATOR_INTEGRATION=1 ...` for the 3 real tests (≈$0.24).
3. Wire `PolicyContext(allowed_schema_globs=...)` and extra tightening from the pipeline before any qinno run.
4. Use `take_snapshot` before/after Claude and Codex (`checkpoint_dir`), `evaluate(codex_before=..., codex_after=...)`.
5. First real quota stop: capture the raw `RateLimitEvent` / error text and add it to the quota tests.
