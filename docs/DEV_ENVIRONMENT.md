# SRIBEESonline — development environment (Contabo)

Written for: whoever sets this up on the Contabo box and whoever debugs it at
2am afterwards.

Production is untouched by everything in this document. It stays on EC2, on
`main`, behind `deploy-prod.yml`, at `api.sribeesonline.com`. Development is a
different branch, a different host, a different database and a different
hostname, and nothing in either pipeline can reach the other.

---

## What this is

| | Production | Development |
|---|---|---|
| Branch | `main` | `development` |
| Workflow | `deploy-prod.yml` | `deploy-dev.yml` |
| Host | AWS EC2 | Contabo VPS (shared with `sribees-express-dev`) |
| Path | `~/sribees-backend` | `/opt/sribees-online-dev` |
| Hostname | `api.sribees.com` | `dev-api.sribees.com` |
| Docs (`/docs`) | closed | open |
| Image | `:latest` tag | pinned by digest |

## The one thing that is shared: the edge

`sribees-express-dev` already runs a Caddy container that binds host ports 80
and 443. **There can only be one.** A second ACME client cannot share those
ports, and two edges on one box means two places to look when TLS breaks.

So this stack runs no proxy of its own. It publishes a single port on
**loopback only** — `127.0.0.1:8100` — and the Express Caddy proxies the dev
hostname to it.

The `127.0.0.1:` prefix is not decoration. Without it Docker writes its own
iptables rules that bypass `ufw` completely, and this API — `DEBUG` on, `/docs`
open — would be reachable from the internet on a high port, unencrypted,
whatever the firewall claims.

### The site block to add

In **the SribeesExpress repo**, in `backend-fastapi/docker/Caddyfile.deploy`,
alongside the existing `{$DOMAIN}` block:

```caddyfile
dev-api.sribees.com {
	encode zstd gzip

	header {
		X-Content-Type-Options "nosniff"
		X-Frame-Options "DENY"
		Referrer-Policy "strict-origin-when-cross-origin"
		# No Strict-Transport-Security on purpose. HSTS set on a *.sribees.com
		# development host is cached by every browser that touches it and then
		# applied to the whole domain family — including hosts that are not
		# ours to pin. Production sets it; development does not.
		-Server
		-X-Powered-By
	}

	# host.docker.internal resolves to the host gateway; the extra_hosts entry
	# below is what makes it exist on Linux.
	reverse_proxy host.docker.internal:8100 {
		health_uri /health
		health_interval 10s
		health_timeout 5s
		# The single replica restarts in place on deploy; give the proxy a
		# moment to find it again rather than serving a 502 to whoever is
		# testing at that second.
		lb_try_duration 30s
		lb_try_interval 1s
	}

	log {
		output stdout
		format json
	}
}
```

and on the Express `caddy` service in `docker-compose.deploy.yml`:

```yaml
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

It has to be committed **there**, not applied by hand: the Express deploy
copies its own `Caddyfile.deploy` onto the box on every run, so a hand-edit is
erased by the next Express release — at a moment nobody will connect to this.

Until that block exists the dev deploy's smoke test fails on `/health`. That is
the intended way to find out, rather than a green deploy pointing at nothing.

---

## Secrets

Repository → Settings → Secrets and variables → Actions.

| Secret | What | Notes |
|---|---|---|
| `SSH_HOST` | Contabo IP or hostname | same box as Express |
| `SSH_USER` | deploy account | must be in the `docker` group; no sudo needed |
| `SSH_PORT` | optional | defaults to `22` |
| `SSH_PRIVATE_KEY` | deploy key, PEM | its own key, not a personal one |
| `SSH_KNOWN_HOSTS` | `ssh-keyscan -p PORT HOST` output | pinned on purpose — never `StrictHostKeyChecking=no` |
| `DEV_ENV_FILE` | the whole `.env` below | piped over SSH, written 0600, never echoed |
| `DEV_DOMAIN` | `dev-api.sribees.com` | used by the smoke test |
| `DEV_DEPLOY_PATH` | optional | defaults to `/opt/sribees-online-dev` |

Getting `SSH_KNOWN_HOSTS`:

```bash
ssh-keyscan -p 22 <contabo-host>
```

## `DEV_ENV_FILE`

Generate real secrets — do not reuse production's, and do not reuse Express's:

```bash
openssl rand -base64 36   # once per secret below
```

```dotenv
# --- the stack itself -------------------------------------------------------
DOMAIN=dev-api.sribees.com
APP_BIND_PORT=8100

POSTGRES_USER=sribees_dev
POSTGRES_PASSWORD=<generated>
POSTGRES_DB=sribeesonline_dev
REDIS_PASSWORD=<generated>

# --- what the app reads -----------------------------------------------------
# Service names, not localhost: these resolve on the compose network.
DATABASE_URL=postgresql+asyncpg://sribees_dev:<same password>@postgres:5432/sribeesonline_dev
REDIS_URL=redis://:<same password>@redis:6379/0

JWT_SECRET_KEY=<generated, DIFFERENT from production>
APP_ENV=development
ENABLE_DOCS=true
STORAGE_BACKEND=local

# --- third parties ----------------------------------------------------------
# Sandbox credentials only. A development box with /docs open must never hold
# a key that can move real money or send real messages.
MARX_IPG_BASE_URL=https://payment.api.dev.marxpos.com
MARX_IPG_API_KEY=<sandbox key>
```

`JWT_SECRET_KEY` differing from production is what stops a token minted on the
dev box from being accepted by the production API.

## DNS

An `A` record for `dev-api.sribees.com` pointing at the Contabo IP. Caddy
provisions the certificate over ACME on first request, so the record has to
exist and have propagated *before* the first deploy — otherwise the challenge
fails and the smoke test times out.

---

## Day to day

```bash
# deploy: just push
git push origin development

# on the box
cd /opt/sribees-online-dev
./deploy-dev.sh status
./deploy-dev.sh logs app 200
./deploy-dev.sh rollback          # back to the previously healthy image

# psql — no port is published, so go through the container
docker compose -f docker-compose.dev-contabo.yml --env-file .env \
  exec postgres psql -U sribees_dev -d sribeesonline_dev
```

Redeploying an image that already exists, without rebuilding: Actions → Deploy
Development (Contabo) → Run workflow → paste the `ghcr.io/...@sha256:...`
reference.

## What the rollout does, in order

1. Pull the image by digest.
2. Start Postgres and Redis, wait for both to report healthy.
3. **Dump the database** to `backups/pre-deploy-<stamp>.sql.gz`, 7-day
   retention. A failed dump aborts before anything is migrated.
4. Extract `/app/migrations` **from the image** and apply the `.sql` files in
   `sort -V` order with `ON_ERROR_STOP=1`.
   The migrations come from the image and never from a running container: a
   release that adds a column starts an app whose ORM selects that column
   against a database that has not got it yet, so the app may legitimately be
   dead at exactly the moment its migrations are needed. Production learned
   that on 2026-09-12 by going down.
5. Recreate the app, wait for healthy.
6. Record the digest in `.image` — only now, so `.image` always names a release
   that actually served traffic and `rollback` has somewhere real to go.
7. Smoke-test `https://dev-api.sribees.com/health` through the public edge.
8. On any failure: roll back to `.image.previous` automatically.

Rollback restores the **image**, not the schema. These migrations are additive
(`ADD COLUMN IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`), so the older image
runs against the newer schema without complaint, while restoring the dump would
throw away everything written since it was taken. If a migration was genuinely
destructive, the dump in `backups/` is the deliberate, manual way back.

## One replica, deliberately

The reminder worker runs in-process — an `asyncio` task started in
`app/main.py`. Two replicas would fire every reminder twice. Development needs
correct deploys, not zero-downtime ones.

## Disk

Both stacks share the box's disk. Watch it:

```bash
df -h /
docker system df
docker image prune -af --filter "until=168h"   # frees old digests
```

Every container here caps its own logs at 10MB × 3-5 files, so logs cannot be
what fills the disk. Old images can.
