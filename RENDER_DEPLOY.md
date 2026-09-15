# Deploying Vaultly to Render — permanent URL + persistent database

## Why this is needed

The sandbox URL (`…daytonaproxy01.net`) and the earlier
`…trycloudflare.com` tunnel are both **temporary**: they exist only while the
sandbox is alive. The quick tunnel also has a documented habit of dying (the
hostname is released when the tunnel process stops), which is exactly the
"link shows an error" problem this fixes.

Render gives you a stable `https://<name>.onrender.com` URL, and the disk
declared in `render.yaml` keeps the SQLite database — i.e. **your user
accounts** — alive across restarts and redeploys.

---

## What you need

* A **GitHub** account (free) — to host the code.
* A **Render** account (free to sign up) — to run it.

No code changes are required. The repo is already git-initialised and
committed.

---

## Step 1 — push this folder to GitHub

Create an empty repository on GitHub (e.g. `vaultly`). **Do not** add a
README or .gitignore when creating it. Then, from inside this folder:

```bash
cd webpages/withdrawal-wireframe_v7

git remote add origin https://github.com/<YOUR-USERNAME>/vaultly.git
git branch -M main
git push -u origin main
```

If it asks for a password, use a **Personal Access Token**
(GitHub → Settings → Developer settings → Personal access tokens → Fine-grained
tokens, with *Contents: Read and write* on that repo) — GitHub no longer
accepts account passwords over HTTPS.

Confirm the push worked: the repo should show `server.py`, `index.html`,
`styles.css`, `app.js`, `icons.js`, `render.yaml`, `Dockerfile` and `assets/`.
It should **not** show `data/` or any `.db` file — those are git-ignored on
purpose, because the database belongs on the Render disk, not in git.

## Step 2 — deploy with the Blueprint

1. Sign in to <https://dashboard.render.com>.
2. Click **New +** → **Blueprint**.
3. Pick the repository you just pushed.
4. Render reads `render.yaml` and shows the plan: a Docker web service named
   `withdrawal-wireframe` with a 1 GB disk mounted at `/app/data`.
5. Click **Apply**.

Render now builds `./Dockerfile` and starts the service. Expect roughly
2–4 minutes for the first build — it is pulling `python:3.11-slim` and copying
the app. Watch the build log for `Listening on 0.0.0.0:8000` at the end.

> **Note on the plan:** a persistent disk is **not available on the free
> plan**. `render.yaml` sets `plan: starter` for that reason. If you switch to
> free, remove the `disk:` block — but then accounts will be wiped on every
> redeploy, which defeats the purpose.

## Step 3 — verify it

Render shows your URL at the top of the service page
(`https://<something>.onrender.com`). Check the API first:

```bash
curl https://<something>.onrender.com/api/health
```

Expected:

```json
{"ok": true, "users": 0, "storage": "sqlite"}
```

Then open the URL in a browser: the Vaultly landing page should render with
styling and the hero image. Create an account, log out, and log back in. To
confirm persistence, open the URL on a **different device** and sign in with
the same credentials.

---

## About the persistent disk

```yaml
disk:
  name: wf-data
  mountPath: /app/data
  sizeGB: 1
```

paired with

```yaml
- key: DB_PATH
  value: /app/data/app.db
```

The database file lives on the disk, *outside* the container image, so it is
untouched by redeploys. **If you delete the disk, every account is lost** —
the next deploy starts from an empty database.

All runtime state is confined to that one directory. There are no env-var
secrets to leak, because the only credentials are the users' own passwords,
stored as salted PBKDF2 hashes inside the database.

---

## Why the deployment was fixed first

The original `Dockerfile` contained:

```dockerfile
COPY server.py index.html ./
```

`index.html` pulls in `styles.css`, `app.js`, `icons.js` and
`assets/hero-vault.jpg`. None of them were copied into the image, so a Render
deploy would have loaded as **unstyled HTML with no working sign-in** — the
page would have 404'd on every asset.

It now copies the whole app and excludes runtime state:

```dockerfile
COPY . .
```

with a `.dockerignore` that keeps `data/`, `logs/`, `*.db` and `__pycache__`
out of the image. This was verified by building a stand-in copy of exactly
what `COPY . .` produces and serving it — all six routes returned 200 with
correct content types.

The old `render.yaml` also hardcoded `PORT: "8000"`, which conflicts with the
port Render injects. That override is gone; `server.py` reads `PORT` from the
environment either way.

---

## Troubleshooting

**Build fails at `COPY . .`** — make sure the push included the static files
(`git ls-files` should list `styles.css`, `app.js`, `icons.js`).

**Service starts then health check fails** — check the log for the
`database : /app/data/app.db` line. If the disk failed to mount, that path is
not writable and SQLite cannot open.

**Site loads but looks unstyled** — the static files are missing from the
image; re-check what the build actually copied.

**`users` count is 0 after a redeploy** — the disk was removed or its
mount path changed.

---

## Deployment status (this attempt)

**Deployment to Render was NOT executed from this environment — no credentials
are available.**

Checked, conclusively:

| Where | Result |
|---|---|
| Environment variables | No `RENDER_API_KEY`, no `RENDER_*`, no `GITHUB_TOKEN` / `GH_TOKEN` / `GIT_*` credentials. Only unrelated API keys (model, search, media) are present. |
| Connected credential profiles | **0 profiles** exist for this account (`get_credential_profiles` → empty). |
| Composio / MCP tool catalog | No Render integration exists in the catalog at all. GitHub exists but is **not connected** — connecting it would only cover the code-hosting half. |
| Local git / CLI | `git` is present but unauthenticated (no `~/.gitconfig`, no `~/.netrc`). The `gh` CLI and any `render` CLI are not installed. |

What that means, precisely:

- The **code side is ready**: this folder is a complete, self-contained app with a
  working `Dockerfile` (which copies the whole app — see the fix note above) and a
  `render.yaml` that requests a **1 GB persistent disk** mounted at `/app/data`, with
  `DB_PATH=/app/data/app.db` so the SQLite database — i.e. the user accounts —
  survives restarts and redeploys. Nothing needs to be written or changed to deploy.
- The **only missing piece is authorization**: a Render API key, or a GitHub
  account/token to host the repo that Render's Blueprint then deploys. Without one of
  those, no service can be created and no code can be pushed. This is not a code or
  configuration problem, and there is nothing further to prepare.

**To unblock it, connect either one:**

- **Render API key** — Render Dashboard → *Account Settings* → *API Keys* → create a
  key, then supply it to the agent. With that, the Blueprint deploy can be run
  non-interactively and the permanent `https://<name>.onrender.com` URL returned.
- **GitHub account/token** — connect a GitHub credential profile (or provide a
  fine-grained Personal Access Token with *Contents: Read and write*). Then push this
  folder and deploy via **New + → Blueprint** (Step 1 and Step 2 above).

Until one of those is supplied, use the manifest Steps 1–3 above; they take about
five minutes by hand and need no code changes.

### While that is pending: the live fallback URL is up and verified

**https://8000-52d02041-6433-4008-9c1b-85cc96d14832.daytonaproxy01.net**

This is the sandbox's stable port-forward hostname. It serves this app (and its API)
on the same origin, so the session cookie works with no CORS configuration.

It was verified end-to-end against **that public URL** — 37 checks, 0 failures —
covering: service health; server-side validation (invalid email, password under 5
characters, password mismatch, duplicate account → 409, wrong password → 401); sign-up
on one session; **a cross-device login from a second, independent session that holds a
genuinely different session token**; the withdrawal flow (methods, live quote, amount
+ 1.00% fee − $1.00 minimum, insufficient-funds rejection, below-minimum rejection,
history); unauthenticated API access refused with 401; logout; and a **third fresh
session reading the same account's history** to prove server-side persistence.
Static assets were confirmed reachable in the served HTML.

Re-run it yourself at any time:

```bash
BASE=https://8000-52d02041-6433-4008-9c1b-85cc96d14832.daytonaproxy01.net ./tests/e2e.sh
```

> This hostname is still **temporary** — it lives only as long as the sandbox does.
> It is a much better fallback than the old `trycloudflare.com` tunnel (which died
> without warning), but it is not the permanent URL this folder is set up to give you.
