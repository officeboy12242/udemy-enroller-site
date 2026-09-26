# Udemy Enroller Site

Standalone web app that automatically enrolls your Udemy account in free
(100%-off) courses. Log in with your Udemy email & password and the access
token is captured automatically — or paste a token manually if Udemy asks
for a captcha. A server-side engine keeps enrolling in the background
**even when the site is closed**, and the dashboard shows exactly what it
did while you were away.

> Personal automation tool. Not affiliated with Udemy, Inc. Use with your
> own account and at your own risk.

## Features

- **Direct Udemy login** — email + password, token captured automatically;
  manual `access_token` paste as fallback
- **24/7 auto-enroll engine** — server-side worker checks for new free
  courses every 10 min (configurable) and enrolls your account(s)
- **Dashboard** — engine status, last/next run, Enroll Now, stats, recent
  enrollments, per-account auto toggle
- **Free courses feed** — Udemy-style cards, per-course Enroll, Enroll All
  with live progress bar
- **History** — full enrollment log with source (auto / manual)
- **My courses** — search inside your linked Udemy account(s)
- **Security** — access-code gated registration, encrypted tokens at rest
  (Fernet), PBKDF2 password hashing, CSRF on every form, rate-limited
  logins, HttpOnly session cookies, API docs disabled

## Local setup

```bash
cd E:\Projects\udemy-enroller-site
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env        # then edit SECRET_KEY
.venv\Scripts\python run.py   # http://localhost:8123
```

First registered account becomes the admin.

## Configuration (.env)

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | auto-generated | Signs sessions + derives token encryption key |
| `PORT` | `8123` | HTTP port |
| `AUTO_ENROLL_INTERVAL` | `120` | Engine tick, seconds (matches the tgbot2 reference bot) |
| `AUTO_ENROLL_ENABLED` | `1` | Auto-enroll ON for new users by default |
| `FEED_CACHE_TTL` | `900` | Feed cache seconds |
| `LOGIN_MAX_ATTEMPTS` / `LOGIN_WINDOW_SECONDS` | `8` / `900` | Login rate limit |

## Deployment (Render)

A `render.yaml` blueprint is included. Deploy from a Git repo:

1. Push this repo to GitHub (or GitLab).
2. Render Dashboard → New → Blueprint → pick the repo → Apply. `render.yaml`
   sets the service up and generates `SECRET_KEY`.
3. Set `SESSION_SECURE=1` (the deploy is HTTPS).

### Connecting Udemy accounts when hosted

The "open a Udemy login window" flow only works when the app runs on your own
PC (it drives a real browser on the host machine). Udemy also guards every
login (password and passwordless) with Cloudflare Turnstile, so a server can't
log in on your behalf. When hosted, the Connect buttons therefore point to
`/connect`, which uses a small **browser extension** (in `extension/`):

1. Download it from the Connect page (`/connect/extension.zip`), unzip, and
   load it unpacked (`chrome://extensions` → Developer mode → Load unpacked).
   Chrome/Edge/Brave and Android Firefox/Kiwi are supported; iOS is not.
2. Copy the pairing code from the Connect page into the extension once.
3. Log in to udemy.com in your own browser (Turnstile passes there), then click
   the extension's Connect button.

The extension reads the Udemy session cookies in your browser and POSTs them to
`/connect/token`, authenticated by a signed pairing code (resettable from the
Connect page). The token is sent in the request body only, never in a URL/log.
Your Udemy password is never seen by the site.

### Free plan caveats

- **Sleeps when idle** (~15 min), which pauses the 2-minute auto-enroll engine.
  The built-in self-ping (`KEEPALIVE_INTERVAL`, auto-on when a public URL is
  known) keeps it awake only while it is already running; to guarantee 24/7,
  use the `starter` plan or an external uptime pinger hitting `/healthz`.
- **Ephemeral disk** — the SQLite DB and `.secret_key` under `data/` reset on
  every deploy/restart, so users, connected accounts and history are lost.
  For persistence, use the `starter` plan with a mounted disk (see the
  commented `disk:` block in `render.yaml`).

Relevant env vars: `PUBLIC_URL` (defaults to `RENDER_EXTERNAL_URL`),
`KEEPALIVE_INTERVAL` (seconds, 0 disables), `LOCAL_BROWSER_LOGIN`
(auto-off when hosted), `SESSION_SECURE`.

## Project layout

```
app/
  main.py                app factory: middleware, engine lifespan, router mounts
  config.py              env configuration
  security.py            hashing, encryption, CSRF, rate limits, sessions
  db.py                  SQLite connection, schema, migrations
  models/                data access split by concern
    users.py accounts.py enrollments.py auto_state.py settings.py
  services/
    udemy_client.py      enroller core (course-id extraction, checkout)
    udemy_login.py       token verification (multi-strategy)
    browser_grab.py      login-window flow (user signs in, token captured)
    feed.py              free-course feed (internal source, not exposed)
    enroll_service.py    unified single/batch/auto pipeline + live progress
    stats.py             dashboard aggregates + chart series
  web/
    deps.py              templates, session, auth dependency, CSRF, render
    routers/             auth, dashboard, accounts, enroll, pages
  templates/             Jinja2 pages (Udemy theme, light + dark)
  static/css/app.css     design system
  static/js/app.js       animations, live polling, toasts, theme toggle
run.py                   uvicorn entry
Dockerfile, render.yaml
```
