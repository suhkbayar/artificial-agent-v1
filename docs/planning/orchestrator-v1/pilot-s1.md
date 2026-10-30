STATUS: NOT RUN

# Pilot `--only S1` на qinno — ажиллаагүй (2026-10-30, S3)

Pilot-д **ороогүй**. Шалтгаан: таны өгсөн нөхцөлүүд (S3 task, 5-р заавар) биелээгүй. `orchestrator preflight
--pipeline pipelines/qinno-rbac-v2.yaml` бодит гаралт (2026-10-30, S3-ын төгсгөл):

```
FAIL  migrate-service DB_NAME  DB_NAME=qinno is PRODUCTION
PASS  .sst/stage               stage=develop2
FAIL  AWS credentials          sts get-caller-identity failed: InvalidClientTokenId
PASS  codex exec / claude version (2.1.283) / codex version (0.147.0) / git state (feature-rbac-v2@75d22606 clean)
PASS  memory MCP (67 entities readable, no lock, no leaked processes) / power (AC)
```

qinno repo-д зөвхөн уншсан (`git status` цэвэр, HEAD `75d22606` — S1/S2-тэй адил).

## Pilot-ын өмнө хэрэглэгч хийх зүйл

1. `services/migrate-service`-д `npm run sync-secrets -- --stage=develop2` → `DB_NAME=qinno_develop2`.
2. AWS static key шинэчлэх (`aws sts get-caller-identity` амжилттай болтол).
3. **Auth шийдвэр:** `.env.local`-ийн `ANTHROPIC_API_KEY` одоо `Credit balance is too low` (billing_error) өгч байна.
   Кредит нэмэх, эсвэл `.env.local`-ийг түр салгаж claude.ai нэвтрэлтээр явах (loader файл байхгүй бол автоматаар).
   Анхаар: key тавьсан үед scrub flag-аас болж `auto`/`plan` горим `default` болно (docs/notes/agent-sdk.md §11) —
   plan алхам policy-оор read-only хэвээр, харин `auto`-ын classifier давхарга алга.
4. Таны шууд зөвшөөрөл ("pilot эхэл") ба хажууд хянах.

## Ажиллуулах

```sh
uv run orchestrator preflight --pipeline pipelines/qinno-rbac-v2.yaml    # бүгд PASS байх ёстой
uv run orchestrator run pipelines/qinno-rbac-v2.yaml --only S1
uv run orchestrator status
```

## Pilot-д бодитоор шалгаж тэмдэглэх (энд бөглөнө)

| Асуулт | Хаанаас харах | Үр дүн |
|---|---|---|
| `auto`/policy горим `drizzle-kit`, `aws`-ийг блоклох уу | `events` `policy_deny`/`blocker` | _pilot-оор_ |
| qinno cwd-д local memory MCP, hook-ууд ачаалагдах уу | `events` `session_init` (`mcp_servers`, `permission_mode`) | _pilot-оор_ (toy/төслийн cwd-д ачаалагддагийг §11-д баталсан; qinno cwd харагдаагүй) |
| S1-ийн schema засвар (roles/users/merchant-users) BLOCKED болохгүй юу | `steps` phase 3, `events` | _pilot-оор_ (offline: policy хориглохгүй гэдгийг qinno-ийн Ownership блокоор тестэлсэн) |
| `post_steps` develop2 push + 4 seed, 2 дахь `seed:merchant` 0/0 | `events` `post_step_output` | _pilot-оор_ (seed-ийн хэвлэх мөрийн формат тодорхойгүй; "0 added … 0 removed" regex таамаг) |
| Зардал, хугацаа | `status` | _pilot-оор_ |
