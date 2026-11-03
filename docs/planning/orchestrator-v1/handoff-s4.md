STATUS: DONE

# handoff-s4 (2026-10-30)

Зорилго: qinno-rbac-v2 pilot-ын S1-ийг дуусгах, S2–S6 хүнгүй ажиллуулах go/no-go бэлдэх.
Үр дүн: **run 1 `done`** (S1 ①–⑤ + post_steps develop2), $5.07 / $100, бодит үр дүн `pilot-s1.md` "S4: run 1 дуусгалт".
`run` (`--only`-гүй) эхлүүлээгүй. Commit хийгээгүй.

## Gate

- `uv run pytest` → **664 passed, 7 skipped** (эхлэхэд 653; +4 watch, +1 push-алдаа, +6 no-op параметр). `ruff check` / `ruff format --check` цэвэр.
- `preflight` 9/9 PASS (resume-ийн өмнө, qinno dirty).
- `status` → run 1 `done`: ①0.46 ②0.91 ③1.49 ④0.00 (Codex, 845k input token) ⑤2.22 = $5.07.

## Хийсэн

1. **Heredoc (хэрэглэгчийн шийдвэр): хориг хэвээр.** Policy өөрчлөгдөөгүй. Graph-д бичсэн.
2. **Resume-ийг хэрэглэгч terminal дээрээ ажиллуулсан**; би state.db-гээс явцыг уншиж оношилсон.
3. **`orchestrator watch`** (шинэ, `orchestrator/watch.py`): `state.db`-г `mode=ro`-оор уншдаг localhost:8765 хуудас. Зөвхөн 127.0.0.1, `redact` + 700 тэмдэгтийн таслалт. 4 тест.
4. **Засвар (flow.py, тесттэй):**
   - `drizzle_push_*` алхам exit 0 буцаасан ч гаралтад `Error:` / `Interactive prompts require a TTY` байвал BLOCKED, done гэж бүртгэхгүй. Шалтгаан: qinno `drizzle.config.ts` `strict: true` → TTY шаардана; stdin `/dev/null` → алдаа хэвлээд exit 0, DB өөрчлөгдөөгүй байхад done гэж бичигдсэн.
   - 2 дахь seed-ийн no-op шалгалт: хуучин regex (`0 added … 0 removed`) бодит merchant seed-ийн гаралттай **таарахгүй** байсан (`(0 new permission links)`, `(0 inserted)`). Одоо `_is_noop`: `N new|inserted|added|removed` тоо бүр 0, ядаж нэг тоо. Бодит merchant (no-op) ба finance (эхний ажиллагаа) гаралтаар тестлэсэн.
5. **Pilot-ын асуултууд хаагдсан** (`pilot-s1.md`): qinno cwd-д MCP (memory, codebase-memory, codex, shadcn) `pending` → `connected`, ⑤ дээр `memory` бодитоор бичсэн; hook-ууд ачаалагдсан; Codex qinno дээр бодитоор ажилласан.

## Нуулгүй: юу таарсан, юу засагдаагүй

- **2 дахь `seed:merchant` бодитоор ажиллаагүй.** Pipeline S1-д `seed_merchant_develop2`-ийг НЭГ удаа л жагсаасан (S3 handoff "хоёр удаа" гэснээс зөрүү). S2, S4 хоёр удаа жагсаасан — тэнд no-op шалгалт идэвхтэй. Шинэ `_is_noop` бодит no-op-тэй тохирсон ч **S2/S4-ийн жинхэнэ 2 дахь ажиллагаан дээр баталгаажаагүй**.
- **Push-ийг орчестратор автоматаар хийж чадсангүй** (TTY). Хэрэглэгч гараар (`.env.local`-ийн DB_*-г shell env болгож) хийсэн. Төлөвлөгөө 23 statement, бүгд нэмэлт/metadata (S1-ийн 5 + HEAD schema-ийн урьдын зөрүү 15: `plans.system`, `features.system`, 13 `SET DEFAULT`). "Changes applied" гаралт надад харагдаагүй; `roles.key` шаардах seed-үүд амжилттай → push хэрэгжсэн.
  Анхны гаралтыг 12 мөрөөр таслах (`post_step_output` tail) нь төлөвлөгөөг нуусан — миний өмнөх "яг 5 мөр" гэсэн дүгнэлт тэр таслагдсан хэсэг дээр үндэслэсэн, **алдаатай** байсан (хэрэглэгчийн бүтэн төлөвлөгөөгөөр залруулсан).
- **`seed_finance` develop2-д демо өгөгдөл үүсгэсэн** (finance@qinno.mn, demo merchant, 3 branch, subscription, 55 данс, 2026 санхүүгийн жил).
- **Policy ⑤ repair-ийг sidecar-аас хориглосон:** gate `confidence.jsonl` шинэчлэлт шаардана; харин policy repo-гоос гадуур бичих (`confidence.jsonl`), heredoc, `python -c`, `mcp__memory__add_observations` (repair сесст) хориглоно → ⑤-д 2 repair round, ~$1.7. Эцэст нь давсан ч яаж давсан нь event-д тодорхой харагдахгүй. **Policy-г сулруулах шийдвэр тул хөндөөгүй.**
- **51 policy_deny** (heredoc 17, `for … $c` 9, `python -c` 5, SubagentHandback 5, sed 4 (засагдсан), memory write 4, `mcp__codex__codex` 3, repo-гоос гадуур 2, бусад 2). Turn зарцуулсан, блоклоогүй.
- **`/codex-review` ажиллаагүй:** `mcp__codex__codex` unattended allow list-д байхгүй (3 удаа). Агент гараар review хийсэн. Codex review-ийн орлуулалт биш.
- **③-д агент qinno CLAUDE.md-ээс болж graph reconcile + `qinno-graph-reconciler` subagent-ийг ~9 удаа давтан оролдсон** (hook/policy хориглосон). Турн, зардал, санах ой үрсэн.
- **Mac хөлдөлт** (~08:29 UTC): qinno hook-ууд (`typecheck.sh` Edit бүрийн дараа, `verify-tests.sh` turn бүрийн төгсгөлд ~9 vitest worker 60–85% CPU) + суурь дарамт (swap 4.6/6 GB, ChatGPT/Codex app-ын 100% CPU `Codex (Service)`, хуучин MCP үлдэгдэл). Яг шалтгаан батлагдаагүй. Resume процесс үхэж, `state.db` `running` хэвээр үлдсэн ч `resume` сэргээсэн. Дараа нь `nice -n 15` ашигласнаас хойш хөлдөөгүй.
- Crash болсон дуудлагын зардал бүртгэгдээгүй (S2-ын хязгаар хэвээр) — $5.07 нь доод хязгаар.
- qinno: 47 зам dirty (39 өөрчлөгдсөн, 732+/173−), commit хийгдээгүй, HEAD `75d22606`.

## S2–S6 go/no-go

**Санал: `run` (бүх S2–S6)-ийг ОДООХОНД эхлүүлэхгүй (NO-GO); `run --only S2`-ээр шатлан GO (нөхцөлтэй).**

Давуу тал:
- S1 бүтэн ①–⑤ + post_steps дууссан: Claude + Codex qinno дээр бодитоор ажилласан, gates, graph audit, repair, resume (хөлдөлтөөс) бүгд ажилласан.
- Зардал: $5.07 (жишиг S1 хэд хэдэн BLOCKED/repair-тэй); S2–S6 тус бүр ≈$3–5 → ≈$15–25, $100-ын хязгаарт хол.
- Push зөвхөн S1-д (S2–S6-д `drizzle_push` байхгүй) → TTY-н асуудал S2–S6-д саад болохгүй. Schema засвар S2–S6-д hard stop хэвээр.

Нөхцөл (эхлэхээс өмнө хэрэглэгчийн шийдвэр):
1. **Policy ба sidecar зөрчил** (дээр): ⑤ repair-д sidecar бичих замыг тодорхой шийдэх (жишээ нь `confidence.jsonl`-ийг зөвхөн ⑤ verify агентад allowlist-ээр нээх, эсвэл orchestrator өөрөө мөрийг шинэчлэх). Үгүй бол сесс бүрд ~$1.7 зарцуулж магадгүй, эсвэл BLOCKED. **Сулруулах өөрчлөлт — танаас.**
2. **③/⑤ prompt**: "graph-д бүү бич (⑤ дээр л), subagent бүү дууд, `$c` for-loop бүү хий" — турн, зардал, санах ойг хэмнэнэ (чангатгал).
3. **Mac ачаалал:** `nice -n 15`, Codex/ChatGPT app хаах, `mem-watch` sampler. Эсвэл qinno hook-ийг агентын сесст `setting_sources`-аар салгах (тест давхардлыг арилгана; gates өөрөө тест ажиллуулдаг) — тусдаа шийдвэр.
4. **S2, S4 (seed_merchant ×2)** дээр no-op шалгалт бодитоор анх ажиллана → эхний ажиллагаанд ажиглах (`--only S2`, дараа нь `--only S3` гэх мэт).
5. `/codex-review`: `mcp__codex__codex` unattended allow list-д нэмэх эсэх (сулруулах шийдвэр) эсвэл pipeline-аас хасах.

## Нээлттэй (дараагийн сесст)

- `post_step_output` гаралтыг 12 мөрөөр таслахгүй болгох (төлөвлөгөө/алдаа алдагдаж байна).
- Unattended `drizzle-kit push`: хэрэгтэй бол "хоёр үе шат" (төлөвлөгөөг парс хийж нэмэлт statement-үүд үед л `--force`; `SET DEFAULT`-ыг жагсаалтад оруулах эсэх). S2–S6-д push байхгүй тул яаралтай биш.
- S1-ийн `seed_merchant`-ийг хоёр удаа жагсаах эсэх (task файл шаарддаг эсэх шалгах).
- `/codex-review` нь S1 handoff-д хийгдээгүй гэж тэмдэглэгдсэн.
- Commit/GitHub remote — хэрэглэгч өөрөө.
