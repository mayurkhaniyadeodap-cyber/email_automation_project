# DeoDap Care Panel — Frontend (React + Vite)

The agent panel for the Care Engine API: login, Org → Brand dropdowns, the ticket
queue + Ignored tab, a ticket detail/thread view with the full action set (classify,
run engine, reply/draft, ignore/un-ignore, reclassify), an analytics dashboard, and a
Settings editor (AI/automation, block list, integration credentials, categories & rules).

## Stack
- **React 18** + **react-router-dom** (SPA, no extra state lib — `fetch` + context)
- **Vite 5** dev server / build
- Token auth (DRF `TokenAuthentication`), token in `localStorage`

## Quick start

```powershell
cd deodap-care\frontend
npm install
npm run dev          # http://localhost:5173
```

The Django backend must be running on `http://127.0.0.1:8000` (see `../backend`).
Vite proxies `/api` → the backend, so there are no CORS hoops. Point it elsewhere with:

```powershell
$env:VITE_API_TARGET = "http://127.0.0.1:9000"; npm run dev
```

Log in with the demo superuser **admin / admin** (created by `manage.py bootstrap_demo`).

```powershell
npm run build        # production build -> dist/
npm run preview      # serve the built dist/
```

## Layout

```
src/
  api.js            # fetch wrapper (token auth, query params) + login/me
  auth.jsx          # AuthProvider / useAuth (token -> /auth/me)
  scope.jsx         # ScopeProvider / useScope (Org + Brand dropdowns, doc §9)
  App.jsx           # routes (login gate -> Layout -> pages)
  main.jsx          # entry
  styles.css
  components/
    Login.jsx
    Layout.jsx      # top bar: Org/Brand selects + nav
    TicketList.jsx  # queue / Ignored tabs, status + search filters
    TicketDetail.jsx# thread, reply/draft, classify/decide/ignore/correct, audit
    Analytics.jsx   # volume / SLA / AI-accuracy / agent reports
    Settings.jsx    # sub-tab container (AI/automation, block list, integrations, taxonomy)
    settings/
      General.jsx     # AI provider/model/key, confidence, toggles, holding, SLA
      Integrations.jsx# Shopify / Shipping / GoKwik credentials
      BlockList.jsx   # block-list CRUD
      Taxonomy.jsx    # categories -> sub-topics -> rules/templates editor
      saveSettings.js # PATCH/POST helper for the BrandSettings row
    ui.jsx          # badges + date formatting
```

## API endpoints used
`/auth/token/`, `/auth/me/`, `/brands/`, `/categories/`, `/sub-topics/`, `/rules/`,
`/templates/`, `/settings/`, `/block-list/`, `/tickets/` (+ `reply`, `classify`,
`decide`, `ignore`, `unignore`, `correct`, `attachments`), and `/analytics/overview/`.
