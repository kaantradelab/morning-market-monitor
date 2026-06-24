# Deploy — Morning Market Monitor

The brief runs once a morning on GitHub Actions, commits the per-day JSON back to
the repo (durable history), and publishes a static HTML site to GitHub Pages.

There is **no server**. Everything is a scheduled CI job + static Pages hosting.

---

## Architecture (one glance)

```
cron 0 6 * * * UTC  ──▶  GitHub Actions  ──▶  python -m morning_monitor.main
(= 09:00 Istanbul,        (ubuntu-latest,        │
 UTC+3, no DST)            py3.13)                ├─ data/<YYYY-MM-DD>.json  ──▶ git commit + push (history)
                                                  └─ site/index.html        ──▶ GitHub Pages (latest)
                                                     site/archive/<date>.html   archive browsing
```

Scheduled crons on GitHub are **best-effort** — they can fire a few minutes late
(occasionally more, rarely skipped under heavy load). For a once-a-morning brief on
EOD/snapshot data this is fine. Use **Run workflow** (workflow_dispatch) for a
guaranteed manual run.

---

## Kaan-gated setup (one-time)

These steps need repo-owner / org-admin rights — do them once.

### 1. Create the repo under the `kaantradelab` org

```bash
gh repo create kaantradelab/morning-market-monitor --private --source . --remote origin --push
```

Or via the UI: **github.com/organizations/kaantradelab → New repository →**
name `morning-market-monitor` → private → create → then push this working tree:

```bash
git remote add origin git@github.com:kaantradelab/morning-market-monitor.git
git push -u origin main
```

### 2. Add the two repo secrets

The pipeline reads API keys from the environment; in CI they come from repo secrets.
**Never commit the real keys** — only `.env.example` (empty) is in the repo.

```bash
gh secret set FRED_API_KEY    --repo kaantradelab/morning-market-monitor   # FRED (St. Louis Fed) API key
gh secret set FINNHUB_API_KEY --repo kaantradelab/morning-market-monitor   # Finnhub calendar key
```

Or via the UI: **Settings → Secrets and variables → Actions → New repository secret**
for each of `FRED_API_KEY` and `FINNHUB_API_KEY`.

> The run degrades gracefully if a key is missing (affected tiles flag stale/missing),
> so the site still publishes — but you want both keys set for a full brief.

### 3. Enable GitHub Pages — source = **GitHub Actions**

**Settings → Pages → Build and deployment → Source = "GitHub Actions"**.

This repo deploys via `actions/upload-pages-artifact` + `actions/deploy-pages`
(the Actions source, **not** a `gh-pages` branch). No branch to create.

The workflow already declares the required permissions
(`pages: write`, `id-token: write`) and the `github-pages` environment, so no
further config is needed once the source is set to Actions.

### 4. (org only) Allow Actions to push commits

The job commits the per-day JSON back to `main`. Ensure
**Settings → Actions → General → Workflow permissions = "Read and write permissions"**
(or rely on the per-workflow `permissions: contents: write`, which is already set).

---

## Trigger a manual run

- **UI:** repo → **Actions → morning-brief → Run workflow** (optionally set a
  `date` input as `YYYY-MM-DD` to backfill a specific Istanbul logical date).
- **CLI:**

  ```bash
  gh workflow run morning.yml --repo kaantradelab/morning-market-monitor
  # backfill a specific date:
  gh workflow run morning.yml --repo kaantradelab/morning-market-monitor -f date=2026-06-24
  ```

First successful run publishes the site at:

```
https://kaantradelab.github.io/morning-market-monitor/
```

---

## Local run (offline, keyless)

No secrets needed — run against the bundled fixture:

```bash
pip install -r requirements.txt
python -m morning_monitor.main --config config.yaml \
  --fixture tests/fixtures/sample_run.json --date 2026-06-24
open site/index.html
```

For a real local run, copy `.env.example` to `.env` and fill in your keys
(`.env` is gitignored and never committed).

---

## Secret hygiene (enforced)

- `.gitignore` ignores `.env` and `.env.*` (except `.env.example`) and all local
  caches — **no secret is ever committed**.
- CI injects keys only as step-scoped `env:` from `secrets.*`; they are never
  written to disk or echoed.
- The committed-back `data/<date>.json` is brief output only — it contains **no keys**.
- The rendered `site/` is a CI build artifact (gitignored locally); it is uploaded
  to Pages, never committed.
