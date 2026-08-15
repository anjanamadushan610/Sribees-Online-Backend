# 🛒 SRIBEESonline — Multi-Branch Grocery Delivery Platform

> A multi-branch grocery/daily-essentials delivery platform for Sri Lanka, with a
> global-to-local product catalog, a customer mobile app, an admin dashboard, and a
> separate portal for a professional referral (partner) team.

---

## 📖 Overview

SRIBEESonline is a **multi-branch** platform: a global product catalog that each of
five branches (Colombo, Kandy, Galle, Negombo, Kurunegala) can stock locally with its
own price, discount, cashback and stock-on-hand overrides. Everything downstream —
inventory, orders, analytics, marketing — is branch-aware.

### Key features (what's actually built, not what's planned)

- 🔐 **JWT auth** for both customers (email/password, Google/Facebook OAuth, phone OTP)
  and a separate admin realm (5 RBAC roles — see below)
- 🌍 **Global-to-local catalog** — one product record, per-branch price/discount/
  cashback/stock overrides (`branch_inventory`)
- 🔍 **AI-powered multilingual search** — Gemini embeddings + pgvector semantic search,
  with fuzzy/typo-tolerant fallback across English, Sinhala, Tamil, Singlish and
  Tamilish, plus an AI keyword-suggestion tool
- 💬 **AI shopping assistant** — a Gemini-backed chat that answers over the real
  catalog and can add matched products straight to the cart
- 🛍️ **Cart, wishlist, coupons, Quick Sale** (branch-scoped flash-discount campaigns),
  home banners (network-wide or per-branch)
- 📦 **Orders** with a full lifecycle (pending → confirmed → processing → shipped →
  out for delivery → delivered), returns/refunds, PDF invoices
- 🔄 **Branch-to-branch stock transfers** — request → approve → ship → complete, with
  real stock movement between branches
- 💳 **Payments** — Marx IPG gateway integration, wallet balance + cashback, COD
- 🚚 **Courier integration** — real courier API + a local mock courier service for dev,
  delivery-verification codes
- 🤝 **Partner / referral program** — a separate portal (`partner-portal/`) for a
  professional referral team: multi-level commission tracking, self-registration,
  sub-partner recruiting, payouts
- ⭐ Reviews & ratings, 🔔 push notifications, 📊 an admin dashboard with role-specific
  views and analytics per branch

### What's *not* built (despite appearing in older planning docs)

No Next.js web storefront, no Kubernetes/Terraform, no Elasticsearch, no Alembic-managed
migrations. See [Tech Stack](#-tech-stack) and [Known documentation debt](#-known-documentation-debt)
below.

---

## 📁 Repository structure — 4 independent git repos, not a monorepo

Despite the shared working directory (and this doc's filename), **there is no single
monorepo remote.** Each of the four project folders below is its own git repository
with its own `origin` — pushing from inside `admin/` publishes only the admin
dashboard, pushing from `fastapi_backend/` publishes only the backend, and so on. A
top-level `.git` also exists in the workspace root for local history, but it has no
remote configured; it's not what gets deployed.

```
SRIBEESonline/                     ← workspace root (local-only git history, no remote)
│
├── fastapi_backend/                ← own git repo — github.com/.../new-sribees-backend
│   ├── app/
│   │   ├── api/v1/                 # route modules — see app/api/v1/router.py for the full map
│   │   ├── config/                 # settings, database, redis
│   │   ├── core/                   # security, RBAC dependencies, exceptions, rate limiting
│   │   ├── models/                 # SQLAlchemy 2.0 models (async)
│   │   ├── schemas/                # Pydantic v2 request/response schemas
│   │   ├── services/                # business logic
│   │   └── main.py                 # FastAPI app + lifespan (DB/Redis/FCM init)
│   ├── migrations/                 # numbered raw .sql files, NOT Alembic — see below
│   ├── docker/postgres/            # init.sql (extensions only) + ec2-docker-compose.yml
│   ├── docker-compose.yml          # local dev stack (Postgres+pgvector, Redis, MinIO, backend)
│   ├── docker-compose.prod.yml
│   ├── mock_courier/               # standalone mock courier API for local dev
│   ├── scripts/                    # setup.sh, smoke_test.py, seed helpers
│   ├── tests/
│   ├── .github/workflows/          # deploy.yml (lint/test), deploy-backend-ec2.yml (build+deploy)
│   ├── docs/                       # you are here
│   └── requirements.txt
│
├── admin/                          ← own git repo — github.com/.../new-sribees-frontend
│   ├── src/
│   │   ├── api/                    # one file per backend resource, typed request/response shapes
│   │   ├── pages/                  # Dashboard, Products, Orders, Inventory, Marketing, Settings, ...
│   │   ├── components/             # shared UI (guards, layout, analytics widgets)
│   │   ├── store/                  # Zustand (auth)
│   │   ├── hooks/                  # usePermissions (RBAC)
│   │   └── types/                  # AdminRole, ROLE_PERMISSIONS, NAVIGATION_CONFIG
│   ├── e2e/                        # Playwright end-to-end suite, run against a real seeded backend
│   ├── .github/workflows/e2e.yml
│   ├── ADMIN_CREDENTIALS.md        # every seeded demo admin/customer/partner login
│   └── vercel.json                 # deployed via Vercel's native GitHub integration
│
├── mobile/                         ← own git repo — github.com/.../new-sribees-mobile
│   ├── lib/
│   │   ├── core/                   # api client, providers (Riverpod), design system, services
│   │   └── features/               # auth, home, products, cart, checkout, orders, chat, ...
│   ├── android/ ios/
│   └── pubspec.yaml
│
└── partner-portal/                 ← own git repo — github.com/.../new-sribees-frontend-referral
    ├── src/
    │   ├── api/
    │   ├── pages/                  # partner dashboard, recruiting, payouts
    │   └── router.tsx
    └── package.json
```

---

## 🏗️ Architecture (brief — see `ARCHITECTURE.md` for the full picture)

- **Backend**: Python FastAPI (async), one process serving the customer API and the
  admin API (`/api/v1/admin/*`) side by side — not separate services.
- **Database**: PostgreSQL 15+ with the `pgvector` extension (semantic search
  embeddings), accessed via SQLAlchemy 2.0's async ORM + `asyncpg`. Schema lives in the
  default `public` schema — **not** a custom `sribees` schema (an early prototype
  briefly did this via `docker/postgres/init.sql`; that file corrupted a fresh local
  database and was trimmed down to just extension setup).
- **Cache/sessions**: Redis 7+.
- **Object storage**: AWS S3 in production, MinIO locally (S3-compatible, same code
  path).
- **Admin dashboard**: React 19 + Vite + Ant Design + TanStack Query, deployed to
  Vercel from its own repo.
- **Mobile app**: Flutter, Riverpod for state, Dio for HTTP, Hive/SharedPreferences for
  local storage.
- **Partner portal**: a second, separate React + Vite + Ant Design app (same stack as
  admin, different audience — the referral/partner team, not store staff).
- **Deployment**:
  - Backend: GitHub Actions builds a Docker image, pushes to GHCR, SSHes into a single
    AWS EC2 host running Docker Compose (Postgres, Redis, the API) behind Nginx +
    Let's Encrypt. See `deploy-backend-ec2.yml`.
  - Admin & partner portal: Vercel's native GitHub integration — pushing to `main`
    deploys directly, no GitHub Actions step involved for the deploy itself (a separate
    `e2e.yml` workflow runs the Playwright suite against a real seeded backend as a CI
    gate, but does not deploy anything).
  - Mobile: built and distributed manually (Flutter APK/IPA), not part of the CI/CD
    pipeline above.

---

## 🚀 Getting started

Full, current instructions live in [`LOCAL_DEV_SETUP.md`](LOCAL_DEV_SETUP.md) — this
is just the short version.

```powershell
# Backend — from fastapi_backend/, NOT the workspace root
cd fastapi_backend
copy .env.example .env       # then fill in secrets as needed — most have safe local defaults
bash scripts/setup.sh        # starts Postgres/Redis/MinIO/backend, runs migrations, seeds demo data

# Admin dashboard — Vite dev server, proxies /api to :8000, no CORS setup needed
cd ../admin
npm install
npm run dev                  # http://localhost:5173 (or next free port)

# Mobile — from mobile/
cd ../mobile
flutter pub get
flutter run                  # point it at 10.0.2.2:8000 for the Android emulator
```

Demo logins for every seeded role (admin, branch managers, marketing/inventory/support,
partners, customers) are in [`admin/ADMIN_CREDENTIALS.md`](../../admin/ADMIN_CREDENTIALS.md).

---

## 🛠️ Tech Stack

### Backend
- **Runtime**: Python 3.11+, FastAPI 0.109, Uvicorn (dev) / Gunicorn+UvicornWorker (prod)
- **ORM**: SQLAlchemy 2.0 (async) + asyncpg — **schema is created by
  `Base.metadata.create_all()` on boot, then numbered raw-SQL files in `migrations/`
  layer on additive changes (new columns/tables, seed data). Alembic is listed in
  `requirements.txt` but not actually wired up — there's no `alembic/versions/`
  directory, no revision history.**
- **Validation**: Pydantic v2
- **Auth**: python-jose (JWT) + passlib[argon2]; separate token realms for customers
  (`users`) and admins (`admins`)
- **AI**: `google-genai` (Gemini) — embeddings for semantic search, generation for the
  shopping chat assistant

### Admin dashboard & partner portal (same stack)
- React 19, Vite 7, Ant Design 6, TanStack Query 5, Zustand, Recharts
- Playwright for E2E (admin only, so far)

### Mobile
- Flutter 3.x / Dart, Riverpod for state, Dio for HTTP, Hive + SharedPreferences for
  local storage, Firebase Cloud Messaging for push (via `flutter_local_notifications` +
  a `FirebaseService` wrapper)

### Data
- PostgreSQL 15+ with `pgvector` (primary datastore, single schema)
- Redis 7+ (cache, rate limiting, sessions)
- AWS S3 (prod) / MinIO (local dev) for product images, review photos, banners, splash
  video

### DevOps
- Docker + Docker Compose (local dev and the EC2 production host — **no Kubernetes**)
- GitHub Actions (backend CI/CD; admin/partner-portal CI only, deploy is Vercel)
- AWS EC2 (backend host) + S3; Vercel (admin, partner-portal)

---

## 📚 Where to look for what

| Question | Look here |
|---|---|
| What API routes exist and who can call them | `fastapi_backend/app/api/v1/router.py`, then the individual route module |
| What does the database actually look like | `fastapi_backend/docs/DATABASE_SPECIFICATION.md` |
| How does deployment actually work | `fastapi_backend/docs/ARCHITECTURE.md`, `fastapi_backend/DEPLOYMENT.md` |
| How do I run this locally | `fastapi_backend/docs/LOCAL_DEV_SETUP.md` |
| What are the demo logins | `admin/ADMIN_CREDENTIALS.md` |
| Admin RBAC — which role can do what | `admin/src/types/admin.types.ts` (`ROLE_PERMISSIONS`), `admin/ADMIN_CREDENTIALS.md` |
| Auto-generated API reference | `http://localhost:8000/docs` (Swagger) or `/redoc` when the backend is running |

## ⚠️ Known documentation debt

`EPICS.md`, `FEATURES.md`, `USER_STORIES.md`, `USER_STORIES_BREAKDOWN.md`,
`ARCHITECTURE_DIAGRAMS.md`, `IMPLEMENTATION_ROADMAP.md` and
`FINAL_IMPLEMENTATION_PLAN.md` in this `docs/` folder — and their near-duplicates at
the workspace root — predate a large amount of what's now actually built (payments,
courier, the AI chat assistant, the partner/referral portal, branch-to-branch stock
transfers, most of the admin dashboard) and have not been kept in sync. Treat them as
historical planning artifacts, not a current feature list, until someone does a full
pass reconciling them against the real codebase. This file, `ARCHITECTURE.md`,
`LOCAL_DEV_SETUP.md` and `DATABASE_SPECIFICATION.md` were brought back in sync with
reality on 2026-08-10 — the rest were not, for lack of time in one sitting.

---

**Last updated**: 2026-08-10 (rewritten to match the actual codebase — see "Known
documentation debt" above for what's still stale)
