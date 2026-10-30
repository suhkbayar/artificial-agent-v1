STATUS: BLOCKED

Блоклосон зүйл **зөвхөн pilot** (`--only S1` на qinno): таны нөхцөлүүд биелээгүй тул орсонгүй (`pilot-s1.md`).
S3-ын бусад бүх код, тест, toy-repo дээрх бодит ①-⑤ урсгал бэлэн.

# handoff-s3

Gate results (run 2026-10-30, real output):

- `uv run pytest` → **625 passed, 7 skipped** (S2: 329 passed, 3 skipped). 7 skipped = opt-in бодит SDK/Codex тестүүд.
- `ORCHESTRATOR_TEST_AUTH=login ORCHESTRATOR_INTEGRATION=1 uv run pytest tests/test_integration_toy.py
  tests/test_integration_real.py` → **13 passed in 187s** (S2-ын 3 бодит тест + S3-ын 4: init/mode/MCP probe, scrub probe,
  `env` хорих, бүтэн ①-⑤ Claude + Codex-той). Auth = claude.ai нэвтрэлт (доорх "Auth" хэсгийг үз).
- `uv run ruff check` → All checks passed! · `ruff format --check` → цэвэр.
- `uv run orchestrator preflight --pipeline pipelines/qinno-rbac-v2.yaml` → **exit 1** (DB_NAME=qinno PRODUCTION,
  AWS `InvalidClientTokenId`; бусад 7 PASS). `plan` exit 0, `status` exit 0, CLI: `run`, `run --only`, `status`, `resume`.
- qinno repo: `git status --short` хоосон, HEAD `75d22606` (S1/S2-той адил). Pilot хүртэл зөвхөн уншсан.
- Бодит SDK/Codex дуудлага зөвхөн toy-repo-н түр хуулбар (болон `/private/tmp/.../probe-cwd`, artificial-agent-v1 cwd-д
  read-only `open_nodes`) дээр. Бодит ①-⑤ урсгалын нэг ажиллагаа: Claude $0.62 + Codex 64k input token (USD-гүй),
  budget хязгаар per_call 0.60 / session 2.5 / total 4.0. Нийт probe + бүх бодит тестийн зардлыг нарийн нийлбэрлээгүй, ≈ $3 орчим (тест бүр өөрийн хязгаартай; нийт $100-ын хязгаарт хамаарахгүй).
  Тестийн `memory.jsonl`/`confidence.jsonl` нь tmp хуулбар; жинхэнэ graph-ийн sha256 тестийн өмнө/дараа ижил (`test_memory.py`).

## Хийсэн

- **3a `.env.local` loader** (`envfile.py`): өгөгдөл болгон parse (shell биш), зөвхөн `ANTHROPIC_API_KEY` allowlist,
  `os.environ`-д бичихгүй, `StepRequest.env` (`repr=False`) → SDK-ийн `env`. Файл байхгүй бол claude.ai нэвтрэлт.
  `redact()` нь runner-ийн reason/stderr/exception/event, Codex алдаа, flow-ийн event/decisions/artefact-д ашиглагдана.
  Тест: `tests/test_secrets.py` (state.db-ийн бүх хүснэгтийг dump хийж key байхгүйг шалгана).
- **3b policy**: `env`/`printenv`/`export`/`declare -p`/`set`/`compgen -e`/`ps eww|-E`, `$…KEY|TOKEN|SECRET…`
  хувьсагчийн лавлагаа, `.env*` унших (`cat`, `grep`, `<` redirect, `git show HEAD:.env`, glob `.env*`/`.e*`), мөн
  `Read`/`Grep`/`Glob` tool-оор `.env*`. `.env.example|sample|template` зөвшөөрнө. 45 хоригтой + 24 зөвшөөрөгдөх командын тест.
- **3c pipeline**: `schema_allowlist` (сесс тус бүр, зөвхөн S1: `roles.ts`, `users.ts`, `merchant-users.ts` — s1-claude-foundation.md
  "Ownership"-оос), pipeline/сесс түвшний `policy:` (зөвхөн нэмнэ, сулруулах түлхүүр = `PipelineError`), `requires_stage`,
  `open_items`. **S1 BLOCKED болохгүй гэдгийг** qinno-ийн 6 сессийн Claude "Ownership" блок бүрийг уншиж, файл бүр
  `Write`-д Allow гэдгийг тестлэсэн (qinno байхгүй машинд skip) + schema_allowlist = Ownership дахь `packages/db/**` гэдгийг тестлэсэн.
  S2-S6 нь schema засахад hard stop хэвээр. **Нэмэлт чангатгал:** schema гэрээ одоо Bash-ийн бичилтэд (`sed -i`, `>`, `tee`) мөн
  үйлчилнэ (өмнө нь зөвхөн Write tool-д байсан — S2-ын цоорхой).
- **3d** state.db schema **v2** (автомат migration, v1 DB-ийн мөр хадгалагдана): `attempts` хүснэгт, `runs.pipeline_path`.
  Flow Claude/Codex/repair оролдлого бүрийн `files_changed` + алдааг бичиж, `resume` дээр `breaker_tripped`-д сэргээнэ
  (тест: шинэ Flow объект state.db-ээс 2 хоосон оролдлого уншаад 3-р дээр блоклоно).
- **3e**: effective permission mode-г **ажиглаж чадлаа** (init `permissionMode`), доор. Бодит quota stop-ийн хэлбэр **олдсон** (доор).
- **`codex.py`**: `codex exec --sandbox workspace-write --skip-git-repo-check --json -C <repo> -` (prompt stdin), JSONL parse
  (бодит 0.147.0 stream-ээс), timeout, 2 алдаа → Claude авна (`codex_fallback` event), USD-гүй (token usage event).
- **`memory.py`**: exact-name `open_nodes` (read-only parse), `.reconcile.lock` mkdir lock (launchd reconcile-ийн яг тэр lock),
  Ням 09:50–11:00 цонхонд бичихгүй, backup, health (parse + үлдсэн `server-memory` процесс + гадны lock), before/after audit
  (устгасан/protected entity → BLOCKED + `logs/pending-*.md`; entity өөрчилсөн ч `confidence.jsonl` мөр шинэчлэгдээгүй → gate fail),
  health унавал graph-гүй үргэлжилнэ (бүх memory tool хориглогдож, prompt "UNAVAILABLE" гэнэ).
- **`flow.py` ①-⑤**, **CLI** `run [--only]`, `status`, `resume [--retry]` (+ `caffeinate -i`). `resume` тасарсан алхмаас;
  `blocked|failed|permission_denied` → хүн засаад `--retry`.
- Баримт: `docs/notes/agent-sdk.md` §11, planning README §2/§3/§6, `pilot-s1.md` (STATUS: NOT RUN + runbook).

## Бодитоор ажигласан (docs/notes/agent-sdk.md §11)

1. **Effective permission mode ажиглагдана** (`init.permissionMode`, `session_init` event + зөрвөл `notice`). `auto`, `plan`,
   `acceptEdits` нь **идэвхтэй болдог**. Харин `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` тавьсан бол CLI **бүх горимыг `default` болгоно**
   ("allowed_non_write_users hardening"). S2-ын "auto → default fallback" таамаглал буруу байсан: шалтгаан нь миний нэмсэн flag.
2. **Scrub flag ажиллана** (хуурамч `AWS_SECRET_ACCESS_KEY` агентын Bash-д flag-тай үед харагдахгүй, flag-гүй үед харагдана).
   **`ANTHROPIC_API_KEY`-д шууд шалгаж чадаагүй** (доорх billing).
3. **Trade-off ба шийдвэр** (таны шалгана уу): key байгаа үед л scrub тавина (`sdk_env`), тэгэхээр key-тэй сесс `default` горимтой —
   `auto`-ын classifier давхарга алга, гэхдээ бидний hook + `can_use_tool` бүх хатуу дүрмийг хэрэгжүүлсээр. ① plan алхмыг
   горимоос үл хамааруулж `PolicyContext.read_only` (жижиг read-команд allowlist) болгов. `ORCHESTRATOR_SCRUB_SUBPROCESS_ENV=0`
   гаргавал scrub үгүй, `auto`/`plan` идэвхтэй, харин агентын бичсэн script key-г уншиж чадна.
4. **Headless MCP**: `memory` зэрэг server-үүд `~/.claude.json`-д **төслийн замаар** (`projects["<cwd>"].mcpServers`) тохируулагдсан,
   user-scope хоосон. artificial-agent-v1 cwd-д `memory` init жагсаалтад орж, `mcp__memory__open_nodes` бодитоор дуудагдсан;
   toy (tmp) cwd-д байхгүй. **qinno cwd-д ачаалагдах эсэх харагдаагүй** (pilot-ын асуулт). Hook-ууд (`hooks_tools` 27 vs 23) ачаалагдсан.
5. **Бодит quota stop** (2026-10-11 интерактив transcript, SDK-run биш): `error: rate_limit`, `apiErrorStatus: 429`, текст
   "You've hit your session limit · resets 8pm (...)", `rate_limit_info{status: rejected, resetsAt, rateLimitType: five_hour}`.
   **Layer-2 regex үүнийг танихгүй байсан** → `hit your (\w+ )?limit`, `usage_limit_reached` нэмсэн, яг эдгээр хэлбэрээр тест.
   **Харагдаагүй:** SDK-run дахь quota stop-ийн эцсийн `ResultMessage`/exception текст.

## Гэрээнээс хазайлт / тайлбарлах шийдвэрүүд

1. `StepResult.output` нэмсэн (plan/contract-ийн текст). `run_command` `input_text` авдаг болсон (Codex prompt stdin).
2. Memory backup нь `backups-orchestrator/<огноо>/` — `backups/` БИШ, учир нь launchd reconcile `backups/*/`-ийг нэрээр эрэмбэлж сүүлийн 8-аас
   бусдыг устгадаг (манай хавтас орвол тэдний backup устна).
3. Graph-д бичигч нь ⑤ verify агент (memory MCP-ээр); orchestrator файлыг өөрөө засахгүй, зөвхөн lock + backup + audit.
4. Preflight-ийн memory шалгалт health унасан үед **ok=True, "DEGRADED"** (readme §7: graph-гүй үргэлжилнэ).
5. `requires_stage: false` (toy pipeline) үед DB_NAME/.sst/AWS preflight алгасна, post_steps хориглоно.
6. Verify агент зөвхөн handoff + `AUTONOMOUS-DECISIONS.md` бичнэ — өөр файл өөрчлөвөл (snapshot diff) gate унана. Тэр md-ийн
   `## асуулт` + `- Options|Chosen|Reason|Files:` мөрүүдийг `decisions` хүснэгтэд синк хийнэ.
7. Repair хүрээ: gate унавал (тест/handoff/memory) хамгийн ихдээ 2 repair + дахин verify; allowlist/breaker/verify-агент-өөрчлөлт/
   `handoff reports BLOCKED` нь шууд BLOCKED.
8. Codex quota/зардал: Codex-д USD байхгүй тул зөвхөн timeout хязгаар (budget-д тоологдохгүй).

## Хийгээгүй / батлагдаагүй (нуулгүй)

- **Pilot ороогүй.** `DB_NAME=qinno` (PRODUCTION), AWS key хүчингүй, таны шууд "эхэл"-ийн зөвшөөрөл байхгүй.
  Тиймээс бодит post_steps (develop2 push/seed), qinno-д auto горим `drizzle-kit`/`aws`-ийг блоклох эсэх, qinno cwd-д MCP/hook —
  **бүгд харагдаагүй**. Post step-ийн "2 дахь seed 0 added / 0 removed" шалгалт нь seed-ийн хэвлэх мөрийн **таамагласан** regex-тэй
  (`0 added … 0 removed`) — бодит гаралтаар таарахгүй бол зөв seed-д хуурамч BLOCKED өгнө.
- **Auth: `.env.local`-ийн key-ээр амжилттай дуудлага ажиглагдаагүй.** Key нь SDK-д ашиглагдаж байна (`apiKeySource: ANTHROPIC_API_KEY`),
  гэтэл `Credit balance is too low` (billing_error, HTTP 400). Тиймээс бүх бодит ажиллагаа claude.ai нэвтрэлтээр
  (`ORCHESTRATOR_TEST_AUTH=login`). Unattended шөнийн ажилд кредит нэмэх, эсвэл key-гүй явах шийдвэр танаас. SDK docs: гуравдагч
  талын бүтээгдэхүүн claude.ai нэвтрэлтийг санал болгож болохгүй гэж заадаг (docs/notes §8).
- Scrub-ийг `ANTHROPIC_API_KEY` дээр шууд баталгаажуулаагүй (дээр 2).
- Policy sandbox биш хэвээр: агентын бичсэн script (`node x.js`) `process.env` уншиж чадна — scrub flag л хаана; flag-гүй үед нээлттэй.
  Recursive `grep -r KEY .` нь `.env*` файлыг уншиж болно (`.env`-ийг нэрээр нь хориглосон, директор скан биш).
- `schema_globs` (`packages/db/src/schemas/**`) нь qinno-д бодитоор байгаа замтай таарна (шалгасан); `…/schema/**` нь таамаг, хор хөнөөлгүй.
  Signing/updater-ийн **яг зам** task файлд байхгүй → `open_items` TODO (`plan` командад харагдана); өргөн glob хэвээр.
- Crash болсон дуудлагын зардал алдагдана (`ResultMessage` л зардал өгнө) — S2-ын хязгаар хэвээр.
- Policy-ийн давхар зардал: агент heredoc (`cat > f <<EOF`) байнга оролдож хоригдоно, `for f in plan/*.md` wildcard heuristic-д
  хориглогдоно (бодит ажиллагаанд 4 удаа харагдсан) — зөвхөн turn зарцуулна; сулруулахыг би шийдээгүй.
- Бодит ①-⑤ toy урсгал ≈2.5 минут. Dashboard, `mcp_server.py`, Docker — MVP-ийн дараа (хөндөөгүй).
- Ownership-ээр write allowlist (Claude сесс зөвхөн өөрийн Ownership файлд бичих) нь боломжит дараагийн чангатгал, **хийгээгүй**
  (Ownership блок бүрэн биш байж, шөнө хуурамч BLOCKED өгөх эрсдэлтэй).

## Дараагийн алхам (хэрэглэгчид)

1. DB_NAME засвар, AWS key, auth шийдвэр (дээр), дараа нь `pilot-s1.md`-ийн runbook-оор pilot. Үр дүнг тэр файлд бичнэ.
2. Pilot-ын дараа S2–S6 хүнгүй ажиллуулах go/no-go танд.

## Дараагийн сесс эхлэхдээ шалгах

1. `uv sync --reinstall-package orchestrator`; `uv run pytest` → 625 passed, 7 skipped.
2. `ORCHESTRATOR_TEST_AUTH=login ORCHESTRATOR_INTEGRATION=1 uv run pytest tests/test_integration_toy.py tests/test_integration_real.py`
   (≈$1.5, 3 мин). Key-ээр: `ORCHESTRATOR_TEST_AUTH` тавихгүй (billing засагдсаны дараа).
3. `uv run orchestrator preflight --pipeline pipelines/qinno-rbac-v2.yaml`.
