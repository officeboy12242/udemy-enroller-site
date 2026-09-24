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
copy .env.example .env        # then edit SECRET_KEY + SITE_ACCESS_CODE
.venv\Scripts\python run.py   # http://localhost:8123
```

First registered account becomes the admin.

## Configuration (.env)

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | auto-generated | Signs sessions + derives token encryption key |
| `SITE_ACCESS_CODE` | `changeme-access-code` | Required to register |
| `PORT` | `8123` | HTTP port |
| `AUTO_ENROLL_INTERVAL` | `600` | Engine tick, seconds |
| `AUTO_ENROLL_ENABLED` | `1` | Auto-enroll ON for new users by default |
| `ENROLL_BATCH_LIMIT` | `50` | Max offers per run |
| `FEED_CACHE_TTL` | `900` | Feed cache seconds |
| `LOGIN_MAX_ATTEMPTS` / `LOGIN_WINDOW_SECONDS` | `8` / `900` | Login rate limit |

## Deployment

Render blueprint included (`render.yaml`, free plan, health check
`/healthz`). Set `SECRET_KEY`, `SITE_ACCESS_CODE`, and mark the session
cookie secure (add `secure=True` in `app/main.py` `_set_session_cookie`)
when running behind HTTPS. Data is stored in SQLite under `data/` — on
Render free plan the disk is ephemeral, so accounts/enrollments reset on
redeploy; mount a disk or accept the reset.

## Project layout

```
app/
  main.py            FastAPI app: routes, engine, middleware
  config.py          env configuration
  security.py        hashing, encryption, CSRF, rate limits, sessions
  store.py           SQLite persistence
  udemy_enroller.py  enroller core (from tgbot2, verbatim)
  udemy_login.py     Udemy email+password login -> token capture
  feed.py            free-course feed (internal source, not exposed)
  enroll_service.py  single/batch/auto enrollment orchestration
  templates/         Jinja2 pages (Udemy theme)
  static/css/        Udemy-inspired stylesheet
run.py               uvicorn entry
Dockerfile, render.yaml
```
