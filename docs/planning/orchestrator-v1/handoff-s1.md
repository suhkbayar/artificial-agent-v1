STATUS: DONE

# handoff-s1

Gate results (run 2026-10-30, all real output):

- `uv run pytest` → 47 passed in 1.15s
- `uv run ruff check` → All checks passed!
- `uv run orchestrator preflight --pipeline pipelines/qinno-rbac-v2.yaml` → exit 1 (see below; a failing
  preflight is the correct result while DB_NAME is production)
- `uv run orchestrator plan pipelines/qinno-rbac-v2.yaml` → exit 0, S1→S6, 226 lines
- `uv run orchestrator status` → exit 0, "no runs recorded (state.db does not exist yet)"

## Decisions from the user (readme.md §2, O9-O12; graph: `Decision: Agent Orchestrator — S1 open questions (O9-O12)`)

- O9 repo: private, personal account. **Remote not created, nothing committed** (asks pending, see below).
- O10 runtime: Mac host. O11 channel: web dashboard only. O12 budget: $2 call / $10 session / $100 total.
- `brew install uv` (0.13.6) approved by the user and done; uv was not installed before.

## Done

- `pyproject.toml` (hatchling, click + pyyaml; dev pytest + ruff), flat package `orchestrator/` (readme §4 layout).
- `config.py` — TOML/env, defaults from O12, checkpoint dir `~/.agent-orchestrator/checkpoints` (outside any repo).
- `state.py` — contract §3 tables `runs/steps/decisions/events`, `PRAGMA user_version` = 1, newer-schema refusal,
  repo API: `start_run/finish_run/ensure_step/update_step/last_unfinished_step/add_event/add_decision/session_cost`.
  `update_step(add_cost_usd=...)` also rolls the cost up into `runs.total_cost_usd`.
- `preflight.py` — 9 checks returning `CheckResult(name, ok, detail, fix_hint)`; `run_command` kills the whole
  process group on timeout. Every check unit-tested with a fake runner; the `.env` check is proven read-only.
- `pipeline.py` — `Pipeline`/`SessionSpec`, topological `order()`, errors for cycles / duplicate ids / unknown deps /
  unknown post_steps / missing task files (all listed at once), `todos()`.
- `pipelines/qinno-rbac-v2.yaml` — S1..S6 from the qinno task files (read-only).
- CLI: `preflight`, `plan`, `status`, plus `post-steps`.

## Deviations from the contract / task file

1. **`tests` carry no `| tail -N`.** The task files pipe every verify command through `2>&1 | tail -N`, which makes
   the exit code `tail`'s. The yaml keeps the bare command; S2's gate must run them with captured output and use the
   real exit code. Shell loops end in `|| exit 1` (task files only `echo`), so a missing locale key fails the gate.
2. **`post_steps` is a registry of names** (`pipeline.POST_STEPS`: drizzle push + four per-system seeds, all
   develop2-only; the full `seed` is not representable). Contract §2 only showed `drizzle_push_develop2`. Running
   them is S3 (`run_post_step` does not exist yet).
3. **Notification channel (O11):** the user chose "web dashboard only", but the dashboard is post-MVP. MVP notices =
   `logs/` + `events` table. I did **not** add macOS notifications. Tell me if you want them as an interim.
4. `S1.post_steps` contains the four seeds as well as the push: `s1-claude-foundation.md` T2 requires
   `seed:manager|finance|customer|merchant` on develop2 to populate `roles.key`. README of this plan said only
   "S1 = drizzle push"; the task file has priority.
5. S2 and S4 list `seed_merchant_develop2` twice on purpose: the task files require a second run reporting 0 added /
   0 removed. S3 must treat a repeated name as "run again", and the gate must read the second run's output.
6. Memory MCP health check is a stub (always ok) until `memory.py` (S3).

## TODOs left in the yaml (not guessed)

- `S6.handoff`: rbac-v2 README lists no handoff for S6 ("—"). What file should the S6 gate read?
- S5 codex verification `grep -n "ROUTE_PATHS.ROLES" ... # no PLACEHOLDER_ROUTES entry left` has no exit-code
  meaning; omitted from `tests`.
- `memory_entities` is just `["Decision: RBAC v2"]` for all six sessions (qinno plan README line 7). The task files
  have no per-session `open_nodes` list; readme §7 also names `Core Domain` / `Qinno Backend Architecture` as
  examples; those were not added.

## Findings from the real preflight (user action needed before any pilot)

- `DB_NAME=qinno is PRODUCTION` in `services/migrate-service/.env` → `npm run sync-secrets -- --stage=develop2`
  (user). Unchanged from 2026-10-29.
- **AWS static key is invalid/expired**: `aws configure list` shows a static credential, but
  `aws sts get-caller-identity` → `InvalidClientTokenId`. New finding; the readme §6 note says the key was checked on
  2026-10-29. The user needs to rotate/refresh the key before any unattended run.
- Passing: `.sst/stage=develop2`, `codex exec` replied ok, claude 2.1.283, codex 0.147.0, qinno on
  `feature-rbac-v2@75d22606` clean, AC power + caffeinate.
- The `codex exec ... "Reply ok"` check makes a real (tiny) Codex call on every preflight.

## Open items for the user

- GitHub remote: private repo on the personal account needs a name; creating it requires a first commit and push.
  Not done; will ask before doing either.

## S2 should check first

1. `uv sync --reinstall-package orchestrator` if `uv run orchestrator` says "No module named orchestrator" (the first
   editable install happened before the package dir existed; already fixed in this checkout).
2. `uv run pytest` (47 green) and `git status` (nothing is committed; all of this is untracked).
3. The `Budget` dataclass is in `orchestrator/config.py`; `Pipeline.budget` and `load_config().budget` share it.
4. Step status strings in `state.py` are lowercase (`pending|running|done|blocked|failed|quota_wait|
   permission_denied`); `OPEN_STATUSES` (pending/running/quota_wait) is what `resume` continues from; `blocked`,
   `failed` and `permission_denied` need a human.
