# 🚀 SRIBEESonline — Local Development Setup Guide

> Rewritten 2026-08-10 against a real from-scratch run on this machine. The previous
> version of this doc described a `docker-compose.yml` at the workspace root and a
> `sribees` custom-schema database init that no longer match reality — see
> [What changed](#-what-changed-from-the-old-version-of-this-doc) at the bottom if
> you're comparing against an old copy.

## Prerequisites

1. **Docker Desktop** — [Download](https://www.docker.com/products/docker-desktop/)
2. **Python 3.11+** (only needed if you want to run the backend outside Docker, or run
   `scripts/smoke_test.py`)
3. **Node.js 20+** (for the admin dashboard / partner portal)
4. **Flutter SDK** + **Android Studio** — only if you're working on the mobile app
5. **Git**

```powershell
docker --version
docker compose version
node --version
python --version
```

---

## 🐳 Part 1: Backend stack (Postgres, Redis, MinIO, FastAPI)

Everything for this lives inside **`fastapi_backend/`** — not the workspace root. The
compose file, the `.env`, and the setup script are all there.

### Step 1 — Environment file

```powershell
cd fastapi_backend
copy .env.example .env
```

The example file has working local defaults for everything except third-party keys
(Gemini, Stripe/Marx IPG, OAuth). The app runs fine with those left blank — semantic
search falls back to keyword search, payments/OAuth routes simply aren't exercised.
Add a `GEMINI_API_KEY` (from [Google AI Studio](https://aistudio.google.com/apikey)) if
you want real semantic search / the AI chat assistant locally.

### Step 2 — Start Docker Desktop, then run the setup script

```powershell
docker compose up -d          # or just run scripts/setup.sh, which does this for you
```

The easiest path is the setup script — it starts every service, waits for health
checks, creates the MinIO bucket, applies every migration in `migrations/` in order,
and prints out service URLs:

```bash
# Git Bash / WSL
bash scripts/setup.sh
```

That script is idempotent — re-run it any time; it only tops up what's missing.

### Step 3 — Verify

```powershell
docker compose ps
```

Expect five **healthy** containers: `sribees_postgres`, `sribees_redis`,
`sribees_minio`, `sribees_backend`, plus `sribees_minio_init` (exits after it creates
the bucket — that's expected, not a failure). A sixth, `sribees_admin`, also exists in
`docker-compose.yml` for a fully-containerized admin build, but the documented
workflow (see Part 2) runs the admin dashboard natively via `npm run dev` instead —
don't be alarmed if the `admin` container shows unhealthy; it isn't what anything here
actually uses.

### Service URLs

| Service | URL | Notes |
|---|---|---|
| FastAPI backend | http://localhost:8000 | |
| Swagger UI | http://localhost:8000/docs | |
| ReDoc | http://localhost:8000/redoc | |
| Health check | http://localhost:8000/health | |
| MinIO Console | http://localhost:9001 | `admin` / `password123` |
| MinIO S3 API | http://localhost:9000 | |
| Postgres | `localhost:5432` | `sribees_user` / `sribees_password_123`, db `sribeesonline` |
| Redis | `localhost:6379` | password `sribees_redis_password` |
| pgAdmin (optional) | http://localhost:5050 | `docker compose --profile tools up -d` first |
| Redis Commander (optional) | http://localhost:8081 | same `--profile tools` |

### Demo data & logins

`bash scripts/setup.sh` (or a manual migration run) seeds a full demo dataset:
5 branches, ~40 demo customers, ~120 days of realistic order history across all five
branches, one admin account per role plus a manager per branch, and a small partner
(referral) tree. **Every seeded login is documented in
[`admin/ADMIN_CREDENTIALS.md`](../../admin/ADMIN_CREDENTIALS.md)** — don't guess
credentials, they're all written down there with which branch/scope each one has.

### Common commands

```powershell
docker compose logs -f fastapi_backend      # follow backend logs
docker compose restart fastapi_backend      # after changing requirements.txt, say
docker compose down                          # stop everything, keep data
docker compose down -v                       # stop and wipe volumes — genuinely fresh start
docker exec -it sribees_backend bash
docker exec -it sribees_postgres psql -U sribees_user -d sribeesonline
```

Backend code is bind-mounted into the container and uvicorn runs with `--reload`, so
Python changes take effect without a rebuild. Changes to `requirements.txt` do need
`docker compose up -d --build fastapi_backend`.

### Applying a single new migration by hand

`scripts/setup.sh` re-applies everything in `migrations/` on every run, so this is
rarely needed, but if you've just added one file and don't want to re-run the whole
script:

```bash
docker exec -i sribees_postgres psql -U sribees_user -d sribeesonline --quiet < migrations/0NN_your_file.sql
```

---

## 🖥️ Part 2: Admin dashboard

```powershell
cd admin
npm install
npm run dev
```

Vite starts on `http://localhost:5173` (or the next free port if that's taken) and
proxies `/api/*` to `http://localhost:8000` — no CORS configuration needed, and no
`VITE_API_URL` env var required for local dev. Sign in with any account from
`ADMIN_CREDENTIALS.md`.

Running the E2E suite (`npm run test:e2e`) spins up its own throwaway Postgres and a
second backend instance against a fresh seed — see `admin/.github/workflows/e2e.yml`
for exactly how CI does it; the same steps work locally if you want to run it outside
Docker.

---

## 🤝 Part 3: Partner portal

Same idea, separate app:

```powershell
cd partner-portal
npm install
npm run dev
```

Log in with a `partner.*@sribeesonline.lk` account — see `ADMIN_CREDENTIALS.md`.

---

## 📱 Part 4: Mobile app

### First-time setup

```powershell
cd mobile
flutter pub get
```

### Point it at your local backend

The app auto-detects the Android emulator and rewrites `localhost` to `10.0.2.2`
(the emulator's alias for the host machine) — see `lib/core/api/api_client.dart` /
`lib/core/platform_defaults.dart`. For a **physical device** on the same network,
you'll need the machine's real LAN IP instead; the app has a dynamic API URL
configuration for this (see `feat: add dynamic API URL configuration for physical
device support` in git history) — check `lib/core/platform_defaults.dart` for the
current mechanism rather than assuming a fixed IP.

### Run

```powershell
flutter run                     # picks a connected device/emulator
flutter devices                 # list what's available
flutter run -d <device_id>
```

### ⚠️ Windows-specific gotcha: stale builds

On this machine, `flutter build apk --debug` has been observed to **silently reuse a
stale compiled Dart kernel** and not pick up source changes, even when file timestamps
look correct — confirmed by grepping a "fresh" build's APK for a string that had just
been added and finding it missing, twice in a row. Root cause looks related to a
Kotlin incremental-compiler warning about the project and pub cache living on
different drive roots. **Always `flutter clean` before a debug build you're about to
actually test** — don't trust a plain incremental build here.

```powershell
flutter clean
flutter pub get
flutter build apk --debug
```

---

## 🔧 Troubleshooting

**Docker containers won't start / port conflicts**
```powershell
netstat -ano | findstr :8000
netstat -ano | findstr :5432
docker compose down -v
docker compose up -d --build
```

**Backend crashes on startup on a genuinely fresh Postgres volume, FK error creating
some table (e.g. `banners`)** — this used to happen because `docker/postgres/init.sql`
was a leftover full prototype schema under a separate `sribees.*` namespace that also
repointed the database's default `search_path` at it, racing the real
`create_all()`-managed `public` schema tables. Fixed 2026-08-10; if you somehow still
hit this, check `docker/postgres/init.sql` only contains `CREATE EXTENSION` statements
and nothing else, then `docker compose down -v && docker compose up -d` for a clean
volume.

**Gemini quota errors during testing** (`assistant unavailable`, fast failures not
timeouts) — the free tier caps `gemini-flash-latest` at ~20 requests/day. Resets
roughly daily; not a bug. `gemini-flash-lite-latest` is a same-day workaround if you
need to keep testing (set `GEMINI_GENERATION_MODEL`).

**Flutter: can't connect to API from emulator**
```powershell
adb devices
curl http://localhost:8000/health          # from the host — confirms the backend itself is up
adb shell curl http://10.0.2.2:8000/health  # from inside the emulator
```

---

## 📝 What changed from the old version of this doc

The previous version of this file (and `MONOREPO_README.md`) described a setup that no
longer matches the repository:

- `docker-compose.yml` used to live (or was assumed to live) at the workspace root,
  with a `.env.docker` template there too. **It's inside `fastapi_backend/`.** The
  compose file itself had stale `context: ./fastapi_backend` / `context: ./admin`
  paths left over from when it *was* at the root — those are fixed now, but if you're
  reading an old cached copy of `docker-compose.yml`, that's why `docker compose up`
  wouldn't build.
- Old paths referenced `c:\Users\Lenovo\Desktop\SRIBEESonline` — the project has since
  moved; don't hardcode a path, `cd` relative to wherever you cloned it.
- The database used to be described as a hand-written `sribees` schema with "16
  tables". The real schema is SQLAlchemy-managed (`Base.metadata.create_all()` on
  boot, `public` schema, currently ~35+ tables — see `DATABASE_SPECIFICATION.md`), and
  the `sribees`-schema `init.sql` was actively harmful on a fresh volume (see
  Troubleshooting above).
- Admin dashboard credentials/URL — the old doc said port 3000 and
  `admin@sribees.lk` / `Admin@123456`. The actual documented workflow runs the admin
  dashboard on Vite's dev server (port 5173) with credentials from
  `ADMIN_CREDENTIALS.md`.
