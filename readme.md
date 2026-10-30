# Agent Orchestrator — дизайн (шинэ төсөл)

Төлөв (2026-10-30): дизайн (2026-10-29) + v1 MVP код S1-S3-аар бэлэн; pilot хүлээгдэж байна — `docs/planning/orchestrator-v1/handoff-s3.md`. Шинэ, тусдаа Python төсөл болгон шинэ сесст эхлүүлнэ.
Энэ файл = тэр сессийн эхлэх цэг. Анхны хэрэглээ: `docs/planning/rbac-v2/` (6 сесс, Claude + Codex).

---

## 1. Зорилго

Төлөвлөгөө ба шийдвэрийг хүн + Claude хамтдаа гаргасны дараа, **хэрэгжүүлэлтийг хүний оролцоогүй**
(шөнө ч гэсэн) гүйцэтгэх, явцыг зургаар харуулах хэрэгсэл.

```
① Төлөвлөгөө  →  ② Хэрэглэгчийн шийдвэрүүд  →  ③ Явц зургаар  →  ④ Агент өдөр шөнөгүй гүйцэтгэнэ
   (Claude-тэй ярилцаж — автоматжуулахгүй)        (dashboard)      (энэ төслийн гол ажил)
```

- ①② нь хүний оролцоотой хэвээр — шийдвэрийн чанар тэндээс гардаг.
- Орчин үеийн бэлэн хэрэгслүүдийн аль нь ч бүх хэрэгцээг хангахгүй (§9) → жижиг өөрийн orchestrator
  - бусдын сайн санааг зээлнэ.
- Дахин ашиглагдана: rbac-v2-ийн дараа plan/үнэ (billing) гэх мэт feature-үүдэд.

## 2. Батлагдсан шийдвэрүүд (2026-10-29)

| #   | Шийдвэр                                                                                                                         |
| --- | ------------------------------------------------------------------------------------------------------------------------------- |
| O1  | Тусдаа **шинэ GitHub төсөл, Python**                                                                                            |
| O2  | Orchestrator алхам бүрт **шинэ Claude сесс** үүсгэнэ (шинэ контекст); `/feature-phase` хэрэглэхгүй (тэр нь хүнд хандаж зогсдог) |
| O3  | **Local commit хийхгүй** — явцыг файлын snapshot / patch-аар хэмжинэ                                                            |
| O4  | develop2 дээр `drizzle-kit push` ба `seed:merchant`-ийг **хүнгүй** ажиллуулна (develop2 л)                                      |
| O5  | QA-д зориулж **develop2-д `sst deploy`** хийнэ (orchestrator өөрөө, агент биш)                                                  |
| O6  | Шинэ асуулт гарвал агент **бие даан шийднэ** (policy §6) — том шийдвэрүүд аль хэдийн гарсан                                     |
| O7  | Хэрэглэгчийн **memory MCP graph**-ийг ашиглана (§7)                                                                             |
| O8  | Production release (migrate, seed, deploy) — **үргэлж хэрэглэгч** (hook хамгаална)                                              |
| O9  | Repo: **private, хувийн account** (suhkbayar). Remote үүсгэх нь тусад нь зөвшөөрөл авна (нэр, цаг) — одоогоор зөвхөн local git (2026-10-30) |
| O10 | Агент **Mac host** дээр ажиллана; Docker = зөвхөн дараа нь dashboard-д, container sandbox = v2 (2026-10-30)                    |
| O11 | Суваг: **зөвхөн web dashboard** (утаснаас хариулах Telegram байхгүй). Dashboard = MVP-ийн дараа тул MVP-д мэдэгдэл = `logs/` + `events` хүснэгт (dashboard тэндээс уншина). macOS notification сонгоогүй (2026-10-30) |
| O12 | Мөнгөн хязгаар (USD): нэг дуудлага **2**, нэг сесс **10**, нийт **100** (2026-10-30)                                            |
| O13 | `sst deploy` develop2-д **хэрэглэгч өөрөө** хийнэ; O5 (orchestrator deploy хийнэ) хүчингүй. O4 (`drizzle-kit push`, `seed:*`) хэвээр эсэхийг S3-ын эхэнд баталгаажуулна (2026-10-30) |

## 3. Нээлттэй асуултууд (шинэ сессийн эхэнд)

1. **Repo:** нэр, private эсэх, хувийн account эсвэл `qinno-mongolia` org (repo үүсгэх = гадагш нийтлэх
   → хэрэглэгчийн зөвшөөрлөөр).
2. **Агент хаана ажиллах:** (a) Mac host дээр шууд — **санал** (local MCP, Codex, AWS, yarn, hook-ууд
   шууд ажиллана); (b) Docker container — тусгаарлалт сайн, гэхдээ node/yarn/sst/aws/MCP/нэвтрэлтийг
   image-д оруулах шаардлагатай. Санал: v1 = host; Docker = зөвхөн dashboard; container sandbox = v2.
   (Docker 29.6.2 суусан, daemon 2026-10-29-нд унтарсан байсан.)
3. **Утаснаас хариулах суваг:** Telegram bot эсвэл зөвхөн web dashboard. Telegram/гаднаас хандах бол
   tunnel хэрэгтэй — `Execute`-эрхтэй систем тул нэвтрэлтгүйгээр интернэтэд **хэзээ ч** гаргахгүй.
4. **Мөнгөн хязгаар:** нэг дуудлага, нэг сесс, нийт.

## 4. Архитектур

```
<төслийн-нэр>/                       Python 3.12+, uv
  orchestrator/
    pipeline.py      plan хавтас (README + task файлууд + pipeline.yaml) → DAG; task → дэд task (T1..Tn)
    runner.py        Claude Agent SDK query(): алхам бүрт шинэ сесс; session_id, зардал → state.db
    codex.py         codex exec --sandbox workspace-write --skip-git-repo-check --json (subprocess)
    gates.py         тест ногоон, STATUS: DONE, Codex allowlist (snapshot diff), circuit breaker, checkpoint
    policy.py        can_use_tool (шууд хариулна, хүлээхгүй), хатуу зогсолт, бие даан шийдэх дүрэм
    preflight.py     DB_NAME, stage, AWS, codex "Reply ok", git төлөв, memory health, CLI хувилбар
    memory.py        graph унших/бичих, lock, backup, pending дараалал (§7)
    mcp_server.py    orchestrator-ийн жижиг MCP: get_task, report_progress, record_decision, raise_blocker
  state.db           SQLite: явц, оролдлого, шийдвэр, зардал, session_id (тасарвал үргэлжилнэ)
  dashboard/         FastAPI + HTMX: Todo/Running/Review/Blocked/Done самбар, сесс бүрийн diff
  pipelines/
    qinno-rbac-v2.yaml
  docker-compose.yml dashboard (+ v2-т sandbox)
```

### Нэг сессийн (жишээ нь S3) урсгал

```
preflight ──► ① plan (plan mode, зөвхөн унших) ──► policy тулгалт ── эрсдэлтэй ──► BLOCKED + мэдэгдэл
                                                        │ OK
                                                        ▼
              ② contract: SDL + тест-гэрээ + codex prompt файл бичнэ
                                                        ▼
              ③ Claude track  ──► snapshot A
                                                        ▼
              ④ codex exec     ──► snapshot B   (A→B diff = Codex-ийн өөрчилсөн файлууд)
                                                        ▼
              ⑤ verify (ӨӨР агент, rubric): allowlist тулгалт, бүх тест, /codex-review,
                 graph reconcile, handoff-sN.md (эхний мөр STATUS: DONE | BLOCKED)
                                                        ▼
              gates: тест ногоон ∧ STATUS: DONE ∧ checkpoint хадгалсан ──► дараагийн сесс
```

- **Claude алхмууд дараалан**, нэг working tree (worktree хэрэглэхгүй — yarn monorepo-д үнэтэй).
  Codex-ийг Claude track-ийн **дараа** ажиллуулбал өөрчлөлтийн эзэмшлийг snapshot-оор яг тогтооно
  (зэрэг ажиллуулбал хэн юу өөрчилснийг ялгах боломжгүй — commit хийхгүй тул).
- Сесс бүрийн `session_id` хадгалагдана → өглөө `claude --resume <id>`-ээр шалгаж болно.
- Codex 2 удаа бүтэлгүйтвэл тэр ажлыг Claude авна (`codex-delegate` skill-ийн хатуу хязгаар).

## 5. Бусад төслөөс авах санаанууд

### 🔴 v1 заавал

| Санаа                                                                                                                 | Эх сурвалж        | Хаана           |
| --------------------------------------------------------------------------------------------------------------------- | ----------------- | --------------- |
| Circuit breaker: 3 удаа файл өөрчлөгдөхгүй / ижил алдаа 5 удаа → зогсоно                                              | Ralph             | `gates.py`      |
| Квот дууссаныг 3 давхаргаар таних (JSON `rate_limit_event` → текст → timeout-ийг андуурахгүй), 60 мин хүлээгээд дахин | Ralph             | `runner.py`     |
| Permission татгалзагдвал зогсоно, сохроор дахин оролдохгүй                                                            | Ralph             | `policy.py`     |
| `can_use_tool` шууд хариулна (хүлээлт = зардал)                                                                       | Composio AO       | `policy.py`     |
| Явцын төлвийг orchestrator эзэмшинэ; агент зөвхөн `passes`; JSON                                                      | Anthropic harness | `state.db`      |
| Сесс эхлэх журам: progress/handoff → `git diff --stat` → smoke тест → дараа нь шинэ ажил                              | Anthropic harness | prompt          |
| Шалгагч ≠ гүйцэтгэгч: verify нь өөр агент, rubric (acceptance, гэрээ, "Стандарт", raw query тоо)                      | Anthropic 3-agent | ⑤               |
| Config-ийг өгөгдөл болгож уншина (shell `source` биш)                                                                 | Ralph v0.11       | `pipeline.yaml` |

### 🟡 v1 сайн

| Санаа                                                               | Эх сурвалж             | Хаана           |
| ------------------------------------------------------------------- | ---------------------- | --------------- |
| Orchestrator MCP tool-ууд (агент файл засахын оронд tool дуудна)    | Task Master            | `mcp_server.py` |
| Plan-first + эрсдэлтэй бол escalate                                 | openclaw-code-agent    | ①               |
| Дэд task + `next_task`; дундаас үргэлжлэх                           | Task Master            | `pipeline.py`   |
| Алхам бүрийн checkpoint (patch + untracked хуулбар, repo-оос гадна) | Conductor              | `gates.py`      |
| Самбар + сесс бүрийн diff                                           | Vibe Kanban, Conductor | `dashboard/`    |
| Дуудлагын timeout, цагийн дуудлагын хязгаар, нийт зардал            | Ralph                  | `runner.py`     |

### 🟢 v2

Playwright E2E QA (develop2 deploy-ийн дараа) · утаснаас pause/steer/approve · CI унах / review comment-д
автомат засвар (Composio AO) · event stream replay (OpenHands) · Docker sandbox.

### ❌ Авахгүй

`--dangerously-skip-permissions` · git worktree · commit-ийг явц гэж тоолох · төлөвлөгөөг агентаар гаргуулах.

## 6. Policy — хамгаалалт ба бие даасан шийдвэр

**Зөвшөөрлийн горим:** `permission_mode="auto"` + `can_use_tool` (кодоор). `bypassPermissions` хэзээ ч.
Hook-ууд (`.claude/settings.json`, `~/.claude/settings.json`) ажилласаар — production DB guard, dirty-tree
guard. Deny rule бүх горимд үйлчилнэ.

**Агентад хориглох:** `git push|commit|checkout|restore|reset|clean|stash`, `rm -rf`, `npx sst *`
(deploy-ийг orchestrator өөрөө develop2-д), production/staging-ийн аливаа зүйл, `apps/agent/**`,
signing/updater, `sst.config.ts`, `infra/**`, CI.

**Хатуу зогсолт (BLOCKED + мэдэгдэл):** production хүрэх; мөнгөний логикийн утга өөрчлөх (plan-д
байхгүй); гэрээнээс гадуур schema; өгөгдөл устгах; graph-ийн "эхлээд асуу" зүйл; circuit breaker.

**Бие даан шийдэх дүрэм (O6):** research баримттай нийцсэн, хамгийн болгоомжтой, буцаах боломжтой
хувилбарыг сонгоно → `AUTONOMOUS-DECISIONS.md`-д (асуулт, сонголтууд, сонгосон, шалтгаан, файл) бичнэ
→ үргэлжилнэ. Task файл "хэрэглэгчээс асуу" гэж заасан газарт ч энэ дүрэм үйлчилнэ.

**Preflight (аль нэг нь бүтэлгүйтвэл ЭХЛЭХГҮЙ):**

- `services/migrate-service/.env`-ийн `DB_NAME` = `qinno_develop2`. ⚠️ **2026-10-29-нд `qinno`
  (PRODUCTION) байсан** — production DB guard hook барьсан. Засвар (хэрэглэгч):
  `npm run sync-secrets -- --stage=develop2`.
- `.sst/stage` = `develop2` (⚠️ `.sst/outputs.json`-ийн URL-ууд production — QA-д develop2 URL).
- AWS: static key (SSO биш — шөнө дуусахгүй, 2026-10-29 шалгасан).
- `codex exec --skip-git-repo-check -s read-only "Reply ok"`.
- Claude Code ≥ 2.1.259 (`--permission-prompts` туг; 2026-10-29 = 2.1.283), codex-cli 0.147.0.
- memory MCP health (§7).
- Mac: цэнэглэгчид залгаатай, таг нээлттэй, `caffeinate -i`.

## 7. Memory MCP graph

- **Унших:** сесс бүрийн эхэнд `pipeline.yaml`-д заасан entity-үүдийг `open_nodes(["яг нэр"])`-ээр
  (жишээ нь `Decision: RBAC v2`, `Core Domain`, `Qinno Backend Architecture`) prompt-д оруулна.
  `search_nodes` = нэг түлхүүр үг; `read_graph` хэзээ ч.
- **Бичих:** зөвхөн verify алхамд, `qinno-graph-reconciler`-ийн загвараар; entity шинэчлэх бүрт
  `~/Documents/my-memory/confidence.jsonl`-ийн мөрийг **тэр алхамд** шинэчилнэ.
  Автоматаар (tier-1, 2026-09-28): observation нэмэх, баримтын алдаа засах, шинэ `Decision:`/`Convention:`.
- **Эхлээд асуу** (entity устгах/нэр солих, `conf: D` өөрчлөх, дүрэм хасах): хийхгүй →
  `~/Documents/my-memory/logs/pending-*.md` + dashboard.
- **Нэг бичигч:** процесс бүр өөрийн memory server эхлүүлж нэг `memory.jsonl` руу бичдэг → orchestrator
  файлын lock. launchd `com.suhkbayar.graph-reconcile` (**ням гараг 10:00**) ижил lock-ийг хүндэтгэх /
  тэр цагт бичихгүй.
- **Backup** бичихийн өмнө (reconcile-ийн `backups/<огноо>/` загвар).
- Health check бүтэлгүйтвэл (10-25-нд timeout, үлдсэн `npx` процессууд) graph-гүй үргэлжилж бичилтийг
  pending руу.
- Codex graph-д хүрэхгүй. Ажиллагааны төлөв (`state.db`) graph-д орохгүй — graph = шийдвэр + шалтгаан.

## 8. Эхний хэрэглээ: `docs/planning/rbac-v2/`

- `README.md` (сессийн хуваарилалт, track хоорондын гэрээ §1-§4) + `s1..s6-{claude,codex}-*.md`
  (бие даасан, allowlist-тэй) + research `docs/planning/rbac-v2-research.md`.
- Бүх нээлттэй асуулт **шийдсэн** (task файлуудын "Шийдсэн асуултууд ✅").
- `PinLookupKey` SST secret (S3) байхгүй байсан ч код bcrypt замаар fail-safe — хүлээхгүй.
- S6: гар QA-г автомат тест + `/codex-review` + (O5) develop2 deploy-ийн дараах шалгалтаар орлуулна.
- Pipeline тайлбар: `pipelines/qinno-rbac-v2.yaml` — S1→S6 дараалал, файл бүрийн зам, memory entity-үүд,
  тест тушаалууд.

## 9. Судалгаа (2026-10-29) — бэлэн хэрэгслүүд яагаад хүрэлцэхгүй

| Ангилал                         | Жишээ                                         | Дутуу нь                                                                                     |
| ------------------------------- | --------------------------------------------- | -------------------------------------------------------------------------------------------- |
| Олон агент зэрэг (хүн удирдана) | Conductor, Claude Squad, Vibe Kanban, Crystal | хүнгүй биш; worktree                                                                         |
| Төлөвлөгөө → task               | Claude Task Master                            | бидний төлөвлөгөө аль хэдийн илүү нарийн                                                     |
| Хүнгүй давталт                  | Ralph, Composio AO, dagain, AgentOS, DevFleet | skip-permissions / worktree; жижиг төслүүд                                                   |
| Plan батлах + resume            | openclaw-code-agent                           | OpenClaw-ийн plugin                                                                          |
| Бүрэн платформ                  | OpenHands                                     | өөрийн агент — Claude Code hook/MCP/subagent ашиглахгүй                                      |
| Cloud                           | `/schedule` routines, GitHub Actions          | develop2/AWS/local MCP/Codex-д хандахгүй                                                     |
| n8n                             | Execute Command, Wait/approval, canvas        | зохицуулалт/мэдэгдэлд сайн; feature код бичихийг хурдасгахгүй; бизнес логикийг n8n-д бүү хий |

Qinno-д л байгаа шаардлага: Claude↔Codex гэрээ + давхцахгүй allowlist, нэг working tree, develop2
хамгаалалт, commit-гүй, memory graph протокол.

**Эх сурвалж:**

- Anthropic — Effective harnesses for long-running agents: https://anthropic.com/engineering/effective-harnesses-for-long-running-agents
- InfoQ — Anthropic three-agent harness: https://www.infoq.com/news/2026/04/anthropic-three-agent-harness-ai
- Claude Code headless: https://code.claude.com/docs/en/headless.md · CLI: https://code.claude.com/docs/en/cli-reference.md · Permission modes: https://code.claude.com/docs/en/permission-modes.md
- Ralph for Claude Code: https://cdn.jsdelivr.net/gh/frankbria/ralph-claude-code@main/README.md
- Codex non-interactive: https://developers.openai.com/codex/noninteractive
- Orchestrators compared: https://orcy-website.pages.dev/compare · https://www.augmentcode.com/tools/open-source-agent-orchestrators
- openclaw-code-agent: https://github.com/goldmar/openclaw-code-agent · Task Master MCP: https://playbooks.com/mcp/eyaltoledano-task-master
- n8n-nodes-claude-code: https://npmjs.com/package/n8n-nodes-claude-code

## 10. Шинэ сессийн эхний алхмууд

1. Энэ файлыг уншина. §3-ийн 4 асуултыг хэрэглэгчээс асууна.
2. Repo үүсгэнэ (зөвшөөрлийн дараа), `uv init`, бүтэц §4.
3. **v1 MVP дараалал:** `preflight.py` → `pipeline.py` (+ `qinno-rbac-v2.yaml`) → `runner.py` (нэг
   алхам) → `gates.py` → `policy.py` → `codex.py` → `memory.py` → CLI (`run`, `status`, `resume`,
   `--only S1`). Dashboard ба MCP server = MVP-ийн дараа.
4. Unit тест: policy (хатуу зогсолт бүр), gates (circuit breaker, snapshot diff), квот таних.
5. **Pilot:** хэрэглэгч сэрүүн үед `--only S1` (DB_NAME-ийг эхэлж засна) → `auto` горим `drizzle-kit`
   / `aws` командыг блоклох эсэх, headless-д local MCP ачаалагдах эсэхийг бодитоор шалгана → дараа нь
   S2-S6 хүнгүй.
