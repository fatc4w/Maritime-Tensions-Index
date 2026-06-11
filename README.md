# Maritime Tension Index (SCS / ECS / Taiwan Strait)

A self-updating dashboard tracking China-counterpart conflict events in the
South China Sea, East China Sea and Taiwan Strait, built on GDELT 1.0 daily
event files.

## How it works (and why it's fast)

```
GDELT daily files ──> update_data.py ──> data/raw_events.parquet   (event store)
      (only NEW days fetched)            data/daily_totals.csv     (progress + volume)
                                         data/mti_data.csv         (monthly index)
                                              │
GitHub Actions (free cron, daily) commits the updated data files
                                              │
Streamlit Community Cloud (free) auto-redeploys and serves app.py
```

The 55-minute problem is gone because **the dashboard never computes anything
from GDELT**. A scheduled GitHub Action fetches *only the days not yet in the
store* (normally 1 file, a few seconds), rebuilds the monthly index, and
commits the result. Opening the dashboard just reads one small CSV.

## Setup (one time, ~15 minutes, $0)

### 1. Create the repo
1. Create a **public** GitHub repository (public = unlimited free Actions
   minutes; private repos are capped at 2,000 min/month, which would still be
   fine for daily runs but tight for the backfill).
2. Push these files, preserving the layout:
   ```
   app.py
   update_data.py
   requirements.txt
   .github/workflows/update-data.yml
   data/            (empty for now — git needs at least one file; add data/.gitkeep)
   ```

### 2. Backfill 2019 → today (one time)
Option A — on your machine (recommended, you can watch it):
```bash
pip install pandas requests pyarrow
python update_data.py        # ~45-70 min for 2019 -> today
git add data/ && git commit -m "initial backfill" && git push
```
Option B — let GitHub do it: repo → **Actions** tab → "Update MTI data" →
**Run workflow**. The job checkpoints every 200 days, so even if it dies it
resumes where it stopped on the next run.

### 3. Turn on the schedule
Nothing to do — the cron in `.github/workflows/update-data.yml` runs daily at
13:15 UTC once the workflow file is on the default branch. Verify the first
scheduled run succeeds under the Actions tab.

> GitHub disables scheduled workflows on repos with **no activity for 60
> days**. The daily data commits count as activity, so this self-sustains; if
> you ever pause it for 2+ months, re-enable it from the Actions tab.

### 4. Deploy the dashboard
1. Go to https://share.streamlit.io and sign in with GitHub.
2. **New app** → pick your repo → main file `app.py` → Deploy.
3. Streamlit Community Cloud watches the repo: every data commit from the
   Action triggers a redeploy, so the dashboard is always current. The app
   also caches data for only 30 minutes, so even without a redeploy it picks
   up fresh data quickly.

That's it. Free tier limits that matter: Streamlit free apps sleep after ~12h
of no visitors and take ~30-60s to wake on the next visit (data is unaffected);
public-repo Actions minutes are unlimited.

## Updating things later

- **Add an incident annotation:** edit the `EVENTS` list in `app.py`, commit.
- **Change keywords/actors/severity weights:** edit the constants at the top of
  `update_data.py`. Note: changing *filters* only affects newly fetched days.
  To apply a filter change historically, delete `data/raw_events.parquet` and
  `data/daily_totals.csv` and re-run the backfill.
- **Force a refresh now:** Actions tab → Run workflow.

## Files

| File | Purpose |
|---|---|
| `update_data.py` | Incremental GDELT fetch + monthly index build |
| `app.py` | Streamlit dashboard (read-only, fast) |
| `data/raw_events.parquet` | Deduplicated filtered event store |
| `data/daily_totals.csv` | Per-day processing log + GDELT volume denominators |
| `data/mti_data.csv` | Final monthly index consumed by the app |
