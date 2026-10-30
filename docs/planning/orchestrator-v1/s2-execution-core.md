# S2 — Гүйцэтгэлийн цөм: runner, policy, gates

Гэрээ: [`README.md`](README.md) §3-§5. Дизайн: [`/readme.md`](../../../readme.md) §4, §5 (🔴/🟡), §6.

## Эхлэхийн өмнө

1. `handoff-s1.md` → `STATUS: DONE` эсэх; хазайлт/TODO-г уншина. `git diff --stat` + `uv run pytest`
   (smoke) — дараа нь л шинэ ажил.
2. Graph: `open_nodes(["Agent Orchestrator"])` + S1-д үүссэн шийдвэрүүд.
3. **Agent SDK API-г таамаглахгүй.** `claude-agent-sdk` (Python)-ийн docs
   (https://code.claude.com/docs/en/agent-sdk)-оос баталгаажуулж, үр дүнг `docs/notes/agent-sdk.md`-д бич:
   - `query()` ба `ClaudeSDKClient`-ийн ялгаа; `can_use_tool` ямар горимд ажилладаг (streaming шаардлага).
   - `permission_mode`-ийн боломжит утгууд — readme §6-ийн `"auto"` SDK-д байгаа эсэх; байхгүй бол
     хамгийн ойр хувилбар + `can_use_tool`, хэрэглэгчид мэдэгдэнэ. `bypassPermissions` **хэзээ ч**.
   - Үр дүнгийн мессежийн `session_id`, зардал, дуусгавар төрөл (амжилт / max turns / алдаа) талбарууд.
   - Квот/rate limit-ийн дохио SDK-ийн stream-д ямар хэлбэрээр ирдэг.
   - Хэрэглэгчийн `~/.claude/settings.json` hook-ууд, local MCP (memory) SDK сесст ачаалагдах тохиргоо.

## Ажлууд

### 1. `runner.py` — нэг алхам = нэг шинэ сесс (O2)

- Оролт: prompt, working dir, allowed tools, policy, budget, timeout. Гаралт: гэрээ §4 `StepResult`.
- `session_id`, зардал, оролдлогыг `state.py`-аар тэр даруй бичнэ (тасарвал `resume`).
- **Квот таних 3 давхарга** (Ralph): SDK-ийн бүтэцтэй rate-limit дохио → алдааны текст → timeout-ийг
  квот гэж **андуурахгүй**. Квот → `QUOTA_WAIT`, 60 мин хүлээгээд дахин (event бичнэ).
- Дуудлагын timeout, цагт дуудах хязгаар, budget (дуудлага/сесс/нийт — S1-ийн тоо) давбал зогсоно.
- Permission татгалзагдвал `PERMISSION_DENIED` — сохроор дахин оролдохгүй.

### 2. `policy.py` — `can_use_tool` (шууд хариулна, хүлээхгүй)

- Гэрээ §5-ийн `decide()`. readme §6-ийн дүрмүүд:
  - Хориглох: `git push|commit|checkout|restore|reset|clean|stash`, `rm -rf`, `npx sst *`,
    production/staging бүх зүйл, `apps/agent/**`, signing/updater, `sst.config.ts`, `infra/**`, CI.
  - Хатуу зогсолт (`hard_stop=True`): production хүрэх, өгөгдөл устгах, graph-ийн "эхлээд асуу" үйлдэл
    (`delete_entities` г.м.), гэрээнээс гадуур schema. (Мөнгөний логик/plan-д байхгүй утга → verify шатанд.)
- Bash командыг **parse** хийж шалгана (`&&`, `;`, `|`, `$(...)`, `env X=1 cmd`, `bash -c` доторх) —
  энгийн substring биш. Тодорхойгүй бол Deny.

### 3. `gates.py`

- **Snapshot/checkpoint** (O3, commit-гүй): tracked patch (`git diff`) + untracked файлын хуулбар,
  repo-оос **гадна**; алхам бүрийн өмнө/дараа. Хоёр snapshot-ийн diff → өөрчлөгдсөн файлын жагсаалт.
- **Codex allowlist**: snapshot A→B-ийн файл бүр `codex_allowlist` glob-д багтах ёстой.
- **Circuit breaker**: 3 дараалсан оролдлогод файл өөрчлөгдөхгүй, эсвэл ижил алдаа 5 удаа → BLOCKED.
- **STATUS**: `handoff-sN.md` эхний мөр `STATUS: DONE|BLOCKED`; тест тушаал бүр exit 0.
- `evaluate()` → `GateVerdict` (гэрээ §4).

## Тест

- **Unit (заавал, readme §10.4):** policy — хатуу зогсолт бүр + bypass оролдлогууд (chained/nested
  bash); gates — circuit breaker, snapshot diff, allowlist зөрчил; квот таних — 3 давхарга тус бүр,
  timeout ≠ квот. SDK-г mock.
- **Integration:** `tests/fixtures/toy-repo` (tmp git repo) дээр жижиг task-аар `runner` → `gates`
  бодит SDK-аар нэг удаа (зардал бага, budget-тай). **qinno-д хүрэхгүй.**

## Хүрэхгүй

`codex.py`, `memory.py`, олон алхмын урсгал, `run`/`resume` CLI (S3). qinno repo.

## Дуусгах шалгуур

- `uv run pytest` ногоон (unit + toy-repo integration), `ruff` цэвэр.
- `docs/notes/agent-sdk.md` — баталгаажуулсан API (эх сурвалжийн URL-тай).
- toy-repo дээр: нэг алхам DONE; хориотой команд → Deny; хатуу зогсолт → BLOCKED; 3 хоосон оролдлого →
  circuit breaker. Бүгд `state.db`-д харагдана.
- `handoff-s2.md`.
