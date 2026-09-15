# Vaultly — a real withdrawal product, with a real backend

A polished, responsive fintech withdrawal site. It shows **one focused screen at a
time** (a five-step flow) instead of tiling panels on a single page, and every piece
of account state — users, sessions and withdrawals — lives on the **server**.

---

## Live URL

**https://8000-52d02041-6433-4008-9c1b-85cc96d14832.daytonaproxy01.net**

This is the sandbox's stable port-forward hostname (it is **not** an ephemeral
`trycloudflare.com` tunnel — those died repeatedly). It serves the app and the API
from the same origin, so the session cookie works without any CORS setup.

> If the hostname ever stops resolving, the sandbox was torn down or restarted.
> Re-run `./start.sh` locally, or deploy permanently with the `Dockerfile` /
> `render.yaml` below. No code changes are required for either.

## What changed from v3 (the wireframe)

| | v3 — wireframe | v4 — designed product |
|---|---|---|
| Look | hand-drawn notebook paper, sketchy borders, handwriting fonts | real product design: Inter + Instrument Serif, glassmorphic cards, emerald-on-ink palette, real SVG icon set, real 3D hero illustration |
| Layout | four sketched panels shown together | **one screen at a time**, five-step flow with a progress stepper |
| Screens | one page | `auth → dashboard → amount → method → review → receipt` |
| Marks | "11.6%", "choose from options", "1st/2nd Phase" scaffolding | real fee model (1.00%, $1.00 min), real limits, live server-computed quotes, real receipt reference |
| Backend | auth only (signup/login/logout/session) | **+ withdrawal endpoints**: methods, quote, create, history — all session-authenticated |
| Withdrawals | not persisted | stored in SQLite, balance/limit debited, listed in history |
| Files | single `index.html` | `index.html` + `styles.css` + `app.js` + `icons.js` + `assets/` |

Auth, hashing, sessions and the validation messages are **unchanged** — the rules and
their exact wording are still the single source of truth in `server.py` (`MSG`).

## The flow, one screen at a time

1. **Auth** — marketing pitch + real auth card (Create account / Log in tabs)
2. **Overview** — available funds, limit, withdrawn total, recent activity, "Withdraw funds"
3. **Amount** — amount entry, quick chips, live server quote (amount / fee / you receive)
4. **Method** — CashApp, PayPal, Chime, Zelle, BTC, Local bank + the destination field
5. **Review** — full summary, then **Confirm withdrawal**
6. **Receipt** — server reference, updated balances, copy-reference, make another

## Run it locally

```bash
cd webpages/withdrawal-wireframe_v7
PORT=8000 DB_PATH=./data/app.db python3 server.py
# -> http://127.0.0.1:8000
```

Standard library only — no `pip install`, no build step, no `node_modules`.
`./start.sh` additionally supervises the process and opens a public tunnel.

## Deploy permanently

`Dockerfile` and `render.yaml` are both included and still valid:

- **Render:** point it at this folder. `render.yaml` requests a persistent disk
  (`/var/data`) and sets `DB_PATH=/var/data/app.db`, so accounts survive redeploys.
- **Any Docker host:** `docker build -t vaultly . && docker run -p 8000:8000 -v vaultly:/var/data vaultly`

A TLS-terminating proxy flips the cookie's `Secure` flag on automatically (the app
reads `X-Forwarded-Proto`). Remember to mount a volume — without one, the SQLite file
is lost on redeploy.

## API

All withdrawal endpoints require a live session (otherwise `401 auth.required`).

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/api/signup` | `{email, password, confirm, name?}` → `201` + session cookie |
| POST | `/api/login` | `{email, password}` → `200` + session cookie |
| POST | `/api/logout` | clears the cookie, deletes the server session |
| GET | `/api/session` | `{user}` or `{user: null}` |
| GET | `/api/methods` | the six withdrawal methods + ETAs |
| POST | `/api/withdrawal/quote` | `{amount}` → `{amount, fee, net, funds, limit}` |
| POST | `/api/withdrawal` | `{amount, method, destination}` → `201` + reference |
| GET | `/api/withdrawal/history` | balances + up to 50 withdrawals |
| GET | `/api/health` · `/api/stats` | health, user and session counts |

## Security

- Passwords: **PBKDF2-HMAC-SHA256, 200,000 iterations, unique 16-byte salt**, never
  stored, logged or returned. Verified with `hmac.compare_digest`.
- Sessions: 32 random bytes; only the **SHA-256 hash** of the token reaches the
  database, so a DB leak yields no usable sessions.
- Cookie: `HttpOnly; SameSite=Lax` (`Secure` over HTTPS) — page scripts cannot read
  it, and no client-side state is trusted.
- Login throttle: 15 attempts / 5 min per IP. Static serving is path-traversal
  guarded. HTML is served with a `default-src 'self'` CSP.

## Validation rules

Enforced server-side; the front end mirrors them for instant feedback. The server
message is always the one rendered, so the copy can never drift.

| Case | Message |
|---|---|
| Empty email | "Enter your email address." |
| Invalid email | "That email does not look right — use the format name@example.com." |
| Password < 5 chars | "Password needs at least 5 characters." |
| Confirm mismatch | "The two passwords do not match." |
| Duplicate account | 409 "That email is already registered — switch to Log in." (`409`) |
| Unknown account | "No account found for that email — sign up first." (`404`) |
| Wrong password | 401 "Incorrect password — try again." |
| Too many attempts | 429 "Too many attempts — wait a few minutes and try again." |
| Amount < $10 | "The smallest withdrawal is $10.00." |
| Above limit / funds | "That is more than your available limit." |
| Bad destination | "That destination does not look right for the method you picked." |

## Files

```
index.html      one screen per section, wired by app.js
styles.css      design system: tokens, glass cards, buttons, responsive rules
app.js          screen router, API calls, validation, session-driven auth state
icons.js        inline SVG icon set (window.ICON / window.paintIcons)
assets/         hero-vault.jpg (generated 3D vault illustration)
server.py       HTTP server + SQLite + auth + withdrawal API (stdlib only)
start.sh        supervise server (+ optional tunnel)
Dockerfile      container image
render.yaml     Render blueprint with a persistent disk
data/app.db     SQLite database (gitignored)
```

## Colour palette

Ink `#04080c → #1a2a35`, emerald `#12c282` (light `#6ceab9`, deep `#0a7d55`),
text `#f2f7f5 / #c3d2d0`, amber `#f2c14e` for warnings.

## Notes

- Balances start at $100,000 funds / $55,000 limit and debit from a local SQLite
  database on the server, so they follow the account across devices.
- Withdrawals are recorded as `pending`; there is no settlement step and the
  reference is generated by the server.
- Passwords are hashed with PBKDF2-HMAC-SHA256 (200,000 iterations, unique salt).
- The earlier versions are untouched: `../withdrawal-wireframe` (v1 wireframe),
  `../withdrawal-wireframe_v2` (localStorage → backend), `../withdrawal-wireframe_v3`,
  `../withdrawal-wireframe_v4` (designed product), `../withdrawal-wireframe_v5`
  (deploy fixes) and `../withdrawal-wireframe_v6`.
