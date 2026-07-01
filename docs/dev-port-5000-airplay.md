# Dev server: moved off port 5000 (macOS AirPlay conflict)

A troubleshooting note: a "blank page" that looked like a Stage 2 regression turned out
to be macOS AirPlay squatting on port 5000. Recorded here so the diagnosis isn't lost.

## The symptom

After the Stage 2 changes, starting the app and opening `http://localhost:5000` showed a
**blank white page** — the page loaded (not "connection refused"), the terminal showed
the app `Running on...`, but no routes or content appeared. It looked like Stage 2 had
broken the app.

## What it was NOT

My first suspicion was the `SERVER_NAME=localhost:5000` setting added in Stage 2 — that
Flask's host-matching was 404'ing my routes. That was wrong, and I ruled it out:
host matching was off, and the test client returned 200 for every route. The app itself
was healthy — the home route rendered a full page, all static assets returned 200.

So "it broke right after Stage 2" was true, but Stage 2 did not break it. (A reminder
that a correlation in time is a hypothesis, not a cause.)

## The actual root cause

**macOS AirPlay Receiver was already listening on port 5000**, and the blank page was
its empty HTTP 403 response — not my app at all.

The evidence:

- `curl -v http://localhost:5000/` returned `HTTP/1.1 403 Forbidden` with
  `Server: AirTunes/...` — an Apple AirPlay response, not Flask. Flask never logged the
  request.
- `lsof` showed `ControlCenter` (the AirPlay Receiver process) listening on `*:5000` on
  both IPv4 and IPv6.
- `localhost` resolves to IPv6 `::1` *first*, then IPv4 `127.0.0.1`.

With Flask running, the two paths split:

| URL                   | reaches | result                |
|-----------------------|---------|-----------------------|
| `127.0.0.1:5000`      | Flask   | 200, full page        |
| `localhost:5000` (::1)| AirPlay | 403, empty → blank    |

Flask's dev server binds IPv4 `127.0.0.1` specifically (which wins over AirPlay's
wildcard there), so the IPv4 path reached Flask. The IPv6 `::1` path stayed with
AirPlay. The browser, following `localhost`, hit `::1` first → AirPlay → blank page.

## Why it only showed up after Stage 2

It was a latent conflict that Stage 2 exposed, not created:

- **Before Stage 2:** `SERVER_NAME` was unset, so the dev server advertised
  `http://127.0.0.1:5000`. I opened `127.0.0.1` → IPv4 → Flask. It worked.
- **Stage 2** set `SERVER_NAME=localhost:5000` (needed so the Celery worker can build
  email links outside a request). The dev server then advertised
  `http://localhost:5000`, and the run instructions said to open `localhost:5000`. The
  browser followed `localhost` → `::1` → AirPlay's 403 → blank page.

So Stage 2 only flipped the advertised host from `127.0.0.1` to `localhost`, which
routed me onto the IPv6 path that AirPlay had been squatting on all along.

## The fix

Moved the dev server off port 5000 — the only fix that makes the
`localhost`-vs-`127.0.0.1` resolution and the AirPlay conflict irrelevant, while keeping
`SERVER_NAME` consistent with how the app is actually reached.

Dev-only changes (production and test config untouched):

- dev server now runs on **port 5001**
- `SERVER_NAME` in the dev config → `localhost:5001`
- the `.env` dev value updated to match
- run-instruction docstrings that mentioned `localhost:5000` updated to `localhost:5001`

Production `SERVER_NAME` is unaffected (it comes from the real domain via env, not
localhost). The test config uses bare `localhost` (no port), which is fine for building
URLs in tests and did not change.

## Other options considered (and why not)

- **Disable AirPlay Receiver** (System Settings → AirDrop & Handoff → turn off AirPlay
  Receiver) frees port 5000, but it's a machine-wide setting affecting anything on this
  Mac — fixes my machine, not the project.
- **Just use `127.0.0.1:5000`** works immediately (IPv4 reaches Flask), but leaves the
  fragility which means risky port! in place for the next person and the next `localhost` link.

Moving the port is the only option that removes the fragility at the project level.

## The lesson kept

The timing made Stage 2 look guilty, but the evidence did not support that.
Checking the response with `curl`, checking the port with `lsof`, and confirming how `localhost`
resolved showed the real issue: the browser was reaching AirPlay on port 5000, not my Flask app.
