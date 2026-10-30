# S1 — Суурь: асуултууд, scaffold, state, preflight, pipeline

Гэрээ: [`README.md`](README.md) §1-§3. Дизайн: [`/readme.md`](../../../readme.md) §3, §4, §6, §8.

## Эхлэхийн өмнө

1. `readme.md` бүтнээр, энэ хавтасны `README.md`-г уншина.
2. Graph: `open_nodes(["Agent Orchestrator"])`. Байхгүй бол `search_nodes("Orchestrator")`-оор
   давхардал шалгаад root entity (`Project`, 3-5 observation) + `confidence.jsonl` мөрийг нэг ээлжинд үүсгэнэ.
3. Хэрэглэгчээс readme §3-ийн **4 асуултыг** асууна (README "Нээлттэй асуултууд"-ын таамаглалтай).
   Хариултыг `readme.md` §2-т O9+ болгож нэмж, graph-д бичнэ.

## Ажлууд

### 1. Scaffold

- `uv init` (энэ repo-д), Python 3.12+, dev: `pytest`, `ruff`. Бүтэц readme §4 (dashboard, `mcp_server.py`-гүй).
- `.gitignore` (§1-ийн жагсаалт; `.claude/settings.local.json` аль хэдийн бий).
- GitHub remote — асуулт 1-ийн зөвшөөрөл гарсан бол л.
- CLI араг (`click` эсвэл `typer`, аль нь ч): `preflight`, `plan`, `status` команд.

### 2. Config ба `state.py`

- Orchestrator-ийн config (budget, timeout, мэдэгдэл, checkpoint хавтас — repo-оос **гадна**,
  жишээ нь `~/.agent-orchestrator/`). Pipeline тус бүрийн тохиргоо yaml-д (§2).
- `state.py`: SQLite, гэрээ §3-ийн хүснэгтүүд, жижиг repository API (run эхлүүлэх, step бичих/шинэчлэх,
  event нэмэх, сүүлийн дуусаагүй step олох). Schema хувилбар (`PRAGMA user_version`) — дараа migrate хийхэд.

### 3. `preflight.py`

readme §6-ийн жагсаалт — шалгалт бүр `CheckResult(name, ok, detail, fix_hint)` буцаана, **аль нэг нь
бүтэлгүйтвэл эхлэхгүй**:

- qinno `services/migrate-service/.env`-ийн `DB_NAME == qinno_develop2` (⚠️ 2026-10-29-нд `qinno`
  = PRODUCTION байсан; fix_hint: `npm run sync-secrets -- --stage=develop2`, хэрэглэгч хийнэ).
  **Файлыг өөрчлөхгүй, зөвхөн уншина.**
- `.sst/stage == develop2`.
- AWS: static key (SSO биш), `aws sts get-caller-identity` амжилттай.
- `codex exec --skip-git-repo-check -s read-only "Reply ok"`.
- `claude --version` ≥ 2.1.259, `codex --version` (0.147.0 тэмдэглэ, хатуу биш).
- git төлөв (working tree-ийн хувилбар/dirty эсэхийг тэмдэглэнэ — commit хийхгүй, O3).
- memory MCP health — S1-д stub (`memory.py` S3-т); timeout-тэй `npx` процесс үлдээхгүй.
- Mac: цэнэглэгч (`pmset -g batt`), `caffeinate` боломжтой эсэх.

Шалгалт бүрийг тусад нь unit тестлэнэ (subprocess/файлыг mock).

### 4. `pipeline.py` + `pipelines/qinno-rbac-v2.yaml`

- Гэрээ §2-ийн yaml → `Pipeline`/`SessionSpec`; plan хавтсын файлууд байгаа эсэх, `depends_on`
  цикл, давхардсан id → тодорхой алдаа.
- `qinno-rbac-v2.yaml`-ийг `~/Documents/Projects/qinno/docs/planning/rbac-v2/` (README + `s1..s6-{claude,codex}-*.md`)
  **зөвхөн уншиж** бөглөнө: task файл, allowlist, тест тушаал, `post_steps` (S1 = drizzle push,
  S2 = seed:merchant — task файлаас баталгаажуул), memory entity-үүд.
- Task файлаас олдохгүй утгыг зохиохгүй — `TODO` гэж үлдээж handoff-д жагсаана.

## Хүрэхгүй

`runner`, `policy`, `gates`, `codex`, `memory`-ийн жинхэнэ логик (S2/S3). qinno repo-д ямар ч бичилт.

## Дуусгах шалгуур

- `uv run pytest` ногоон, `ruff check` цэвэр.
- `uv run orchestrator preflight --pipeline pipelines/qinno-rbac-v2.yaml` — шалгалт бүрийн үр дүнг
  хүснэгтээр гаргана (DB_NAME буруу бол бүтэлгүйтэх нь **зөв** үр дүн).
- `uv run orchestrator plan pipelines/qinno-rbac-v2.yaml` — S1→S6 дараалал, сесс бүрийн файл,
  allowlist, тестийг хэвлэнэ.
- `handoff-s1.md` (`STATUS:` эхний мөр), §3-ийн хариултууд `readme.md` ба graph-д.
