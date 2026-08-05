# SRIBEESonline — Backend Deployment Guide

Full reference for how the **FastAPI backend** is built, tested, shipped, and run
in production — CI/CD pipelines, Docker images, environments, the AWS EC2 host,
Nginx/TLS, database migrations, and day-2 operations.

> This document describes the deployment setup **as it actually exists in the
> repository today**, including a few caveats/gaps worth fixing (see
> [§16 Known gaps & recommendations](#16-known-gaps--recommendations)).

---

## 1. TL;DR — what happens on `git push`

```
push to  main  of  sribees-backend
        │
        ├─▶ CI — Lint & Test         (.github/workflows/deploy.yml)
        │      ruff + mypy + pytest against ephemeral Postgres+Redis
        │
        └─▶ CD — Build & Deploy      (.github/workflows/deploy-backend-ec2.yml)
               1. Build production Docker image (multi-stage)
               2. Push to ghcr.io  →  ghcr.io/<owner>/sribees-backend:latest  + :<git-sha>
               3. SSH into EC2 (appleboy/ssh-action)
               4. Install Docker / Nginx / Certbot if missing
               5. Write .env  (from ENV_CONTENT secret)
               6. Render docker-compose.yml (from docker/ec2-docker-compose.yml)
               7. docker compose pull + up  (postgres, redis, fastapi)
               8. Run migrations (see §9)
               9. Configure Nginx reverse proxy → :8000
              10. Issue/renew Let's Encrypt TLS (Certbot) + renewal cron
```

End result: `https://<HOST_DOMAIN>/health` served by Nginx → gunicorn/uvicorn → FastAPI,
backed by PostgreSQL (pgvector) and Redis, all in Docker on a single EC2 instance.

---

## 2. Repositories & where CI runs

| Repo | URL | Role |
|---|---|---|
| **Backend (deploy)** | `github.com/anjanamadushan310/new-sribees-backend` | **Repo the CD pipeline runs from.** `fastapi_backend/` *is* this repo's root, so `.github/workflows/` sit at the top level. |
| **Admin dashboard** | `github.com/anjanamadushan310/new-sribees-frontend` | React admin dashboard (`admin/`). |
| **Mobile client** | `github.com/anjanamadushan310/new-sribees-mobile` | Flutter customer app (`mobile/`). |

> The three folders under the local `SRIBEESonline/` working directory are
> **independent git repos**, each with its own `origin`. There is no shared
> monorepo remote, so pushing `fastapi_backend/` publishes the backend and
> nothing else — the workflows fire from that repo's own root
> (they use `context: .` / `file: ./Dockerfile`).

**Branches**
- `main` → production (build image, deploy to EC2).
- `staging` → CI lint/test only in GitHub Actions; Jenkins additionally treats `staging` as a deploy branch.

---

## 3. Runtime architecture (production)

```
                         Internet (443/80)
                               │
                        ┌──────▼───────┐
                        │    Nginx     │  systemd service on the EC2 host
                        │  reverse     │  TLS via Let's Encrypt (Certbot)
                        │  proxy + SSL │  proxy_pass → 127.0.0.1:8000
                        └──────┬───────┘
        ┌──────────────────────┼──────────────────────┐
        │            Docker network: sribees_net       │
        │   ┌────────────────┐   ┌──────────────────┐  │
        │   │ fastapi_backend│──▶│  postgres_db     │  │
        │   │  (gunicorn +   │   │  ankane/pgvector │  │
        │   │   uvicorn, 4w) │   │  :5432 volume    │  │
        │   │   :8000        │   └──────────────────┘  │
        │   │                │──▶┌──────────────────┐  │
        │   └────────────────┘   │  redis_cache     │  │
        │                        │  redis:7-alpine  │  │
        │                        │  :6379 AOF vol   │  │
        │                        └──────────────────┘  │
        └──────────────────────────────────────────────┘
                               │
                     AWS S3 (product/asset storage — real S3 in prod)
```

**Services & images**

| Service | Image | Container (prod) | Port | Notes |
|---|---|---|---|---|
| API | `<dockerhub-user>/sribees-backend:latest` | `sribees_backend_prod` | 8000 | gunicorn, 4 uvicorn workers, `--timeout 120` |
| PostgreSQL | `ankane/pgvector:v0.5.1` | `sribees_postgres_prod` | 5432 | pgvector for semantic search; named volume `postgres_data` |
| Redis | `redis:7-alpine` | `sribees_redis_prod` | 6379 | AOF persistence, password-protected; volume `redis_data` |
| Object storage | **AWS S3** (managed) | — | — | MinIO is **local dev only**, disabled in prod |
| Reverse proxy | Nginx (host package, not Docker) | — | 80/443 | Certbot-managed TLS |

---

## 4. The Docker image (`Dockerfile`)

Multi-stage build with three targets:

| Stage | Base | Command | Used by |
|---|---|---|---|
| `base` | `python:3.11-slim` | installs `gcc`, `libpq-dev`, `curl` + `requirements.txt` | shared layer |
| `development` | `base` | `uvicorn app.main:app --reload` | local `docker compose` |
| `production` | `base` | `gunicorn app.main:app -w 4 -k uvicorn.workers.UvicornWorker --timeout 120` | CI/CD image |

- Runs as a **non-root `appuser`**.
- Exposes **8000**; `HEALTHCHECK` curls `/health`.
- Production stage accepts `BUILD_DATE` / `GIT_COMMIT` build-args (stamped as OCI labels).

**Build locally**
```bash
docker build --target development -t sribees-api:dev  fastapi_backend
docker build --target production  -t sribees-api:prod fastapi_backend
```

---

## 5. CI — Lint & Test  (`.github/workflows/deploy.yml`)

**Triggers:** push to `main`/`staging`, PRs to `main`. Concurrency-cancels in-progress runs per ref.

**Job `test`** (Ubuntu, Python 3.11) with **service containers**:
- `pgvector/pgvector:pg15` (Postgres, `test_db`)
- `redis:7-alpine`

Steps:
1. `pip install -r requirements.txt` (pip cache keyed on `requirements.txt`).
2. **Lint:** `ruff check app/ --output-format github` *(blocking).*
3. **Types:** `mypy app/ --ignore-missing-imports || true` *(non-blocking).*
4. **Tests:** `pytest tests/ -v --cov=app --cov-report=xml || true` *(non-blocking).*

> ⚠️ Only `ruff` can fail the build. `mypy` and `pytest` are suffixed with `|| true`,
> so type errors and failing tests **do not block** a deploy. Tighten this before
> relying on CI as a quality gate.

---

## 6. CD — Build & Deploy to EC2  (`.github/workflows/deploy-backend-ec2.yml`)

**Trigger:** push to `main`. Two jobs:

### Job 1 — `build-and-push`
1. `docker/build-push-action` builds the **`production`** target with GitHub Actions layer cache (`type=gha`).
2. Pushes two tags to the **GitHub Container Registry** (authenticated with the
   automatic `GITHUB_TOKEN`, so no registry secret is needed):
   - `ghcr.io/anjanamadushan310/new-sribees-backend:latest`
   - `ghcr.io/anjanamadushan310/new-sribees-backend:<git-sha>`  ← immutable, used for rollbacks

   The path is derived from `${{ github.repository }}`, so it follows the repo
   automatically if it is ever renamed or moved to another account.
3. Base64-encodes `docker/ec2-docker-compose.yml` and passes it to the deploy job as an output (avoids heredoc/SSH quoting issues).

### Job 2 — `deploy` (SSH via `appleboy/ssh-action`, `script_stop: true`)
Runs this idempotent bootstrap on the EC2 host:

| Step | Action |
|---|---|
| 1 | `apt-get install` core deps: `curl git nginx certbot python3-certbot-nginx` |
| 2 | Install Docker CE + `docker-compose-plugin` **if missing** (adds user to `docker` group) |
| 3 | Create deploy dir `~/sribees-backend`, chown to the SSH user |
| 4 | Write `.env` from the **`ENV_CONTENT`** secret, `chmod 600` |
| 5 | Decode the compose template, substitute `__DOCKER_IMAGE__` → `:latest`, validate with `docker compose config` |
| 6 | `docker login ghcr.io` (run-scoped `GITHUB_TOKEN`) + `docker compose pull fastapi_backend`, then `docker logout` |
| 7 | `docker compose down` → `up -d postgres_db redis_cache` → wait 10s → `up -d fastapi_backend` |
| 8 | `docker compose exec fastapi_backend alembic upgrade head` *(falls back to "skipped" — see §9)* |
| 9 | Write Nginx server block → `proxy_pass http://127.0.0.1:8000`, enable site, `nginx -t` |
| 10 | `certbot --nginx -d $DOMAIN --redirect` (non-interactive) for TLS |
| 11 | Add SSL auto-renewal cron: `0 0 * * * certbot renew --post-hook 'systemctl reload nginx'` |

**Domain/email resolution:** the script greps the `.env` for `HOST_DOMAIN` and
`SSL_EMAIL`, defaulting to `api.sribeesonline.lk` / `admin@sribeesonline.lk`.

**Required GitHub Secrets** (repo → Settings → Secrets → Actions):

| Secret | Purpose |
|---|---|
| `EC2_HOST` | EC2 public IP or DNS |
| `EC2_USERNAME` | SSH user (e.g. `ubuntu`) |
| `EC2_SSH_KEY` | SSH private key (PEM) |
| `ENV_CONTENT` | **Entire `.env` file contents** injected onto the host at deploy time |

> Registry credentials are **not** in this list. Both the push (runner) and the
> pull (EC2) authenticate to `ghcr.io` with the `GITHUB_TOKEN` that Actions mints
> per run, via the `packages: write` / `packages: read` job permissions. The
> published package is private to the repo by default; making it public would let
> the EC2 pull without logging in at all.

**`ENV_CONTENT` should include at minimum:**
```dotenv
APP_ENV=production
DEBUG=false
HOST_DOMAIN=api.sribeesonline.lk        # used for Nginx server_name + Certbot
SSL_EMAIL=admin@sribeesonline.lk        # used for Certbot registration
JWT_SECRET_KEY=<strong-random>
# AWS S3 (real), Gemini, Stripe, SMTP, FCM, Sentry, etc.
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=ap-southeast-1
S3_BUCKET_NAME=sribeesonline-assets
GEMINI_API_KEY=...
SENTRY_DSN=...
```
> `DATABASE_URL` and `REDIS_URL` do **not** need to be in `ENV_CONTENT` for the EC2
> flow — `docker/ec2-docker-compose.yml` sets them to the in-network service DSNs
> (`postgres_db` / `redis_cache`). Anything you *do* put in `.env` is still loaded
> via `env_file`.

---

## 7. Jenkins pipeline (alternative CI/CD)  (`Jenkinsfile`)

A self-contained pipeline for teams running Jenkins instead of (or alongside) GitHub Actions.

| Stage | What it does | Branch gate |
|---|---|---|
| **Build** | `docker build --target production` with `BUILD_DATE`/`GIT_COMMIT` args | all |
| **Lint & Test** | runs `ruff` + `pytest` **inside** the built image | all |
| **Health Check** | `docker compose up -d --wait`, then probes Postgres, Redis, MinIO, FastAPI; tears down after | all |
| **Push** | tag + push to `$DOCKER_REGISTRY` (default `localhost:5000`) using `DOCKER_REGISTRY_CREDENTIALS` | `main`/`staging` |
| **Deploy** | `docker compose pull` + `up -d --force-recreate --no-deps fastapi_backend`, then health probe | `main`→prod, `staging`→staging |

Options: 20-min timeout, no concurrent builds, keep last 10 builds, `docker image prune -f` always.
Requires Jenkins credential **`DOCKER_REGISTRY_CREDENTIALS`**.

---

## 8. Environments & configuration

Config is via env vars, loaded by `pydantic-settings` (`app/config/settings.py`).

| File | Environment | Highlights |
|---|---|---|
| `.env.example` | template | documents every variable; copy to `.env` |
| `.env.docker` | local Docker | service-name hosts (`postgres`, `redis`) |
| `.env.staging` | staging | `APP_ENV=staging`, `WORKERS=2`, staging DB/Redis hosts, staging CORS |
| `.env.production` | production | `DEBUG=false`, secrets via `${VAR}` placeholders, prod CORS, higher rate limits |

**Key variables**

| Group | Vars |
|---|---|
| App | `APP_ENV`, `DEBUG`, `API_VERSION`, `HOST`, `PORT`, `WORKERS` |
| DB | `DATABASE_URL` (`postgresql+asyncpg://…`), `DATABASE_POOL_SIZE`, `DATABASE_MAX_OVERFLOW` |
| Redis | `REDIS_URL` |
| Auth | `JWT_SECRET_KEY`, `JWT_ALGORITHM`, `JWT_ACCESS_TOKEN_EXPIRE_MINUTES`, `JWT_REFRESH_TOKEN_EXPIRE_DAYS` |
| Storage | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, `S3_BUCKET_NAME`, `S3_ENDPOINT_URL`* |
| AI search | `GEMINI_API_KEY`, `GEMINI_EMBEDDING_MODEL`, `GEMINI_EMBEDDING_DIMENSION`, `SEMANTIC_SEARCH_SIMILARITY_THRESHOLD` |
| Payments | `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET` |
| Email | `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_FROM` |
| Push | `FIREBASE_CREDENTIALS_PATH`, `FCM_SERVER_KEY` |
| Monitoring | `SENTRY_DSN`, `SENTRY_TRACES_SAMPLE_RATE`, `SENTRY_PROFILES_SAMPLE_RATE` |
| CORS / limits | `CORS_ORIGINS` (JSON array), `FRONTEND_URL`, `RATE_LIMIT_PER_MINUTE` |

\* `S3_ENDPOINT_URL` is set (MinIO) locally and **blank** in prod (uses real AWS).

---

## 9. Database & migrations

**PostgreSQL 15 + pgvector** (`ankane/pgvector`). There are **three** migration mechanisms in play:

1. **First-boot init (local dev)** — `docker-compose.yml` mounts into the Postgres
   container's `docker-entrypoint-initdb.d`:
   - `docker/postgres/init.sql`
   - `docker/postgres/migrations/010_enable_semantic_search.sql`
   These run **only when the data volume is empty** (first container init).

2. **Versioned SQL migrations** — `fastapi_backend/migrations/*.sql` (011–015):
   branch inventory, post-office↔branch mapping, product discount/branch fields,
   orders `branch_id`, coupons, app settings, wallet tables. Applied manually
   (e.g. `psql -f`) or via `run_migration_011.py`.

3. **Seed scripts** — `migrations/seed_branch_mappings.py`, `migrations/seed_splash_video.py`.

> ⚠️ **Alembic is a no-op today.** `requirements.txt` includes `alembic`, and the EC2
> pipeline runs `alembic upgrade head`, but there is **no `alembic.ini`/versions/**
> in the repo — so that step logs *"No alembic setup — migration skipped."* In
> production, schema is expected to come from the init SQL on first boot; **new
> SQL migrations (011+) must currently be applied by hand** on the EC2 Postgres
> container:
> ```bash
> # on the EC2 host, from ~/sribees-backend
> docker compose cp fastapi_backend:/app/migrations ./migrations   # if baked in
> docker compose exec -T postgres_db psql -U sribees_user -d sribeesonline < migrations/015_create_wallet_tables.sql
> ```
> **Recommendation:** either finish the Alembic setup or add an explicit
> "apply SQL migrations" step to the pipeline.

---

## 10. Object storage

- **Local dev:** MinIO (`minio/minio`) provides an S3-compatible endpoint at
  `:9000` (console `:9001`). `minio_init` creates the `sribees-assets` bucket with
  public-read. Emulator URL `http://10.0.2.2:9000/...` is used for the Android emulator.
- **Production:** real **AWS S3** (`docker-compose.prod.yml` blanks the MinIO
  endpoint and disables the MinIO services). Set `AWS_*` + `S3_BUCKET_NAME`
  via `ENV_CONTENT`.

---

## 11. Networking, endpoints & health

| Path | Purpose |
|---|---|
| `GET /health` | Liveness — used by Docker `HEALTHCHECK`, Nginx target, CI/CD probes |
| `GET /api` | API info + endpoint index |
| `/api/v1/...` | All feature routers (auth, products, cart, orders, payments, wallet, admin/*, …) mounted under `prefix="/api"` |
| `/docs`, `/redoc`, `/openapi.json` | **Only when `DEBUG=true`** |

> ⚠️ Because production runs `DEBUG=false`, **Swagger `/docs` is disabled in prod**
> (the deploy log's "Docs:" line will 404). Enable a docs route deliberately if you
> need it, or view docs from a staging/dev instance.

Internal ports: API `8000`, Postgres `5432`, Redis `6379`. Dev-only: MinIO `9000/9001`,
admin `3000`, pgAdmin `5050`, Redis Commander `8081`.

---

## 12. Reverse proxy & TLS

- Nginx runs as a **host systemd service** (not containerized), config at
  `/etc/nginx/sites-available/sribees`, symlinked into `sites-enabled`, default site removed.
- Proxies `/` → `http://127.0.0.1:8000` with WebSocket upgrade headers and
  `proxy_read_timeout 86400`.
- **TLS** via Certbot (`--nginx --redirect`), auto-renewed nightly by cron.
- If DNS for `HOST_DOMAIN` isn't pointed at the EC2 IP yet, Certbot is skipped
  gracefully and the API stays reachable over HTTP until DNS propagates (re-run the
  deploy, or run Certbot manually, once DNS is ready).

---

## 13. Admin dashboard deployment (`admin/`)

Separate React/Vite app with its own multi-stage Dockerfile:

- `development` → Vite dev server on `:3000`.
- `builder` → `npm run build` with build-args `VITE_API_BASE_URL`, `VITE_ENV`.
- `production` → static `dist/` served by **Nginx** (`admin/nginx.conf`) on port `80`,
  non-root user, `HEALTHCHECK` on `/`.

```bash
docker build \
  --target production \
  --build-arg VITE_API_BASE_URL=https://api.sribeesonline.lk \
  --build-arg VITE_ENV=production \
  -t sribees-admin:prod admin
```
> The admin app is **not** part of the EC2 backend pipeline. Deploy it separately
> (static host / its own container / CDN). In local `docker compose` it runs as the
> `admin` service; `docker-compose.prod.yml` disables it.

---

## 14. Observability & operations

- **Sentry** — initialized on startup when `SENTRY_DSN` is set, with FastAPI +
  SQLAlchemy + Redis integrations, `send_default_pii=False`, release
  `sribeesonline-api@1.0.0`.
- **Logs** — `loguru` to stdout (captured by `docker compose logs`) and `logs/app.log`
  (volume `backend_logs`). Gunicorn access/error logs go to stdout.
- **Healthchecks** — every service defines one; the API's is `curl /health`.

**Common commands (on the EC2 host, `~/sribees-backend`):**
```bash
docker compose ps                          # service status
docker compose logs -f fastapi_backend     # tail API logs
docker compose pull fastapi_backend        # get :latest
docker compose up -d --force-recreate --no-deps fastapi_backend   # hot-swap API
docker compose exec postgres_db psql -U sribees_user -d sribeesonline
```

**Rollback to a previous build (by immutable SHA tag):**
```bash
# pin the image to a known-good commit and recreate
docker pull <user>/sribees-backend:<git-sha>
sed -i "s|sribees-backend:latest|sribees-backend:<git-sha>|" docker-compose.yml
docker compose up -d --force-recreate --no-deps fastapi_backend
```
> Because every build is also pushed as `:<git-sha>`, you can always roll back to
> any prior commit's image without rebuilding.

---

## 15. Run it locally

```bash
# from repo root (monorepo)
docker compose up -d                 # postgres + redis + minio + api + admin
docker compose logs -f fastapi_backend
# API:    http://localhost:8000/health   /docs (debug on)
# Admin:  http://localhost:3000
# MinIO:  http://localhost:9001  (admin / password123)

docker compose --profile tools up -d # + pgAdmin (:5050) + Redis Commander (:8081)

# production-style run (gunicorn, no MinIO/admin/hot-reload)
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Native (no Docker):
```bash
cd fastapi_backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                 # then edit
uvicorn app.main:app --reload
```

---

## 16. Known gaps & recommendations

| # | Finding | Impact | Suggested fix |
|---|---|---|---|
| 1 | **`mypy`/`pytest` gated with `\|\| true`** in CI | Failing tests/types don't block deploy | Remove `\|\| true` once the suite is green; make CI a required check |
| 2 | **Alembic step is a no-op** (no `alembic.ini`) | Schema changes 011+ aren't auto-applied in prod | Finish Alembic **or** add an explicit SQL-migration step to the pipeline |
| 3 | **Hardcoded DB/Redis passwords** in `docker/ec2-docker-compose.yml` (`sribees_password_123`, `sribees_redis_password`) | Weak, committed secrets on the prod host | Parameterize via `.env`/secrets; rotate credentials |
| 4 | **`.env` / `.env.production` committed** to the repo | Risk of leaking real secrets | Ensure only placeholders are committed; keep real values in `ENV_CONTENT`/secrets manager; add to `.gitignore` |
| 5 | **Swagger `/docs` disabled in prod** (`DEBUG=false`) | No live API docs in prod | Intentional; expose docs behind auth or use staging if needed |
| 6 | **Single EC2 host, no orchestration** | No HA; downtime on deploy/host failure | Consider ECS/Fargate or a managed DB (RDS) + ElastiCache for scale/HA |
| 7 | **Workflow lives in monorepo but deploys from a separate repo** | Push to monorepo won't deploy | Document/automate the sync, or point Actions at the monorepo path |
| 8 | **Postgres & Redis are containers with local volumes** on the app host | Data tied to the instance | Move to **RDS + ElastiCache** for backups, failover, and safer deploys |

---

*Sources: `.github/workflows/deploy-backend-ec2.yml`, `.github/workflows/deploy.yml`,
`Jenkinsfile`, `Dockerfile`, `docker-compose.yml`, `docker-compose.prod.yml`,
`docker/ec2-docker-compose.yml`, `requirements.txt`, `app/main.py`, and the
`.env.*` files.*
