# Agent Orchestrator v1 MVP — хэрэгжүүлэх төлөвлөгөө (3 сесс)

Дизайны эх сурвалж: [`/readme.md`](../../../readme.md) (§1-§10, шийдвэр O1-O8). Энэ файл = **ажлын
хуваарилалт + сесс хоорондын гэрээ**. Дизайны шийдвэрийг энд дахин нээхгүй; зөрчил гарвал
`readme.md`-г эх сурвалж гэж үзээд хэрэглэгчээс асуу.

Хамрах хүрээ = `readme.md` §10.3-ийн **v1 MVP** + §10.4 unit тест + §10.5 pilot. Dashboard,
`mcp_server.py`, Docker, Telegram/утасны суваг, Playwright = **MVP-ийн дараа** (энэ төлөвлөгөөнд орохгүй).

## Файлууд

| Сесс | Task файл                    | Гол модулиуд                                          | Сессийн төгсгөлд |
| ---- | ---------------------------- | ----------------------------------------------------- | ---------------- |
| S1   | `s1-foundation.md`           | scaffold, `config`, `state.py`, `preflight.py`, `pipeline.py` + `qinno-rbac-v2.yaml` | `handoff-s1.md`  |
| S2   | `s2-execution-core.md`       | `runner.py`, `policy.py`, `gates.py`                  | `handoff-s2.md`  |
| S3   | `s3-integration-pilot.md`    | `codex.py`, `memory.py`, сессийн урсгал ①-⑤, CLI, pilot | `handoff-s3.md`  |

Сесс бүрийг **шинэ Claude Code сесст** (хэрэглэгч сэрүүн, интерактив) тухайн task файлаар
эхлүүлнэ. Orchestrator өөрөө хараахан байхгүй тул эдгээр 3 сесс хүнтэй явна.

`handoff-sN.md`-ийн эхний мөр `STATUS: DONE` эсвэл `STATUS: BLOCKED`. Дараа нь: юу хийсэн,
гэрээнээс хазайсан зүйл, нээлттэй асуудал, дараагийн сесс юуг эхэлж шалгах вэ. (Энэ нь
orchestrator-ийн өөрийнх нь gate-ийн формат — §4 ⑤.)

## Хамаарал

```
S1 ──► S2 ──► S3
 │      │      └─ pilot: qinno-rbac-v2 --only S1 (хэрэглэгч сэрүүн үед)
 │      └─ gates/policy/runner нь гэрээ §3-§5-ийг хэрэгжүүлнэ
 └─ гэрээ §2 (pipeline.yaml), §3 (state.db)-ийг эзэмшинэ
```

Дараалал заавал: S2 нь S1-ийн `state.py`, `pipeline.py`-д, S3 нь S2-ийн `runner`/`gates`/`policy`-д тулгуурлана.

## Сесс тус бүрийн хамрах хүрээ (товч)

- **S1 — Суурь.** §3-ийн 4 нээлттэй асуулт → `uv` төсөл, бүтэц §4 → `config` + `state.py`
  (SQLite) → `preflight.py` (§6-ийн бүх шалгалт) → `pipeline.py` (plan хавтас + yaml → DAG) →
  `pipelines/qinno-rbac-v2.yaml` → CLI араг (`preflight`, `plan`, `status`).
- **S2 — Гүйцэтгэлийн цөм.** Agent SDK API-г docs-оос баталгаажуулах → `runner.py` (нэг алхам =
  нэг шинэ сесс, квот таних, timeout, зардал) → `policy.py` (`can_use_tool`, хатуу зогсолт) →
  `gates.py` (snapshot, checkpoint, allowlist, circuit breaker, STATUS). Бүгдийг qinno биш,
  **тоглоом fixture repo** дээр шалгана.
- **S3 — Нэгтгэл ба pilot.** `codex.py` → `memory.py` (lock, backup, pending) → сессийн урсгал
  ①-⑤ (`pipeline` + `runner` + `gates`) → CLI `run`/`resume`/`--only` → pilot `--only S1`.

## Сесс хоорондын гэрээ

Модуль бүр зөвхөн эдгээр интерфейсээр харилцана. Нэр/талбарыг өөрчлөх шаардлага гарвал
энэ файлыг шинэчилж, `handoff-sN.md`-д тэмдэглэ.

### §1 Төсөл, хэрэгсэл

- Python 3.12+, `uv`, `pytest`, `ruff`. Package: `orchestrator/` (readme §4-ийн бүтэц).
- CLI нэвтрэх цэг: `uv run orchestrator <command>`.
- Config = өгөгдөл (YAML/TOML) — shell `source` хэзээ ч (Ralph v0.11).
- Gitignore: `state.db`, checkpoint хавтас, `.env*`.

### §2 `pipeline.yaml` схем (S1 эзэмшинэ)

```yaml
name: qinno-rbac-v2
repo: ~/Documents/Projects/qinno          # агент ажиллах working tree
plan_dir: docs/planning/rbac-v2           # repo-д харьцангуй
budget_usd: { per_call: ?, per_session: ?, total: ? }   # §3.4-ийн хариулт (S1)
sessions:
  - id: S1
    claude_task: s1-claude-foundation.md
    codex_task: s1-codex-role-keys.md     # байхгүй бол null
    handoff: handoff-s1.md
    depends_on: []
    codex_allowlist: ["packages/common/src/**", "..."]   # codex task файлаас
    tests: ["yarn ... test"]              # gate: бүгд ногоон
    post_steps: [drizzle_push_develop2]   # O4; зөвхөн develop2, preflight дахин шалгана
    memory_entities: ["Decision: RBAC v2", "Qinno Backend Architecture"]
```

`pipeline.py` нь үүнийг `Pipeline` / `SessionSpec` dataclass болгож, `depends_on`-оор
топологийн дараалал гаргана (цикл → алдаа).

*(S3 нэмсэн талбарууд — бүгд нэмэлт, хуучин yaml хэвээр ажиллана:)*

```yaml
requires_stage: true        # false = toy pipeline: DB_NAME/.sst/AWS preflight алгасна, post_steps хориглоно
policy: { forbidden_write_globs: [...], forbidden_commands: [...] }   # зөвхөн чангатгана; сулруулах түлхүүр = алдаа
open_items: ["TODO ..."]    # төлөвлөгөөний файлд байхгүй, таамаглаагүй утгууд -> `plan` командад TODO гэж гарна
sessions:
  - id: S1
    schema_allowlist: [packages/db/src/schemas/core/roles.ts]   # Claude "Ownership" блокоос; байхгүй бол schema засвар = hard stop
    policy: { ... }         # энэ сесст нэмэх чангатгал
```

### §3 `state.db` (S1 эзэмшинэ, бусад нь зөвхөн `state.py`-аар)

Хамгийн бага хүснэгтүүд: `runs` (pipeline, эхэлсэн, төлөв, нийт зардал), `steps` (run, session_id
(S1..), phase ①-⑤, төлөв, Claude `session_id`, оролдлого, зардал, сүүлийн алдаа, checkpoint зам),
`decisions` (AUTONOMOUS-DECISIONS.md-ийн мөр бүр), `events` (квот хүлээлт, blocker, gate үр дүн).
Явцын төлвийг **orchestrator эзэмшинэ**; агент зөвхөн `STATUS:` мөр/`passes` мэдээлнэ (Anthropic harness).
Тасарвал `resume` нь `steps`-ээс үргэлжилнэ.

*(S3: schema **v2** — `PRAGMA user_version` 1→2 автомат migration: `runs.pipeline_path` (resume pipeline-аа олно),
`attempts(run_id, session_id, phase, files_changed, error)` (circuit breaker-ийн түүх; resume-д сэргээнэ).
Хэрэглээ: `StateDB.add_attempt / attempts_for / set_run_status / reset_step / decision_questions`.
Шинэ `events.kind`: `session_init`, `codex_call`, `codex_fallback`, `post_step`, `post_step_output`,
`graph_write`, `session_done`, `run_start`, `run_resume`, `notice`.)*

### §4 Алхмын үр дүн (S2 эзэмшинэ)

```python
class StepStatus(StrEnum):
    DONE, BLOCKED, FAILED, QUOTA_WAIT, PERMISSION_DENIED


@dataclass
class StepResult:
    status: StepStatus
    session_id: str | None
    cost_usd: float
    reason: str | None  # BLOCKED/FAILED-ийн шалтгаан (хүнд уншигдахаар)
```

`gates.evaluate(...) -> GateVerdict(passed: bool, failures: list[str])` — тест, STATUS, allowlist,
checkpoint, circuit breaker-ийг нэг дор.
*(S3: `StepResult` нь нэмэлт `output: str | None` талбартай — агентын эцсийн мессеж (plan / contract), secret-гүй.)*
*(S2: `GateVerdict` нь нэмэлт `test_results` талбартай; `evaluate` нь keyword-only —
`session, repo, handoff, checkpoint, attempts, codex_before, codex_after, run, test_timeout`.)*

### §5 Policy (S2 эзэмшинэ)

`policy.decide(tool_name, tool_input, ctx) -> Allow | Deny(reason, hard_stop: bool)`. `hard_stop=True`
→ алхам `BLOCKED` + мэдэгдэл; сохроор дахин оролдохгүй. Дүрмүүд = readme §6 (хориглох, хатуу зогсолт).
Дүрмүүдийг кодонд hard-code биш, `policy` config-оос уншиж болохоор (pipeline тус бүр нэмж чангатгана,
сулруулахгүй).

### §6 Сессийн урсгал, CLI, memory (S3 эзэмшинэ)

- `flow.run_pipeline(pipeline, config, deps, options, run_id=None, db=None) -> RunOutcome`. `FlowDeps` =
  `memory`, `sdk_env`, `query_fn`, `runner`, `sleep`, `now`, `preflight` (бүгд тестэд inject хийнэ).
  Phase 1..5 = `steps.phase`; сесс тус бүрийн артефакт `<checkpoint_dir>/<pipeline>/run-<id>/<session>/`.
- `codex.run_codex_with_fallback(...) -> CodexOutcome` (2 алдаа → Claude авна), `memory.MemoryStore`.
- CLI: `run <pipeline> [--only S1]`, `status`, `resume [--retry]`, `preflight`, `plan`.
  `resume` = дуусаагүй (`pending|running|quota_wait`) алхмаас; `blocked|failed|permission_denied` = хүн
  засаад `--retry`.
- `.env.local` (төслийн root): `envfile.sdk_env()` зөвхөн SDK-ийн `env`-д; `os.environ`, лог, state.db, event,
  алдааны текстэд хэзээ ч орохгүй (`envfile.redact`).

## Нээлттэй асуултууд (S1-ийн эхэнд хэрэглэгчээс)

readme §3-ийн 4 асуулт. Төлөвлөгөөний таамаглал (хэрэглэгч өөрөөр шийдвэл task файлыг шинэчил):

1. Repo — GitHub remote үүсгэх нь гадагш нийтлэх тул **зөвшөөрлийн дараа л**. Local git аль хэдийн бий.
2. Агент **Mac host** дээр (санал). Docker = post-MVP.
3. Утасны суваг MVP-д **байхгүй** — мэдэгдэл = macOS notification + `logs/`. Dashboard/Telegram = дараа.
4. Мөнгөн хязгаар (нэг дуудлага / сесс / нийт) — тоо **заавал** (S2-ийн `runner` ашиглана).

## Memory graph

- Entity нэр төслийн `CLAUDE.md`-ийн дүрмээр ("Agent Orchestrator" prefix). Сессийн эхэнд
  `open_nodes(["Agent Orchestrator"])`.
- S1-ийн эхэнд root entity байхгүй бол үүсгэ (+ `confidence.jsonl` мөр тэр ээлжиндээ).
- Сесс бүрийн төгсгөлд батлагдсан шийдвэрийг (§3-ийн хариултууд, гэрээний өөрчлөлт) graph-д бич.
  Ажиллагааны төлөв graph-д орохгүй (readme §7).

## Codex

Энэ төслийн ихэнх нь logic-heavy (policy, gates, квот таних) → анхдагчаар **Claude хийнэ**
(`~/.claude/CLAUDE.md`: эргэлзвэл өөрөө). Codex-ийг S3-т `codex.py`-ийн бодит smoke тестэд л
ажиллуулна. Том механик хэсэг гарвал `codex-delegate` skill-ээр ангил.
