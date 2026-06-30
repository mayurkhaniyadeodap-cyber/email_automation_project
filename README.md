# DeoDap Care Panel — Email Support Automation Engine

An AI-assisted email support and ticketing platform. It ingests customer emails,
classifies them, runs a deterministic decision engine (verification → evidence →
ticket), auto-replies, escalates high-priority cases, and syncs tickets to an
external Care Panel — with a React dashboard for agents.

> **Status:** 🟢 Live in production (active development). Backend has an extensive test suite (590+ tests).

---

## Overview

Customers email a support inbox. The engine:

1. **Fetches** mail over IMAP (or the Gmail API).
2. **Classifies** each email (AI provider or deterministic keyword rules).
3. **Decides** the next action via a category-first policy: verify the customer →
   request evidence (photo/video/payment screenshot) → create a ticket.
4. **Auto-replies** with the right template at each step.
5. **Escalates** legal / grievance / fraud emails to a manual-review queue (no automation).
6. **Routes internal** company mail to a separate Internal Communications inbox.
7. **Syncs** finalized tickets to the external Care Panel and emails the customer a
   tracking link.

Agents work the queues from the React dashboard (Inbox, Tickets, Escalation,
Internal Communications, Dashboard analytics).

## Features

- 📥 **Email ingestion** — IMAP polling or Gmail API (OAuth + Pub/Sub push).
- 🤖 **AI classification** — Gemini / Groq / OpenAI, with a keyword-rule fallback (works with no API key).
- 🧭 **Decision engine** — category-first evidence policy (Damaged / Defective / Wrong / Missing / Quantity / Quality / Payment-screenshot / Website-App).
- ✅ **Verification & evidence flow** — verifies the customer against Shopify, then collects mandatory photo/video before a ticket.
- 🎫 **Smart ticketing** — de-dupes replies into one thread; syncs to the external Care Panel with a tracking hash.
- 🚨 **Escalations** — legal / consumer-court / grievance / fraud → manual-review queue, no auto-reply.
- 🏢 **Internal communications** — internal-recipient mail kept out of the support pipeline.
- 🔔 **Live notifications** — sidebar unread badges + toasts for new Escalations and Internal mail.
- 📊 **Analytics** — dashboard, SLA, AI accuracy, agent performance, manual/auto reply reports.
- 🏷️ **Multi-tenant scoping** — Organization → Brand → Mailbox.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **Backend** | Python 3, Django 5.1, Django REST Framework |
| **Frontend** | React 18, Vite, Material UI (MUI), React Router |
| **Database** | SQLite (dev); any Django-supported DB in production |
| **AI Provider** | Google Gemini / Groq / OpenAI (pluggable; keyword-rule fallback) |
| **Auth** | DRF Token authentication (with optional auto-login) |
| **Integrations** | Gmail API, Shopify Admin API, shipping/courier portal, Care Panel store-json |
| **Scheduling** | APScheduler in dev (auto-fetch + waiting-state sweeps); **cron** in production (gunicorn does not run the in-process scheduler) |

## Folder Structure

```
.
├── Web/
│   ├── backend/                  # Django project
│   │   ├── deodap_care/          # settings, urls, api routes, wsgi/asgi
│   │   ├── apps/
│   │   │   ├── organizations/    # orgs, brands, mailboxes, users, scoping
│   │   │   ├── brand_settings/   # per-brand config, support emails, block list
│   │   │   ├── taxonomy/         # categories, sub-topics, rules, templates
│   │   │   ├── classifier/       # AI classification (provider-agnostic)
│   │   │   ├── decision/         # decision engine + policy
│   │   │   ├── ingestion/        # IMAP/Gmail fetch, evidence/verify flow, escalation
│   │   │   ├── tickets/          # tickets, messages, pending, escalations, internal mail
│   │   │   ├── integrations/     # Shopify, shipping, Care Panel store/media
│   │   │   └── analytics/        # dashboards, reports, exports
│   │   ├── manage.py
│   │   ├── requirements.txt
│   │   └── .env.example
│   └── frontend/                 # React + Vite app
│       ├── src/
│       │   ├── components/       # Dashboard, Inbox, Tickets, Escalations, etc.
│       │   ├── api.js            # fetch wrapper (token auth)
│       │   └── main.jsx
│       └── package.json
├── Credentials.json.example      # Google OAuth client template
├── .gitignore
└── README.md
```

## Installation

### Prerequisites
- Python 3.11+
- Node.js 18+

### Backend

```bash
cd Web/backend

# 1. Virtual environment
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# 2. Dependencies
pip install -r requirements.txt

# 3. Configuration
cp .env.example .env              # then edit .env with your values

# 4. Database
python manage.py migrate

# 5. (optional) create an admin
python manage.py createsuperuser

# 6. Run
python manage.py runserver        # http://localhost:8000
```

Run the test suite:

```bash
python manage.py test
```

### Frontend

```bash
cd Web/frontend
npm install
npm run dev                       # http://localhost:5173
```

The dev server proxies `/api` to the Django backend on port 8000.

## Environment Variables

- **`.env`** — copy `Web/backend/.env.example` to `Web/backend/.env` and fill in real
  values. Every variable has a safe default for local dev, so the app and tests run
  with an almost-empty file. Key groups: Django core, Email (IMAP/SMTP), Gmail API,
  AI provider keys, Shopify/shipping, Care Panel, automation timers, admin bootstrap.
- **`Credentials.json`** — only needed for the **Gmail API** provider. Copy
  `Credentials.json.example` to `Credentials.json` and paste your Google OAuth client
  (from Google Cloud Console → APIs & Services → Credentials).

> 🔒 Never commit `.env` or `Credentials.json` — both are git-ignored.

## Build

Frontend production build:

```bash
cd Web/frontend
npm run build                     # outputs to Web/frontend/dist/
npm run preview                   # preview the production build locally
```

Backend (collect static if you serve the SPA via Django/whitenoise, then run with a
WSGI/ASGI server):

```bash
cd Web/backend
python manage.py collectstatic --noinput
gunicorn deodap_care.wsgi:application --bind 0.0.0.0:8000
```

## Deployment

1. Set `DJANGO_DEBUG=False`, a strong `DJANGO_SECRET_KEY`, and a real
   `DJANGO_ALLOWED_HOSTS` / `CORS_ALLOWED_ORIGINS`.
2. Run `migrate` (SQLite is fine for low volume; switch to PostgreSQL for scale).
3. Serve the API with **gunicorn** behind nginx; build the frontend and serve
   `dist/` from nginx.
4. Configure the email provider (IMAP app password, or Gmail OAuth + Pub/Sub push).
5. Set `PUBLIC_BASE_URL` to this app's public URL (for tracking links).
6. **Run the background jobs from cron**, not APScheduler — gunicorn does **not**
   start the in-process scheduler (it only runs under `runserver`). Schedule
   `manage.py fetch_emails` and `manage.py sweep_waiting` (see `deploy/crontab.txt`).

A full step-by-step EC2 (Ubuntu + nginx + gunicorn + cron) guide lives in
**[DEPLOYMENT.md](DEPLOYMENT.md)**, with ready-to-use configs under `deploy/`.

### Live deployment

The Care Panel is **live in production** on AWS EC2 (Ubuntu), behind nginx with a
`deodap-care` systemd/gunicorn service and cron-driven mail fetch:

- **Frontend** — nginx serves the React `dist/` build.
- **API** — nginx reverse-proxies `/api`, `/admin`, `/static` to gunicorn.
- **Background jobs** — `fetch_emails` (every 5 min) and `sweep_waiting` (every 30 min) via cron.
- **Email** — Gmail over IMAP (fetch) + SMTP (replies), using a 16-char app password.

**Update after new commits:**

```bash
cd /path/to/Email_Automation && git pull
cd Web/backend && source venv/bin/activate \
  && pip install -r requirements.txt \
  && python manage.py migrate && python manage.py collectstatic --noinput
cd ../frontend && npm install && npm run build
sudo systemctl restart deodap-care && sudo systemctl reload nginx
```

## API Endpoints

Base path: `/api/`. Auth: `Authorization: Token <token>`.

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/auth/login/` | Obtain an auth token |
| POST | `/api/auth/guest/` | Auto-login (when enabled) |
| GET | `/api/auth/me/` | Current user + permissions |
| GET/POST | `/api/tickets/` | List / act on tickets |
| GET | `/api/pending/` | Awaiting-evidence conversations |
| GET/POST | `/api/escalations/` | Escalation queue (+ `…/unread_count/`) |
| GET/POST | `/api/internal-emails/` | Internal communications (+ `…/unread_count/`) |
| GET | `/api/settings/` | Per-brand settings |
| POST | `/api/gmail/fetch/` | Trigger a mail fetch |
| GET | `/api/analytics/dashboard/` | Manager dashboard metrics |
| POST | `/api/gmail/webhook` | Gmail Pub/Sub push |
| POST | `/api/care-panel/webhook` | Care Panel → engine agent replies |

Full router resources: organizations, brands, mailboxes, categories, sub-topics,
rules, templates, settings, block-list, support-emails, tickets, messages,
audit-log, users, pending, escalations, internal-emails.

## Screenshots

> _Add screenshots here._
>
> - Dashboard — `docs/screenshots/dashboard.png`
> - Inbox / Tickets — `docs/screenshots/tickets.png`
> - Escalation queue — `docs/screenshots/escalation.png`

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `Author identity unknown` on commit | `git config user.email "you@example.com"` |
| No emails fetched | Check `IMAP_*` values; Gmail/Zoho need an **app password**, not your login password |
| Auto-replies not delivered | Ensure `REPLY_FROM` / IMAP user is an **authorized SMTP sender**; check `SEND_CONFIRMATION_*` logs |
| AI classification skipped | No `GEMINI_API_KEY`/`GROQ_API_KEY`/`OPENAI_API_KEY` set — it falls back to keyword rules (expected) |
| Care Panel issue shows "Other…" | Set the correct `CARE_PANEL_*_ISSUE_ID` env values to your panel's real ids |
| Frontend can't reach API | Confirm Django runs on :8000 and `CORS_ALLOWED_ORIGINS` includes the Vite origin |
| `python manage.py migrate` errors | Activate the venv and `pip install -r requirements.txt` first |

## License

No license file is currently included. Add a `LICENSE` (e.g. MIT) to make reuse terms
explicit. Until then, all rights are reserved by the repository owner.
