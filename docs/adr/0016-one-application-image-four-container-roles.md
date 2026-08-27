# 0016. One Application Image, Four Container Roles

## Status

Accepted

## Context

The application had reached the point where every remaining pre-deployment task was
about *where it runs* rather than what it does. A pre-Docker audit of the repository
found eight blockers, all fixed before this ADR: no production WSGI server, no
PostgreSQL driver, no `.dockerignore`, `.flaskenv` enabling the Werkzeug debugger even
under `FLASK_ENV=production`, silent fallbacks to local SQLite and `redis://localhost`,
unpinned ML dependencies, and unbounded LLM strings written into `String(n)` columns.

What remained was the container architecture itself. The application already runs as
four distinct processes: the Flask app, an ML Celery worker, an email/default Celery
worker, and Celery Beat (ADR-0008, ADR-0010). They share one codebase and one
`create_app()` factory, differing only in environment. The question was how to package
that.

Two shapes were available:

- **One image per role.** A web image could omit scikit-learn and SciPy entirely. But
  `app/__init__.py` imports `ml_service` unconditionally, so `numpy` and `joblib` stay
  regardless; the saving is real but partial. The cost is four Dockerfiles, four builds,
  four things to patch, and four opportunities for the roles to drift apart.
- **One image, four roles.** A single build. Docker layer sharing means a host running
  all three roles pulls the ~193 MB ML layer once, not three times. Every role provably
  runs the same code.

Memory was the other open question, and estimates were not good enough: the reason the
`LOAD_MODELS` gate exists (ADR-0008) turned out to be roughly 81x larger than the model
files it was designed around. Measured on this codebase, `create_app()` uses **174.5 MB**
with `LOAD_MODELS=0` and **288.0 MB** with `LOAD_MODELS=1` — a 113 MB difference for
three pickle files totalling 1.4 MB. The gate is not skipping the weights; it is
skipping the `scikit-learn` -> `scipy` -> `pandas` import chain that unpickling triggers.

## Decision

Build one application image from `python:3.14.3-slim-trixie` and reuse it for four
roles, each differing only by its `command` and its `LOAD_MODELS` value.

### The base image is Debian, not Alpine, and the Debian release is pinned

Alpine was rejected on evidence, not preference: there is no `musllinux` scikit-learn
wheel for CPython 3.14. `numpy`, `scipy` and `pandas` all publish one; scikit-learn does
not, so pip would fall back to compiling it from source and the image would need a full
C/C++ toolchain, Cython and an OpenMP runtime.

On Debian the opposite is true — every runtime wheel is prebuilt for `manylinux_2_28`
and vendors its own native libraries (`libgfortran`, OpenBLAS, `libgomp`), so the
Dockerfile installs **no** system packages and needs no compiler.

The `-trixie` suffix is deliberate. The bare `3.14.3-slim` tag will silently follow a
future Debian release; pinning the suffix means a rebuild six months from now produces
the same base.

### Roles differ by command and LOAD_MODELS, nothing else

This extends the pattern ADR-0008 established and ADR-0010 completed, rather than
inventing a container-specific one:

| Role           | `LOAD_MODELS` | Queue    |
|----------------|---------------|----------|
| `migrate`      | `0`           | —        |
| `web`          | `0`           | —        |
| `worker-ml`    | `1`           | `ml`     |
| `worker-email` | `0`           | `celery` |

`web` runs with `LOAD_MODELS=0`, which is a change from ADR-0008's assumption that the
web app needs the models. Since ADR-0010 moved analysis onto the `ml` queue, no route
reaches ML or LLM inference — `ml_service.predict()` is reachable only through
`workflow_service.run_message_analysis()`, which only `analyze_message` calls. Verified
by serving the app with `LOAD_MODELS=0`: homepage, login, register and static all
return 200. This saves 113 MB per web worker.

The Gunicorn target is `app:create_app()`, not `app:app`. The latter fails — the `app/`
package shadows `app.py`, and the package exports `create_app`, not an app instance.

### Migrations run as a one-shot service, not in an entrypoint

Alembic takes no lock. If `web` and both workers each ran `flask db upgrade` on boot,
three containers would race on the same schema. A dedicated `migrate` service that runs
to completion, with the other services gated behind
`depends_on: condition: service_completed_successfully`, makes the ordering explicit and
the failure visible.

`migrate` runs with `LOAD_MODELS=0` because `flask db upgrade` imports `app.py`, and
therefore `create_app()` — without the flag it would load 113 MB of models to run DDL.

No entrypoint script is used at all. Compose health checks and `depends_on` already
express readiness, and a dispatch wrapper would obscure which role failed and why.

### Gunicorn runs without --preload

`--preload` would fork workers from an already-imported application. Two reasons not to:

The memory argument for it does not hold here. Its benefit is copy-on-write sharing, and
copy-on-write was measured collapsing under real work — a Celery prefork child on the ML
worker grows from ~5 MB to ~271 MB once it processes messages, because CPython's
reference counting writes to every object header it touches.

The correctness argument is about the future. `create_app()` builds a SQLAlchemy
`Engine` and a `QueuePool` but opens **zero** connections — pools are lazy, so nothing is
shared across the fork today. The hazard is that this holds only as long as nobody adds a
startup warm-up query or a DB-backed readiness probe. The first such line would silently
give every worker a shared socket, which is the classic post-fork corruption case.
Skipping `--preload` costs nothing measurable and removes that trap.

### The web container has a health check; the workers deliberately do not

The web check calls `/healthz`, a route added for this purpose that returns
`{"status": "ok"}` without touching the database. That is intentional: it answers "is
this process serving HTTP", not "is every dependency healthy". A probe that failed on a
transient database blip would restart a web container that is fine, turning one slow
query into a restart loop.

The probe uses Python's `urllib` rather than `curl` or `wget`, because `python:slim`
ships neither. Measured cost: 0.05 s and 23.9 MB.

`/healthz` is `@limiter.exempt`. `RATELIMIT_DEFAULT` is `300 per hour` and applies to
any route without its own limit (ADR-0012); a 30-second probe spends 120/hour of that
budget and would eventually receive a 429 — which the container runtime reads as
unhealthy, restarting a working process.

The workers have no health check because the only real option is expensive:
`celery inspect ping` measured **11.16 s** and spawns a full application import
(~297 MB with `LOAD_MODELS=1`). Running that every 30 seconds inside a 640 MB limit is an
OOM risk that would cause the failure it is meant to detect. `restart: unless-stopped`
covers process death instead.

### Beat is embedded in worker-email, which must stay at one replica

`worker-email` runs with `-B`, so Celery Beat runs inside it rather than as a fifth
container. Beat is required, not optional: `sweep_stuck_analyses` is the only backstop
for analyses killed by SIGKILL, OOM or machine death (ADR-0011), and it is what returns
the user's quota slot in those cases (ADR-0013). Without it those messages stay
`PENDING` forever and the user permanently loses a slot they never spent.

Embedding it saves a container and ~185 MB. The cost is a hard constraint: **two
replicas of `worker-email` would run two Beat schedulers and double-fire the reaper.**
Beat must be split into its own service before this worker is ever scaled horizontally.

Beat's schedule path is set explicitly to `/tmp/celerybeat-schedule` because it writes
three files (a shelve plus SQLite `-shm`/`-wal`) and would otherwise put them in the
working directory.

### Compose commands must pass --env-file .env.docker

This is not a style preference; the two mechanisms read different files:

| Mechanism                         | Feeds `${VAR}` in `compose.yaml` | Reaches the container |
|-----------------------------------|----------------------------------|-----------------------|
| `env_file: .env.docker` (service) | No                               | Yes                   |
| `--env-file .env.docker` (CLI)    | Yes                              | No                    |

Compose auto-loads only a file named exactly `.env`, and variables from a service's
`env_file:` are container-only — they are never available for interpolation inside
`compose.yaml`. Since `db` interpolates `${POSTGRES_PASSWORD}`, the flag is mandatory.
The `:?` guard on that variable turns a forgotten flag into a readable error instead of
an empty password and a confusing authentication failure later.

`POSTGRES_PASSWORD` is a Compose/Postgres bootstrap secret, not one of the eleven
application variables — the application never reads it. `FLASK_ENV=production` is the
config selector rather than a checked value, but setting it is what causes those eleven
to be validated at import of `config.py`, so a misconfigured container refuses to start
instead of running on silent fallbacks.

## Consequences

- **The stack needs more than 1 GB at peak.** Measured after driving real work through
  both queues: `web` 371 MB (Gunicorn, 2 workers), `worker-ml` 474 MB (prefork,
  concurrency 1), `worker-email` 167 MB (prefork, concurrency 2) — **1012 MB for the
  application alone**, before PostgreSQL (~50 MB) and Redis (~18 MB). `mem_limit` is set
  per service to the measured figure plus headroom, so a sizing mistake surfaces as a
  clean OOM locally rather than as a surprise on a host.
- **`web` + `worker-email` do not fit a 512 MB unit.** Together they measure 538 MB.
  Fitting 1 GB is possible by dropping to one web worker and running `worker-ml` with
  `--pool=solo` (666 MB total), but `--pool=solo` does not enforce the task time limits,
  which ADR-0011 relies on as the production backstop.
- **`worker-ml` uses prefork with concurrency 1, not solo.** Prefork enforces the
  soft/hard time limits; solo does not. Concurrency stays at 1 because each additional
  prefork slot on this worker costs ~271 MB once copy-on-write collapses. Note this is
  specific to the ML worker: `worker-email`'s children stayed at ~25 MB, because
  lightweight tasks never touch enough pages to materialise a private copy.
- **A hung worker is not detected.** Skipping the Celery health check is a real trade:
  a worker that is alive but wedged will not be restarted automatically. Accepted,
  because the alternative probe is more likely to cause an outage than to catch one.
  ADR-0011's reaper still moves the stuck *messages* to a terminal state.
- **PostgreSQL now enforces foreign keys for the first time.** SQLAlchemy's pysqlite
  driver leaves `PRAGMA foreign_keys = 0`, so no foreign key in this application had
  ever actually been checked — an orphan insert was verified to succeed on SQLite. Every
  cascade is ORM-side `delete-orphan` with no `ON DELETE` in the schema. Account
  deletion was therefore tested explicitly end-to-end against PostgreSQL, and removed
  the related records correctly.
- **The build context is 2.55 MB, not 518 MB.** `.dockerignore` excludes the 508 MB
  macOS `.venv` (which would break a Linux image), `.git`, `instance/`, the training
  data and notebooks, tests, and — critically — `.env` and `.flaskenv`. Docker does not
  read `.gitignore`, so those exclusions had to be restated. `.flaskenv` matters
  specifically: it sets `FLASK_DEBUG=1`, which was verified to enable the Werkzeug
  interactive debugger even under `FLASK_ENV=production`.
- **`.env.docker.example` needed a `.gitignore` exception.** The existing `.env.*` rule
  matched it, and `!.env.example` un-ignores only that exact name, so the example file
  would have been silently untracked.
- **Static files are served by Gunicorn.** There is no nginx, CDN or WhiteNoise, so all
  27 assets (392 KB) are served by sync workers, and there is no cache-busting or
  `SEND_FILE_MAX_AGE_DEFAULT`. Acceptable at this scale; a reverse proxy or CDN belongs
  in front before real traffic.
- **`compose.yaml` is a local production-like stack, not a deployment target.** It has
  no rolling deploys, no secrets manager and no autoscaling. Its value is that it runs
  the real role split against real PostgreSQL and Redis, and exercises the fail-fast
  configuration — which is what made the end-to-end verification below meaningful.

## Verification

The full stack was brought up in stages and tested end to end:

```bash
# One-time: copy the example and fill in real values
cp .env.docker.example .env.docker

# Build the single application image
docker compose --env-file .env.docker build

# Infrastructure first, then confirm both report healthy
docker compose --env-file .env.docker up -d db redis
docker compose --env-file .env.docker ps

# Schema, once, before any app service starts
docker compose --env-file .env.docker up migrate

# Application roles, one at a time, checking logs between each
docker compose --env-file .env.docker up -d web
docker compose --env-file .env.docker logs -f web

docker compose --env-file .env.docker up -d worker-ml
docker compose --env-file .env.docker logs -f worker-ml

docker compose --env-file .env.docker up -d worker-email
docker compose --env-file .env.docker logs -f worker-email

# Full stack
docker compose --env-file .env.docker up -d
docker compose --env-file .env.docker ps

# Teardown; -v also drops the pgdata and redisdata volumes
docker compose --env-file .env.docker down
```

Results: PostgreSQL and Redis reported healthy; migrations completed with exit code 0;
Gunicorn started two workers and `/healthz` reported healthy; the ML worker connected to
Redis, loaded its models and consumed the `ml` queue; the email worker started on the
default queue; an account was created; a real analysis completed through the
web -> Redis -> Celery ML path and its results were stored and displayed; account
deletion removed the related records through the cascades; all containers stopped
cleanly with `docker compose down`; and the named volumes were not removed.
