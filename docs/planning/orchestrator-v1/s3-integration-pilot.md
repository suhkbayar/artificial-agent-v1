# S3 — Нэгтгэл ба pilot: codex, memory, урсгал ①-⑤, CLI

Гэрээ: [`README.md`](README.md) §2-§5. Дизайн: [`/readme.md`](../../../readme.md) §4 (сессийн урсгал),
§6 (O6 дүрэм), §7, §10.5.

## Эхлэхийн өмнө

1. `handoff-s2.md` → `STATUS: DONE`; `git diff --stat` + `uv run pytest` (smoke).
2. Graph: `open_nodes(["Agent Orchestrator"])`.
3. `docs/notes/agent-sdk.md` (S2) — headless-д MCP/hook ачаалагдах тухай тэмдэглэл.

## Ажлууд

### 1. `codex.py`

- `codex exec --sandbox workspace-write --skip-git-repo-check --json` (subprocess), prompt = pipeline-ийн
  `codex_task` файл + contract (②).
- JSON stream-ээс дуусгавар/алдаа/зардлыг уншина; timeout.
- **2 удаа бүтэлгүйтвэл** тэр ажлыг Claude авна (`codex-delegate` skill-ийн хатуу хязгаар) — event бичнэ.
- Codex graph-д хүрэхгүй (prompt-д заана).

### 2. `memory.py` (readme §7)

- Унших: `memory_entities`-ийг `open_nodes`-оор prompt-д оруулна. `read_graph` хэзээ ч.
- Бичих зөвхөн ⑤ verify-д; entity бүрд `confidence.jsonl` мөрийг **тэр алхамд**.
- **Нэг бичигч:** `memory.jsonl` дээр файлын lock; launchd `com.suhkbayar.graph-reconcile`
  (ням 10:00) мөн тэр lock-ийг хүндэтгэх эсвэл тэр цагийн цонхонд бичихгүй.
- Бичихийн өмнө backup (`~/Documents/my-memory/backups/<огноо>/` загвар).
- "Эхлээд асуу" үйлдэл → хийхгүй, `~/Documents/my-memory/logs/pending-*.md`.
- Health check (timeout, үлдсэн `npx` процесс) бүтэлгүйтвэл graph-гүй үргэлжилж бичилтийг pending руу.
- Тестийг **жинхэнэ `memory.jsonl` биш**, tmp хуулбар дээр.

### 3. Сессийн урсгал ①-⑤ (`pipeline` + `runner` + `gates`)

readme §4-ийн диаграмаар, сесс бүрт:
preflight → ① plan (зөвхөн унших) → policy тулгалт (эрсдэлтэй → BLOCKED) → ② contract (SDL, тест-гэрээ,
codex prompt) → ③ Claude track → snapshot A → ④ codex → snapshot B → ⑤ verify (**өөр** агент, rubric:
allowlist, бүх тест, `/codex-review`, graph reconcile, `handoff-sN.md`) → gates → `post_steps` (O4,
develop2; preflight дахин) → дараагийн сесс.

- Алхам бүр шинэ сесс (O2); сесс эхлэх журам prompt-д: handoff → `git diff --stat` → smoke тест → шинэ ажил.
- O6 бие даасан шийдвэр → plan repo-ийн `AUTONOMOUS-DECISIONS.md` + `state.db` `decisions`.
- BLOCKED/budget/квот → мэдэгдэл (S1-д сонгосон суваг).
- Production release (O8) — урсгалд **байхгүй**.

### 4. CLI

`run <pipeline>` · `run --only S1` · `status` (сесс/алхам/зардал/session_id) · `resume` (сүүлийн
дуусаагүй алхмаас). `run` эхлэхдээ preflight, `caffeinate -i`.

### 5. Pilot (хэрэглэгч сэрүүн үед, readme §10.5)

1. Хэрэглэгч qinno-гийн `DB_NAME`-ийг засна → `orchestrator preflight` бүгд ногоон.
2. `orchestrator run pipelines/qinno-rbac-v2.yaml --only S1` — хэрэглэгч хажууд нь хянана.
3. Бодитоор шалгаж тэмдэглэх: `auto`/policy горим `drizzle-kit`, `aws` командыг блоклох эсэх;
   headless сесст local memory MCP, hook-ууд ачаалагдах эсэх; зардал, хугацаа.
4. Үр дүнг `docs/planning/orchestrator-v1/pilot-s1.md`-д. S2-S6-г хүнгүй ажиллуулах **go/no-go**-г
   хэрэглэгч шийднэ.

## Хүрэхгүй

Dashboard, `mcp_server.py`, Docker, утасны суваг, Playwright (post-MVP). Production бүх зүйл.

## Дуусгах шалгуур

- `uv run pytest` ногоон (codex/memory unit — subprocess mock, memory tmp хуулбар).
- toy-repo дээр бүтэн ①-⑤ урсгал (codex-той) DONE; `resume` тасалсан газраас үргэлжилнэ.
- Pilot `--only S1` ажилласан (амжилт эсвэл тайлбартай BLOCKED аль аль нь хүлээн зөвшөөрөгдөнө),
  `pilot-s1.md` бичигдсэн.
- `handoff-s3.md`, graph-д MVP-ийн шийдвэрүүд.
