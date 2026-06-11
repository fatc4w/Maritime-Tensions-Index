"""
Maritime Tension Index — incremental data updater.

Run daily by GitHub Actions (or manually). Behaviour:
  1. Reads data/daily_totals.csv to find the last GDELT day already processed.
  2. Downloads only the missing daily GDELT 1.0 export files (one ~10 MB zip per day).
  3. Filters to CHN-vs-counterpart conflict events (CAMEO root 13-19) geolocated
     to the South China Sea / East China Sea / Taiwan Strait.
  4. Appends filtered events to data/raw_events.parquet.
  5. Rebuilds the monthly index from the full raw store and writes data/mti_data.csv.

A normal daily run fetches 1 file and finishes in a few seconds.
A from-scratch backfill (2019 -> today) takes ~45-70 minutes; run it once.

Env overrides (mainly for testing):
  MTI_START = YYYY-MM-DD   override series start date (default 2019-01-01)
  MTI_END   = YYYY-MM-DD   override end date (default: yesterday UTC)
"""

import io
import os
import sys
import time
import zipfile
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests

# ── CONFIG ────────────────────────────────────────────────────────────────────

DATA_DIR     = "data"
RAW_PATH     = os.path.join(DATA_DIR, "raw_events.parquet")
TOTALS_PATH  = os.path.join(DATA_DIR, "daily_totals.csv")
OUTPUT_CSV   = os.path.join(DATA_DIR, "mti_data.csv")

START_DATE = date.fromisoformat(os.environ.get("MTI_START", "2019-01-01"))
BASE_YEAR  = 2019

# GDELT 1.0 daily file for day D is published the following morning (~06:00 ET).
# We therefore never request anything newer than yesterday (UTC).
TODAY_UTC = datetime.now(timezone.utc).date()
END_DATE  = date.fromisoformat(os.environ["MTI_END"]) if "MTI_END" in os.environ \
            else TODAY_UTC - timedelta(days=1)

# ── GDELT 1.0 COLUMN MAP (verified against live files) ───────────────────────
# 1  SQLDATE              7  Actor1CountryCode    17 Actor2CountryCode
# 26 EventCode            28 EventRootCode        30 GoldsteinScale
# 31 NumMentions          50 ActionGeo_FullName   53 ActionGeo_Lat
# 54 ActionGeo_Long       57 SOURCEURL
USECOLS = {
    1:  "sqldate",
    7:  "a1_country",
    17: "a2_country",
    26: "event_code",
    28: "root_code",
    30: "goldstein",
    31: "num_mentions",
    50: "action_geo",
    53: "action_lat",
    54: "action_long",
    57: "source_url",
}

CONFLICT_CODES = {"13", "14", "15", "16", "17", "18", "19"}

# One side must be China; the other side must be a claimant / principal actor.
COUNTERPARTS = {"TWN", "JPN", "VNM", "PHL", "USA", "MYS", "IDN", "BRN"}

GEO_KEYWORDS = [
    "south china sea", "east china sea", "taiwan strait",
    "senkaku", "diaoyu", "spratly", "paracel", "pratas",
    "scarborough", "huangyan", "nine-dash", "west philippine",
    "second thomas", "ayungin", "sabina shoal", "whitsun",
    "natuna", "luconia", "vanguard bank", "reed bank",
]
GEO_PATTERN = "|".join(GEO_KEYWORDS)

SEVERITY = {
    "13": 1.0,   # Threaten
    "14": 1.2,   # Protest / demand
    "15": 1.5,   # Exhibit force posture
    "16": 1.8,   # Reduce relations
    "17": 2.2,   # Coerce
    "18": 2.8,   # Assault
    "19": 3.5,   # Fight
}

REQUEST_TIMEOUT = 90
MAX_RETRIES     = 3
SLEEP_EVERY     = 10      # polite pause every N downloads during backfill
SLEEP_SECONDS   = 0.5


# ── FETCH + FILTER ONE DAY ────────────────────────────────────────────────────

def fetch_day(d: date):
    """
    Returns (filtered_df, total_rows, status).
    status: "ok" | "missing" | "not_published"
    """
    url = f"http://data.gdeltproject.org/events/{d.strftime('%Y%m%d')}.export.CSV.zip"

    resp = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 404:
                # File for very recent days may simply not be published yet.
                recent = (TODAY_UTC - d).days <= 3
                return pd.DataFrame(), 0, ("not_published" if recent else "missing")
            resp.raise_for_status()
            break
        except requests.exceptions.RequestException as e:
            if attempt == MAX_RETRIES - 1:
                print(f"  ! {d} failed after {MAX_RETRIES} attempts: {e}", flush=True)
                return pd.DataFrame(), 0, "not_published"   # retry on next run
            time.sleep(2 ** attempt)

    try:
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            with zf.open(zf.namelist()[0]) as f:
                df = pd.read_csv(
                    f, sep="\t", header=None, names=list(range(58)),
                    usecols=list(USECOLS.keys()), dtype=str, on_bad_lines="skip",
                )
    except Exception as e:
        print(f"  ! {d} unreadable zip: {e}", flush=True)
        return pd.DataFrame(), 0, "missing"

    df = df.rename(columns=USECOLS)
    total_rows = len(df)   # denominator for volume normalisation

    # Filter 1: hostile CAMEO root codes
    df = df[df["root_code"].isin(CONFLICT_CODES)]
    if df.empty:
        return df, total_rows, "ok"

    # Filter 2: China on one side, a claimant / principal counterpart on the other
    chn = df["a1_country"].eq("CHN") | df["a2_country"].eq("CHN")
    cpt = df["a1_country"].isin(COUNTERPARTS) | df["a2_country"].isin(COUNTERPARTS)
    df = df[chn & cpt]
    if df.empty:
        return df, total_rows, "ok"

    # Filter 3: SCS / ECS / Taiwan Strait geography (ActionGeo name + source URL)
    geo_text = (df["action_geo"].fillna("") + " " + df["source_url"].fillna("")).str.lower()
    df = df[geo_text.str.contains(GEO_PATTERN, na=False, regex=True)].copy()
    if df.empty:
        return df, total_rows, "ok"

    # SQLDATE sanity: GDELT daily files contain re-reports of historical events
    # (anniversaries, retrospectives). Keep only events dated within 60 days of
    # the file date so old months are not retroactively polluted.
    df["sqldate_parsed"] = pd.to_datetime(df["sqldate"], format="%Y%m%d", errors="coerce")
    lo = pd.Timestamp(d - timedelta(days=60))
    hi = pd.Timestamp(d)
    df = df[(df["sqldate_parsed"] >= lo) & (df["sqldate_parsed"] <= hi)]
    df = df.drop(columns=["sqldate_parsed"])

    df["file_date"] = d.isoformat()
    return df, total_rows, "ok"


# ── LOAD EXISTING STATE ───────────────────────────────────────────────────────

os.makedirs(DATA_DIR, exist_ok=True)

if os.path.exists(TOTALS_PATH):
    totals = pd.read_csv(TOTALS_PATH, parse_dates=["date"])
    done = set(totals["date"].dt.date)
else:
    totals = pd.DataFrame(columns=["date", "total_events", "status"])
    done = set()

if os.path.exists(RAW_PATH):
    raw_store = pd.read_parquet(RAW_PATH)
else:
    raw_store = pd.DataFrame()

# ── INCREMENTAL FETCH LOOP ────────────────────────────────────────────────────

all_days  = [START_DATE + timedelta(days=i) for i in range((END_DATE - START_DATE).days + 1)]
remaining = [d for d in all_days if d not in done]

print(f"Series window : {START_DATE} -> {END_DATE}")
print(f"Already done  : {len(done):,} days")
print(f"To fetch      : {len(remaining):,} days "
      f"(~{len(remaining) * 1.5 / 60:.0f} min if backfilling)\n")

new_rows, new_totals = [], []

for i, d in enumerate(remaining):
    df_day, total_rows, status = fetch_day(d)

    if status == "not_published":
        print(f"  {d}: not published yet — stopping here, will retry next run.")
        break

    new_totals.append({"date": pd.Timestamp(d), "total_events": total_rows, "status": status})
    if not df_day.empty:
        new_rows.append(df_day)

    if len(remaining) > 5 and (i + 1) % 25 == 0:
        print(f"  ...{i + 1}/{len(remaining)} days "
              f"({sum(len(x) for x in new_rows):,} events captured)", flush=True)

    # Periodically checkpoint during long backfills so a crash loses little work
    if (i + 1) % 200 == 0:
        if new_rows:
            raw_store = pd.concat([raw_store] + new_rows, ignore_index=True)
            raw_store.to_parquet(RAW_PATH, index=False)
            new_rows = []
        totals = pd.concat([totals, pd.DataFrame(new_totals)], ignore_index=True)
        totals.to_csv(TOTALS_PATH, index=False)
        new_totals = []
        print(f"  [checkpoint saved at {d}]", flush=True)

    if (i + 1) % SLEEP_EVERY == 0:
        time.sleep(SLEEP_SECONDS)

# Flush remainder
if new_rows:
    raw_store = pd.concat([raw_store] + new_rows, ignore_index=True)
if new_totals:
    totals = pd.concat([totals, pd.DataFrame(new_totals)], ignore_index=True)

if raw_store.empty:
    print("No events in store yet — nothing to build. Exiting.")
    sys.exit(0)

# Deduplicate the raw store: GDELT emits many near-identical rows for the same
# incident across outlets. Collapse to one row per (event date, dyad, full CAMEO
# code, geocoded location), summing media mentions as a salience measure.
raw_store["goldstein"]    = pd.to_numeric(raw_store["goldstein"], errors="coerce")
raw_store["num_mentions"] = pd.to_numeric(raw_store["num_mentions"], errors="coerce").fillna(1)

raw_store = (
    raw_store
    .groupby(["sqldate", "a1_country", "a2_country", "event_code", "action_geo"],
             dropna=False, as_index=False)
    .agg(root_code=("root_code", "first"),
         goldstein=("goldstein", "mean"),
         num_mentions=("num_mentions", "sum"),
         action_lat=("action_lat", "first"),
         action_long=("action_long", "first"),
         source_url=("source_url", "first"),
         file_date=("file_date", "first"))
)

raw_store.to_parquet(RAW_PATH, index=False)
totals = totals.drop_duplicates(subset=["date"]).sort_values("date")
totals.to_csv(TOTALS_PATH, index=False)

print(f"\nRaw store: {len(raw_store):,} deduplicated events "
      f"| coverage: {len(totals):,} days")

# ── BUILD MONTHLY INDEX ───────────────────────────────────────────────────────

ev = raw_store.copy()
ev["date"]     = pd.to_datetime(ev["sqldate"], format="%Y%m%d", errors="coerce")
ev["severity"] = ev["root_code"].map(SEVERITY).fillna(1.0)
ev = ev.dropna(subset=["date", "goldstein"])

monthly = (
    ev.set_index("date").resample("MS")
    .agg(conflict_events=("root_code", "count"),
         weighted_events=("severity", "sum"),
         avg_goldstein=("goldstein", "mean"),
         total_mentions=("num_mentions", "sum"))
    .reset_index()
)

# Monthly GDELT volume denominator (controls for the steady growth in the number
# of sources GDELT monitors — raw counts drift upward even when tension doesn't)
totals["date"] = pd.to_datetime(totals["date"])
ok_totals = totals[totals["status"] == "ok"].copy()
vol = (ok_totals.set_index("date")["total_events"]
       .resample("MS").sum().rename("gdelt_total").reset_index())
monthly = monthly.merge(vol, on="date", how="left")
monthly = monthly[monthly["gdelt_total"] > 0]

# Volume-normalised intensities (per 100k global GDELT events). Also makes the
# current *partial* month comparable to complete months.
monthly["events_per_100k"]   = monthly["conflict_events"] / monthly["gdelt_total"] * 1e5
monthly["weighted_per_100k"] = monthly["weighted_events"] / monthly["gdelt_total"] * 1e5
monthly["goldstein_inv"]     = -monthly["avg_goldstein"]

# Index components, base year = 100
base_mask = monthly["date"].dt.year == BASE_YEAR
if base_mask.sum() == 0:
    print(f"WARNING: no data in base year {BASE_YEAR}; using full-series mean as base.")
    base_mask = pd.Series(True, index=monthly.index)

def to_index(series: pd.Series) -> pd.Series:
    base_val = series[base_mask].mean()
    if pd.isna(base_val) or base_val == 0:
        base_val = series.mean()
    return series / base_val * 100

monthly["idx_count"]     = to_index(monthly["events_per_100k"])
monthly["idx_weighted"]  = to_index(monthly["weighted_per_100k"])
monthly["idx_goldstein"] = to_index(monthly["goldstein_inv"])

monthly["MTI"] = (0.5 * monthly["idx_weighted"]
                  + 0.3 * monthly["idx_count"]
                  + 0.2 * monthly["idx_goldstein"])
monthly["MTI_smooth"] = monthly["MTI"].rolling(3, min_periods=1).mean()

# Categories from full-series percentiles (frozen here, not in the app, so the
# category of a given month never changes when the user filters dates)
p25, p75, p90 = (monthly["MTI"].quantile(q) for q in (0.25, 0.75, 0.90))

def categorise(v):
    if v < p25:  return "Low"
    if v < p75:  return "Medium"
    if v < p90:  return "High"
    return "Severe"

monthly["category"] = monthly["MTI"].apply(categorise)

# Monthly counts by CAMEO root code (for the stacked bar panel)
cameo = (ev.groupby([pd.Grouper(key="date", freq="MS"), "root_code"])
         .size().unstack(fill_value=0).reset_index())
cameo.columns.name = None
monthly = monthly.merge(cameo, on="date", how="left")
for code in CONFLICT_CODES:
    if code in monthly.columns:
        monthly[code] = monthly[code].fillna(0).astype(int)

monthly.to_csv(OUTPUT_CSV, index=False)

print(f"\nIndex rebuilt -> {OUTPUT_CSV}")
print(f"  Months          : {len(monthly)}")
print(f"  Latest month    : {monthly['date'].iloc[-1].strftime('%Y-%m')}")
print(f"  Latest MTI (3M) : {monthly['MTI_smooth'].iloc[-1]:.1f} "
      f"[{monthly['category'].iloc[-1]}]")
print(f"  Series peak     : {monthly['MTI'].max():.1f} "
      f"({monthly.loc[monthly['MTI'].idxmax(), 'date'].strftime('%b %Y')})")
print(f"  Thresholds      : Low<{p25:.0f} | Med {p25:.0f}-{p75:.0f} | "
      f"High {p75:.0f}-{p90:.0f} | Severe>{p90:.0f}")
