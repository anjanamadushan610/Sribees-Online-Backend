# Testing and the release gate

How SRIBEESonline is tested, what has to pass before code reaches a server, and
how to run the same checks locally. Covers all four repositories: this backend,
`admin`, `partner-portal` and `mobile`.

## The rule

**Nothing deploys unless CI is green.** Production (`deploy-prod.yml`)
and development (`deploy-dev.yml`) both call `deploy.yml` as their first job and
build nothing until it passes. (Before 2026-09-25 CI and deploy ran side by
side, and `main` shipped for days with CI red.)

## Layers

| Layer | Where | What it proves | Runs |
|---|---|---|---|
| Unit | `tests/test_*.py` | Pricing, cancellation rules, returns, loyalty, courier contract parsing | every push |
| Security | `tests/security/` | Every route's auth, JWT forgery, RBAC, OTP limits, uploads, headers, CORS, rate limits | every push |
| Integration | `tests/integration/` | The money path end to end over HTTP, against real Postgres and Redis | every push |
| Deploy rehearsal | `deploy.yml` → `rehearsal` | The production image boots on an empty DB, every migration applies **twice**, the smoke test passes, production hardening is on | every push |
| Smoke | `scripts/smoke_test.py` | Admin login, analytics answer and agree with each other, catalog is not empty | rehearsal, and on the server after every production deploy |
| Public edge | `deploy-prod.yml` → `verify-public` | DNS, TLS, nginx and the app together: health, HSTS, no public docs, admin routes refuse anonymous calls | after every production deploy |
| Browser E2E | `admin/e2e/` | Real browser, real backend, seeded data: auth, dashboards, analytics, every route opens, RBAC from the browser and the API | admin pushes |
| Frontend unit | `admin/src/**/*.test.ts(x)`, `partner-portal/src/**` | Permissions, route guards, the API client's token refresh and logout, money and Sri Lanka time formatting | every push |
| Mobile | `mobile/test/` | Models, providers, widgets; every English string has Sinhala and Tamil with the same `{placeholders}`; NIC rules match the backend | every push |

### What the security suite pins

- **Auth matrix** (`test_route_auth_matrix.py`): derives each route's realm from
  its dependencies, then proves over HTTP that anonymous and wrong-realm tokens
  are refused, for every route. The list of public routes is pinned: a new one
  fails CI until it is added to `PUBLIC_ROUTES` with a reason.
- **Admin authorisation** (`test_admin_authorization.py`): an admin route
  needs a role check, permission check or branch scoping, not just "is an
  admin". Super-Admin-only routes are checked against the other five roles.
- **Tokens**: wrong key, expired, `alg: none`, refresh-as-access and malformed
  headers are refused, and errors leak nothing.
- **OTP**: single use, expiry, 5 wrong guesses burn the code, a resend does not
  reset the count, and the test code (`123456`) verifies only for numbers in
  `OTP_TEST_PHONES` (SEC-001). In production that list comes from the
  `OTP_TEST_PHONES` Actions secret; left unset, no number can use it.
- **Smoke credentials**: the post-deploy smoke test signs in as the Super Admin
  in the `SMOKE_ADMIN_EMAIL` / `SMOKE_ADMIN_PASSWORD` Actions secrets, layered
  onto the server's `.env` in deploy step 4b like `OTP_TEST_PHONES`. Change the
  production admin password and these secrets together, or the deploy fails at 7c.
- **Uploads**: nothing is stored under an executable or document extension,
  and nothing escapes the media root.

### The integration world

`tests/integration/conftest.py` builds a fresh, isolated world per test: an
unlocked province, district and launched postal area, a branch serving them, a
stocked product, and customers and admins with real sessions. Only the
SribeesExpress courier is faked, at the HTTP layer
(`tests/integration/fake_courier.py`), so the courier client's own code still
runs. These tests write, so they refuse to run unless the database name
contains `test`.

## Running it locally

Backend, with Docker:

```bash
docker run -d --name sbq-pg -e POSTGRES_USER=test_user -e POSTGRES_PASSWORD=test_password \
  -e POSTGRES_DB=test_db -p 55432:5432 pgvector/pgvector:pg15
docker run -d --name sbq-redis -p 56379:6379 redis:7-alpine

python -m venv .venv311 && .venv311/bin/pip install -r requirements-dev.txt   # Python 3.11, as CI
export DATABASE_URL=postgresql+asyncpg://test_user:test_password@localhost:55432/test_db \
       REDIS_URL=redis://localhost:56379/0 JWT_SECRET_KEY=local APP_ENV=testing
pytest tests/ -o addopts="" -q --cov=app          # everything
pytest tests/security -o addopts="" -q            # no database needed
ruff check .                                       # the whole repo, like CI
bandit -r app -ll -ii
```

Use Python 3.11 with `requirements-dev.txt`. A newer interpreter resolves
newer FastAPI/SQLAlchemy/Starlette and can hide (or invent) failures CI will
see differently. Without Docker, `uv venv --python 3.11` plus a local Postgres
database whose name contains `test`, and
`fakeredis.TcpFakeServer(("127.0.0.1", 56380))` as the Redis, works too.

Admin: `npm test`, `npm run test:coverage`, `npm run typecheck`, `npm run build`;
E2E per `playwright.config.ts`. Mobile: `flutter analyze --no-fatal-infos && flutter test`.

## Gates and baselines

- **Coverage floor**: `COVERAGE_FLOOR` in `deploy.yml` (backend) and
  `thresholds` in `admin/vitest.config.ts`. They are ratchets: raise them as
  coverage rises, and never lower them to get a build out.
  The backend floor is 80% (82.5% measured on 2026-09-26). Coverage traces
  greenlets (`[tool.coverage.run]` in `pyproject.toml`): SQLAlchemy's async
  session runs queries in one, and without it every line after a handler's
  first query counted as never run -- the old 62-66% figures were that.
- **Dependency CVEs**: `pip-audit` ignores exactly the IDs in
  `.github/pip-audit-baseline.txt` (empty), so any advisory on what ships
  fails CI.
- **Secrets**: gitleaks scans the full history of each repository. Reviewed
  false positives are allowlisted in `.gitleaks.toml`, each with its reason.
- **SAST**: bandit at medium severity and confidence. A false positive gets
  `# nosec <id>` beside the reason.
- **Advisory for now** (report, do not fail): mypy (large backlog), the admin
  ESLint run (~90 `no-explicit-any` in pages), and the Trivy image scan. Make
  each one blocking once its backlog is cleared.

## Setup the repository owner has to do

1. **Branch protection on `main`** in `admin`, `partner-portal` and `mobile`:
   require the `CI / quality` and `CI / security` checks (and `E2E` for admin).
   Vercel deploys on its own, and this is what stops it deploying a red commit.
2. `PUBLIC_API_URL` repository variable in the backend repo, if the API ever
   moves off `https://api.sribees.com` (the public-edge check uses it).

## Dependencies

The `pip-audit` baseline has been empty since the 2026-09-26 upgrade: FastAPI
0.136.3 on Starlette 1.x, python-multipart 0.0.32, python-jose replaced by
PyJWT, gunicorn 26, sentry-sdk 2, pydantic 2.13, and the never-imported
fastapi-mail, google-generativeai, slowapi, aiofiles, alembic and
python-dateutil removed (fastapi-mail had been the only source of
email-validator, so `pydantic[email]` now names it).

FastAPI is held at 0.136.3 on purpose. From 0.137 an included router is kept
as one lazy `_IncludedRouter` entry, so `app.routes` no longer lists the
real routes, and the security route matrix (and several tests) walk exactly
that list. Moving past it means rewriting that walk first.
