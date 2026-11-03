# Дараагийн сессийн prompt (S6: S2 үр дүн → S3–S6 → artificial-agent-ийг дуусгаж qinno дээр ажиллуулах)

Доорх блокийг шинэ Claude Code сесст (`artificial-agent-v1` хавтаст) бүтнээр нь өг.
Өмнөх (S5) prompt-ыг энэ файл орлоно: S5-ийн код/шийдвэр `handoff-s5.md` ба graph-д байгаа.

---

```text
Agent Orchestrator (~/Documents/Projects/artificial-agent-v1) — S6: S2-ийн үр дүнг хаах, S3–S6-г qinno-rbac-v2 дээр
шатлан ажиллуулах, ингэж artificial-agent-ийг дуусгаж qinno дээр бодитоор ашиглах төлөвт хүргэнэ.

Эхлээд унш:
- docs/planning/orchestrator-v1/handoff-s5.md (эхний мөр STATUS: DONE | BLOCKED; S2 ажилласан эсэх, "Хүлээгдэж буй")
- docs/planning/orchestrator-v1/handoff-s4.md ("Нээлттэй"), pilot-s1.md ("S4: run 1 дуусгалт")
- Graph: open_nodes(["Agent Orchestrator",
  "Decision: Agent Orchestrator — S5 policy sidecar and prompt tightening",
  "Decision: Agent Orchestrator — S4 pilot S1 completion and go/no-go"])
- pipelines/qinno-rbac-v2.yaml (S2–S6, `open_items`, S6 `handoff` TODO)
- readme.md §2 (O1–O13), §5 (v1/v2 хүрээ), §6 (policy)

Суурь шалгалт (кодонд хүрэхээс өмнө):
- `uv sync --reinstall-package orchestrator`; `uv run pytest` (674 passed, 7 skipped эсвэл түүнээс дээш);
  `uv run ruff check .`; `uv run ruff format --check .`
- `uv run orchestrator status` — run-уудын төлөв, зардал.
- `uv run orchestrator preflight --pipeline pipelines/qinno-rbac-v2.yaml` — 9/9 PASS байх ёстой.
  `power (Mac)` FAIL бол цэнэглэгч залгах, `caffeinate -i` гэж надад хэл (орчны асуудал, кодын биш).

Ажил:
0. Эхлэх цэгийг тогтоо. handoff-s5.md-ийн дагуу:
   - S2 ХАРААХАН ажиллаагүй (STATUS: BLOCKED, "Хүлээгдэж буй") бол S5-ийн 3–5-р алхмыг үргэлжлүүл: S2-ийн
     тушаалыг (`ORCHESTRATOR_IGNORE_ENV_LOCAL=1 caffeinate -i nice -n 15 uv run orchestrator run
     pipelines/qinno-rbac-v2.yaml --only S2`) надад өг, өөрөө бүү ажиллуул. Би "дууслаа" / "blocked" гэхэд
     state.db (`events`, `steps`, `attempts`) ба logs/-оос уншиж тайлагна.
   - S2 ажилласан бол үр дүнг (step хүснэгт, зардал, repair round, policy_deny-ийн тоо ба төрөл —
     S1-д 51 байсан, 2 дахь `seed_merchant_develop2`-ийн `_is_noop` бодит гаралттай таарсан эсэх, Mac) тайлагна.
   BLOCKED / failed бүрийг оношил: orchestrator-ийн алдаа бол тесттэй засаад, юу засснаа хэлж, дахин
   ажиллуулахаас өмнө надаас асуу; агент/task-ийн асуудал бол надад тайлагна, qinno-д өөрөө бүү зас.
   Qinno-г зөвхөн оношлогоонд зайлшгүй үед уншина (`git -C ~/Documents/Projects/qinno status --short`, `diff --stat`).
1. "artificial-agent дууссан" гэдгийн шалгуурыг надтай тохир (нэг AskUserQuestion). Санал болгох шалгуур:
   (i) S2–S6 бүгд `done`, S6-ийн handoff бичигдсэн; (ii) pipeline-ийн `open_items` ба `TODO`-ууд хаагдсан эсвэл
   зориуд хойшлогдсон гэж бичигдсэн (S6 `handoff` нэр, signing/updater-ийн яг зам); (iii) readme-ийн v1 хүрээ
   биелсэн, v2 (Playwright E2E, утаснаас steer, CI auto-fix, Docker sandbox) тусдаа үлдсэн; (iv) `orchestrator
   watch` ба `status` ажиллах; (v) pytest/ruff цэвэр. Өөр шалгуур нэмэх/хасахыг надаас асуу.
2. Өмнөх нээлттэй зүйлсийг шалга, хэрэгтэйг нь л хий (тус бүрийг дуусгах эсвэл "хойшлуулсан, учир нь…" гэж тэмдэглэ):
   - pipeline-ийн `TODO handoff-s6.md`: rbac-v2 README S6-д handoff заагаагүй → gate юу уншихыг qinno-ийн
     docs/planning/rbac-v2/-ээс (зөвхөн унших) тодорхойл, pipeline-д яг нэрийг тавь.
   - signing/updater-ийн яг зам (`policy.forbidden_write_globs`) — task файлуудаас олдвол тавь, үгүй бол
     өргөн хамгаалалт хэвээр гэж тэмдэглэ (таамаглаж болохгүй).
   - Unattended `drizzle-kit push` (S2–S6-д push байхгүй тул яаралтай биш) — шаардлагагүй бол хойшлуул.
   - `/codex-review` S5-д prompt-оос хасагдсан; Codex-ийн independent review шаардлагатай эсэхийг S6-ийн
     task файлаас хар, хэрэгтэй бол өөр аргыг (жишээ нь ④-ийн Codex track-д review хэсэг) санал болго.
3. S3–S6-г ажиллуулах төлөвлөгөө. Миний тодорхой "тийм"-гүйгээр `--only`-гүй `run`-ийг бүү эхлүүл.
   Анхдагч санал: S2 цэвэр дууссан бол `--only S3`, дараа нь S4 (seed_merchant ×2), S5, S6-ийг нэг нэгээр,
   тус бүрийн дараа үр дүнг тайлагна. S2–S4 цэвэр, repair/policy_deny тогтвортой байвал үлдсэнийг нэг дор
   ажиллуулах саналыг (зардал ≈ $3–5/сесс, нийт $100-ын хязгаарт хол) тоотой хамт өг. Тушаал бүрийг
   надад өг, би өөрийн terminal дээр ажиллуулна.
4. Сесс бүрийн дараа (S3–S6) тайлагна: step хүснэгт, зардал, хугацаа, repair round, policy_deny (төрлөөр),
   post_steps, Mac. Policy deny-ийн шинэ давтагдсан төрөл гарвал prompt/policy-д чангатгал санал болго
   (сулруулах бол эхлээд асуу).
5. Дуусгалт — qinno дээр ашиглахад бэлэн болгох:
   - Шалгуур (1) биелсэн эсэхийг баталгаажуул; үлдсэн зүйлийг нэрлэ.
   - Production-тэй холбоотой бүх зүйл (migrate, seed, deploy — O8; develop2 `sst deploy` — O13) ХЭРЭГЛЭГЧИЙН
     гарт. Orchestrator зөвхөн develop2 `post_steps`-ийг ажиллуулна. Release runbook-ийг (S6 Claude track
     бичдэг) уншиж, надад "production-д юу хийх" жагсаалтыг тайлагна, өөрөө бүү хий.
   - qinno-ийн working tree-г (dirty зам, HEAD) тайлагна; commit/push/PR миний хэрэг.
   - Дараагийн qinno pipeline (rbac-v2-оос бусад) ажиллуулах заавар: pipeline YAML бичих, preflight,
     `run --only`, `watch`, `resume` — readme-д товч "Хэрхэн ашиглах" хэсэг нэм (байхгүй бол).

Дүрэм:
- Commit хийхгүй; commit, GitHub remote-ийн талаар надад бүү сануул (би өөрөө хийнэ).
- Нууц (DB_PASSWORD, API key) лог, state.db, event, хариултад хэзээ ч бүү гарга.
- Policy-г зөвхөн чангатгана; сулруулах бүх өөрчлөлтийг эхлээд надаас асуу (AskUserQuestion, үнэ/эрсдэлтэй,
  санал болгох сонголт эхэнд).
- Графт: шинэ шийдвэр = "Decision: Agent Orchestrator — <сэдэв>", ижил төстэй нэр байгаа эсэхийг эхлээд шалга;
  entity үүсгэх/шинэчлэх бүрд confidence.jsonl-ийн мөрийг тэр ээлжинд шинэчил. `read_graph` бүү дууд.
- Кодын өөрчлөлт бүр тесттэй; Gate: `uv run pytest`, `ruff check`, `ruff format --check` цэвэр.
- Төгсгөлд docs/planning/orchestrator-v1/handoff-s6.md бич: эхний мөр `STATUS: DONE` эсвэл `STATUS: BLOCKED`,
  бодит гаралтыг нуулгүй тайлагна (S2–S6-ийн step хүснэгт, зардал, засварууд, шалгуур (1)-ийн биелэлт,
  qinno дээр ашиглах заавар, үлдсэн зүйл). handoff-s5.md-ийг S2-ийн бодит гаралтаар шинэчил.
```
