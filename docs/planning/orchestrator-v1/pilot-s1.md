STATUS: DONE — run 1 `done` (2026-10-30, S4): S1 ①–⑤ + post_steps develop2. Доорх "S4: run 1 дуусгалт" хэсгийг үз; дээрх хүснэгтүүд run 1-ийн анхны (BLOCKED) байдал, түүх болж үлдэнэ.

# Pilot `--only S1` на qinno (2026-10-30)

Run 1 ажилласан: ① ② done, ③ policy false positive-оор BLOCKED, засвар хийгдсэн (доор "Pilot-ын явц").
S1-ийг дуусгах (`resume --retry` → ③-⑤, gates, post_steps) нь дараагийн сессийн ажил (`next-session-prompt.md`).

## Анхны preflight (S3-ын төгсгөл, засварын өмнө)

Pilot-д **ороогүй**. Анхны шалтгаан: DB_NAME/AWS нөхцөл биелээгүй (доорх гаралт); тэр нь дараа нь шийдэгдсэн (Шинэчлэл хэсэг). `orchestrator preflight
--pipeline pipelines/qinno-rbac-v2.yaml` бодит гаралт (2026-10-30, S3-ын төгсгөл):

```
FAIL  migrate-service DB_NAME  DB_NAME=qinno is PRODUCTION
PASS  .sst/stage               stage=develop2
FAIL  AWS credentials          sts get-caller-identity failed: InvalidClientTokenId
PASS  codex exec / claude version (2.1.283) / codex version (0.147.0) / git state (feature-rbac-v2@75d22606 clean)
PASS  memory MCP (67 entities readable, no lock, no leaked processes) / power (AC)
```

qinno repo-д зөвхөн уншсан (`git status` цэвэр, HEAD `75d22606` — S1/S2-тэй адил).

## Шинэчлэл (2026-10-30, S3 дараа): DB зорилт = `services/migrate-service/.env.local`

Хэрэглэгч `.env.local`-д `DB_NAME=qinno_develop2` (`DB_USER=qinno_develop2_app`, өөрийн нууц үг)
тавьсан, AWS key шинэчлэхгүй гэсэн. Үүнийг кодод тусгасан (тест: 634 passed):

- `drizzle-kit push` зөвхөн `.env`-ийг уншдаг (`dotenv.config()`), seed-үүд `.env.local`-ийг түрүүлж уншдаг.
  Тиймээс `.env.local` бүрэн (6 `DB_*`) бол orchestrator түүнийг өгөгдөл болгон уншиж `DB_*`-ийг post step-ийн
  subprocess env-д өгнө (dotenv дарж бичихгүй → production `.env` хүрэхгүй). `sync-secrets`/`sst`/AWS алгасна:
  `PostStepDef.local_command` = `cd services/migrate-service && npx --no-install drizzle-kit push | tsx src/seeds/<x>.ts`.
- Preflight `DB_NAME`: `.env.local` байвал түүнийг шалгана (`qinno` → PRODUCTION FAIL; түлхүүр дутвал FAIL).
  AWS шалгалт зөвхөн `.env.local` байхгүй (sync-secrets зам) үед. Нууц үг лог/state.db/event-д орохгүй (redact).
- **Үр дагавар:** task файлын T1-ийн "Syncing secrets for stage: develop2" мөр энэ замд гарахгүй. `.sst/stage=develop2`
  шалгалт хэвээр.

Бодит preflight (read-only, qinno `git status` цэвэр, HEAD `75d22606`): 9/9 PASS —
`DB_NAME=qinno_develop2 (services/migrate-service/.env.local)`, `.sst/stage develop2`,
`AWS: not needed`, codex/claude/git/memory/power PASS.

## Pilot-ын өмнө үлдсэн зүйл

1. **Auth: claude.ai нэвтрэлт (хэрэглэгч шийдсэн, 2026-10-30).** `.env.local`-ийг хөндөхгүй, `ORCHESTRATOR_IGNORE_ENV_LOCAL=1` тавьж ажиллуулна (key уншигдахгүй → scrub flag байхгүй → `auto`/`plan` идэвхтэй). _Өмнөх асуудал:_ `.env.local`-ийн `ANTHROPIC_API_KEY` `Credit balance is too low` (billing_error) өгч байсан.
   Кредит нэмэх, эсвэл project-root `.env.local`-ийг түр салгаж claude.ai нэвтрэлтээр явах.
   Анхаар: key тавьсан үед scrub flag-аас `auto`/`plan` горим `default` болно (docs/notes/agent-sdk.md §11).
2. Таны шууд зөвшөөрөл ("pilot эхэл") ба хажууд хянах.

## Ажиллуулах

```sh
uv run orchestrator preflight --pipeline pipelines/qinno-rbac-v2.yaml    # бүгд PASS байх ёстой
ORCHESTRATOR_IGNORE_ENV_LOCAL=1 uv run orchestrator run pipelines/qinno-rbac-v2.yaml --only S1
uv run orchestrator status
```

## Pilot-д бодитоор шалгаж тэмдэглэх (энд бөглөнө)

| Асуулт | Хаанаас харах | Үр дүн |
|---|---|---|
| `auto`/policy горим `drizzle-kit`, `aws`-ийг блоклох уу | `events` `policy_deny`/`blocker` | **Ажиглагдаагүй**: ③ BLOCKED болсон тул агент `drizzle-kit`/`aws` дуудаагүй. Горим: ① `plan`, ②③ `auto` жинхэнээсээ идэвхтэй |
| qinno cwd-д local memory MCP, hook-ууд ачаалагдах уу | `events` `session_init` (`mcp_servers`) | **Хэсэгчлэн**: init-д `memory`, `codebase-memory`, `codex`, `shadcn` жагсаалтад орсон (qinno-ийн project-scope тохиргоо уншигдсан), гэхдээ init үед бүгд `pending` — холбогдсон эсэх баталгаагүй. Hook ачаалагдсан эсэх init-ээс харагдахгүй |
| S1-ийн schema засвар (roles/users/merchant-users) BLOCKED болохгүй юу | `steps` phase 3, `events` | **Тийм, BLOCKED болоогүй**: ②-д гурван файл засагдсан. Харин `operations.ts`-ийг `sed -n`-ээр УНШИХАД hard stop (policy false positive, засагдсан) |
| `post_steps` develop2 push + 4 seed, 2 дахь `seed:merchant` 0/0 | `events` `post_step_output` | **Хүрээгүй** (③ BLOCKED). "0 added / 0 removed" regex таамаг хэвээр |
| Зардал, хугацаа | `status` | $1.63; 07:51→08:01 UTC (10 мин): ① 3 мин $0.46 · ② 6.5 мин $0.91 · ③ 0.5 мин $0.26 |
| Policy-ийн turn зарцуулалт | `events` `policy_deny` | ① 4 (`sed`, засагдсан), ② 1 (`python3 -I - <<EOF` heredoc — хэвээр) |

## Pilot-ын явц (2026-10-30, run 1, хэрэглэгч terminal дээр ажиллуулсан)

| Алхам | Үр дүн |
|---|---|
| ① plan | done, $0.46, 23 turn, 4 policy татгалзал (бүгд `sed`), горим `plan` идэвхтэй, `api_key_source: none` |
| ② contract | done, $0.91, 50 turn, горим `auto` идэвхтэй; qinno-д schema/type/SDL/contract-тест бичсэн |
| ③ Claude track | **BLOCKED** (07:54→08:01 UTC, $0.26, 7 turn): `policy hard stop: …/schemas/core/operations.ts is a database schema file outside this session's contract` |

**Шалтгаан = orchestrator-ийн policy-ийн false positive (миний алдаа), агентын буруу биш.** Хориглогдсон тушаал нь
`sed -n 20,40p packages/db/src/schemas/core/operations.ts` буюу УНШИХ тушаал. `sed` нь read-only жагсаалтад байхгүй
(зориуд: `-i`, `w file`) тул тушаал дахь бүх зам "бичих боломжтой" гэж тооцогдож, schema гэрээний hard stop үүссэн.
① plan-ийн 4 татгалзал ч мөн энэ.

**Засвар:** `policy._sed_print_only` — зөвхөн `sed [-nEsr] N,Mp file` хэлбэр read-only. `-i`, `w`/`e`, `-f`, `-e`, бусад
скрипт өмнөх шигээ бичих гэж тооцогдоно. Мөн нэг цоорхой олж хаасан: `sed -n '1w <schema>' x.ts` (скриптийн дотор
файлын операнд) өмнө нь Allow байсан → одоо sed скриптийн токен бүрийг шалгана. Тест: 653 passed. Бодит S1 policy-оор:
өмнөх тушаал Allow; `sed -i …operations.ts`, `sed -n '1w …operations.ts'`, `>> …operations.ts` Deny (hard stop);
`roles.ts` (allowlist-д байгаа) Allow.

**Нээлттэй:** ③ алхамыг `resume --retry`-ээр шинэ сесстэй дахин ажиллуулна. qinno working tree-д ②-ийн (мөн ③-ийн
эхэн) өөрчлөлт үлдсэн (commit хийгдээгүй).


## S4: run 1 дуусгалт (2026-10-30, `resume --retry`) — бодит үр дүн

Run 1 `done`, $5.07 / $100, 07:51 → 10:15 UTC (wall 2 ц 24 мин; хөлдөлт, mock засвар, гар push-ийн хүлээлт орно).

| Алхам | Үр дүн |
|---|---|
| ① plan | done, $0.46, 1 оролдлого |
| ② contract | done, $0.91 |
| ③ Claude track | done, $1.49, 5 оролдлого (1 BLOCKED sed false positive, 1 Mac хөлдөлтөөр тасарсан, repair-ууд) |
| ④ Codex | done, $0 (USD байхгүй): **qinno дээр бодитоор ажилласан** — 13 файл, 845k input token (802k cached), 7.3k output, 38 event, ~2.5 мин, allowlist давсан |
| ⑤ verify + gates | done, $2.22, 5 оролдлого: 1-р удаа `role-key.contract.test.ts` mock алдаагаар BLOCKED (хүн засав), дараа нь memory gate-д 2 repair round |
| post_steps | `drizzle_push_develop2` (гараар, доор) → `seed_manager` → `seed_finance` → `seed_customer` → `seed_merchant` бүгд exit 0 |

### Pilot-ын асуултуудын бодит хариулт

| Асуулт | Үр дүн |
|---|---|
| `auto` горим `drizzle-kit`/`aws`-ийг блоклох уу | **Ажиглагдаагүй**: агент `drizzle-kit`/`aws` дуудаагүй. Policy-д дүрэм бий (`policy.py`), тестээр баталгаатай, бодит блок харагдаагүй |
| qinno cwd-д MCP, hook ачаалагдах уу | **Тийм.** `memory`, `codebase-memory`, `codex`, `shadcn`, `shadcn-kitchen`: сесс эхлэхэд зарим нь `pending`, дараа нь `connected`; ⑤ дээр `memory` MCP бодитоор бичсэн (`graph_write` event). Hook-ууд **ачаалагдсан**: qinno `typecheck.sh` (Edit бүрийн дараа) ба `verify-tests.sh` (turn бүрийн төгсгөлд `vitest --changed HEAD`) — Mac-ийг хүнд ачаалсан (доор). `mcplens` qinno-д тохируулагдаагүй |
| S1-ийн schema засвар BLOCKED болох уу | Үгүй (roles/users/merchant-users засагдсан). `operations.ts` унших `sed -n` false positive-ийг засав |
| post_steps | **Дараах эх сурвалж/засваруудтай** (доор): push-ийг гараар хийв; seed 4 амжилттай |
| **2 дахь `seed:merchant` "0 added / 0 removed"** | **Pipeline `seed_merchant_develop2`-ийг НЭГ удаа л жагсаасан** (S3 handoff-д "хоёр удаа" гэснээс зөрүү) тул 2 дахь ажиллагаа бодитоор болоогүй. Бодит merchant seed-ийн гаралт `0 added / 0 removed` гэсэн үг агуулаагүй (`(0 new permission links)`, `(0 inserted)`) → хуучин regex таарахгүй байсан тул **заасан**: бүх тоо 0 (`N new/inserted/added/removed`), ядаж нэг тоо. Бодит гаралтаар тестлэсэн. 2 дахь ажиллагаа pipeline-д нэмэгдээгүй |
| Зардал, хугацаа | $5.07; ①0.46 ②0.91 ③1.49 ④0.00 ⑤2.22 |
| Policy-ийн turn зарцуулалт | 51 `policy_deny`: heredoc 17, `for … $c` 9, `python -c` inline 5, SubagentHandback 5, `sed` 4 (засагдсан), `mcp__memory__add_observations` 4, `mcp__codex__codex` 3, repo-гоос гадуур бичих 2 (confidence.jsonl), бусад 2 |

### Дайралт хийсэн орчестраторын алдаанууд (S4-д засав / нээлттэй)

1. **`drizzle-kit push` TTY-д унаад exit 0** (зассан): qinno `drizzle.config.ts` `strict: true` → "push хийх үү?" асуулт TTY шаардана; stdin `/dev/null` → алдаа хэвлээд exit 0, orchestrator done гэж бүртгэсэн, DB өөрчлөгдөөгүй. Одоо `Error:` / `Interactive prompts require a TTY` гарвал BLOCKED, done гэж бүртгэхгүй (тест). **Нээлттэй:** unattended push-ийг яаж баталгаажуулах (handoff-s4).
2. **Post step гаралтыг сүүлийн 12 мөрөөр таслах** — push-ийн бүтэн төлөвлөгөө алдагдсан (нээлттэй).
3. **Policy ⑤ repair-ийг sidecar-аас хориглосон:** gate `confidence.jsonl` шинэчлэлт шаардана, харин policy repo-гоос гадуур бичихийг, heredoc/`python -c`-г хориглоно → $1.7 зарцуулсан (нээлттэй, policy сулруулах шийдвэр).
4. **Mac хөлдөлт:** qinno hook-ууд (typecheck, vitest ~9 worker) + суурь санах ойн дарамт (swap 4.6/6 GB, ChatGPT/Codex app-ын 100% CPU helper, хуучин MCP үлдэгдэл). Яг шалтгаан батлагдаагүй. `nice -n 15` ашигласнаас хойш дахин хөлдөөгүй.

### Тэмдэглэл

- Push-ийг хэрэглэгч TTY-тэй terminal дээр `.env.local`-ийн DB_*-тэй гараар хийсэн: төлөвлөгөө 23 statement, бүгд нэмэлт/metadata (S1: `roles.key`, `users.pin_lookup`, `merchant_users.all_branches` + 2 index; бусад 15 нь HEAD schema ба develop2-ийн урьдын зөрүү: `plans.system`, `features.system`, 13 `SET DEFAULT`). "Changes applied" гаралт надад харагдаагүй; seed-үүд `roles.key` шаардах тул амжилттай ажилласан нь push хэрэгжсэнийг батлана.
- `seed_finance` develop2-д демо өгөгдөл үүсгэсэн (finance@qinno.mn, demo merchant, 3 branch, subscription, 55 данс, 2026 санхүүгийн жил).
- Qinno working tree: 47 зам dirty (39 өөрчлөгдсөн, 732+/173−), commit хийгдээгүй, HEAD `75d22606`.
