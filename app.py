"""
Maritime Tension Index — dashboard.

This app only READS data/mti_data.csv (rebuilt daily by GitHub Actions).
It never downloads GDELT files, so it loads in ~1 second.
"""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="Maritime Tension Index", page_icon="🌊", layout="wide")

BASE_YEAR = 2019

CHART_LAYOUT = dict(
    template="plotly_white",
    hovermode="x unified",
    font=dict(family="Arial", size=11),
    paper_bgcolor="white",
    plot_bgcolor="white",
    legend=dict(orientation="h", yanchor="bottom", y=1.02,
                xanchor="right", x=1, font=dict(size=10)),
    margin=dict(t=70, b=50, l=60, r=40),
)

# ── LOAD DATA ─────────────────────────────────────────────────────────────────

@st.cache_data(ttl=1800)
def load_data():
    df = pd.read_csv("data/mti_data.csv")
    df["date"] = pd.to_datetime(df["date"])
    return df

full = load_data()
if full.empty:
    st.error("No data found. Run `python update_data.py` first (or wait for the "
             "scheduled GitHub Action to populate data/mti_data.csv).")
    st.stop()

# ── SIDEBAR ───────────────────────────────────────────────────────────────────

st.sidebar.title("Maritime Tension Index")
st.sidebar.markdown(
    "**Region:** South China Sea / East China Sea / Taiwan Strait  \n"
    "**Data:** GDELT 1.0 daily event files  \n"
    "**Updates:** automatically, daily via GitHub Actions  \n"
    f"**Base year:** {BASE_YEAR} = 100"
)
st.sidebar.markdown("---")

min_date = full["date"].min().date()
max_date = full["date"].max().date()

picked = st.sidebar.date_input(
    "Date range", value=(min_date, max_date),
    min_value=min_date, max_value=max_date,
)
start, end = (picked if isinstance(picked, tuple) and len(picked) == 2
              else (min_date, max_date))

st.sidebar.markdown("---")
st.sidebar.markdown("**Component weights**")
w_weighted  = st.sidebar.slider("Severity-weighted intensity", 0.0, 1.0, 0.5, 0.05)
w_count     = st.sidebar.slider("Event-share intensity",       0.0, 1.0, 0.3, 0.05)
w_goldstein = st.sidebar.slider("Inverted Goldstein",          0.0, 1.0, 0.2, 0.05)

total_w = (w_weighted + w_count + w_goldstein) or 1.0
w_weighted, w_count, w_goldstein = (w / total_w for w in (w_weighted, w_count, w_goldstein))

# ── RECOMPUTE INDEX ON THE FULL SERIES ────────────────────────────────────────

full = full.copy()
full["MTI"] = (w_weighted * full["idx_weighted"]
               + w_count * full["idx_count"]
               + w_goldstein * full["idx_goldstein"])
full["MTI_smooth"] = full["MTI"].rolling(3, min_periods=1).mean()

p25, p75, p90 = (full["MTI"].quantile(q) for q in (0.25, 0.75, 0.90))

def categorise(v):
    if v < p25:  return "Low"
    if v < p75:  return "Medium"
    if v < p90:  return "High"
    return "Severe"

full["category"] = full["MTI"].apply(categorise)

df = full[(full["date"].dt.date >= start) & (full["date"].dt.date <= end)]
if df.empty:
    st.warning("No data in the selected range.")
    st.stop()

# ── HEADER METRICS ────────────────────────────────────────────────────────────

latest = df.iloc[-1]
prev   = df.iloc[-2] if len(df) > 1 else latest
delta  = latest["MTI_smooth"] - prev["MTI_smooth"]

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Latest MTI (3M avg)", f"{latest['MTI_smooth']:.1f}",
          f"{delta:+.1f} vs prior month", delta_color="inverse")
c2.metric("Tension category", latest["category"])
c3.metric("Series peak", f"{full['MTI'].max():.1f}",
          full.loc[full["MTI"].idxmax(), "date"].strftime("%b %Y"),
          delta_color="off")
c4.metric("Latest month events", f"{int(latest['conflict_events']):,}")
c5.metric("Data through", full["date"].max().strftime("%b %Y"))

if (pd.Timestamp.now() - full["date"].max()).days > 45:
    st.warning("Data looks stale (latest month is more than 45 days old). "
               "Check the GitHub Actions update workflow.")

st.markdown("---")

# ── CATEGORY SHADING HELPER ───────────────────────────────────────────────────

cat_colours = {
    "Low":    "rgba(46,204,113,0.12)",
    "Medium": "rgba(241,196,15,0.12)",
    "High":   "rgba(230,126,34,0.12)",
    "Severe": "rgba(192,57,43,0.12)",
}

def add_category_shading(fig):
    prev_cat, start_dt = None, None
    for _, row in df.iterrows():
        if row["category"] != prev_cat:
            if prev_cat is not None:
                fig.add_vrect(x0=start_dt, x1=row["date"],
                              fillcolor=cat_colours[prev_cat],
                              layer="below", line_width=0)
            prev_cat, start_dt = row["category"], row["date"]
    if prev_cat:
        fig.add_vrect(x0=start_dt, x1=df["date"].iloc[-1],
                      fillcolor=cat_colours[prev_cat],
                      layer="below", line_width=0)

# ── CHART 1: MTI ──────────────────────────────────────────────────────────────

fig1 = go.Figure()
add_category_shading(fig1)

fig1.add_trace(go.Scatter(
    x=df["date"], y=df["MTI"], mode="lines", name="MTI (monthly)",
    line=dict(color="rgba(192,57,43,0.2)", width=1),
    hovertemplate="MTI: %{y:.1f}<extra></extra>"))

fig1.add_trace(go.Scatter(
    x=df["date"], y=df["MTI_smooth"], mode="lines", name="MTI (3M avg)",
    line=dict(color="#C0392B", width=2.8),
    hovertemplate="MTI 3M: %{y:.1f}<extra></extra>"))

fig1.add_hline(y=100, line_dash="dash", line_color="rgba(100,100,100,0.5)", line_width=1.2)

for threshold, label, colour in [
    (p75, f"High >{p75:.0f}",   "rgba(230,126,34,0.7)"),
    (p90, f"Severe >{p90:.0f}", "rgba(192,57,43,0.7)"),
]:
    fig1.add_hline(y=threshold, line_dash="dot", line_color=colour, line_width=1.2,
                   annotation_text=label, annotation_font=dict(size=9, color=colour),
                   annotation_position="right")

fig1.update_layout(
    **CHART_LAYOUT,
    height=420,
    title=dict(text=f"Maritime Tension Index — SCS / ECS / Taiwan Strait ({BASE_YEAR}=100)",
               font=dict(size=13), x=0, xanchor="left"),
    yaxis_title=f"{BASE_YEAR}=100",
)

st.plotly_chart(fig1, width="stretch")

# ── CHART 2: COMPONENT BREAKDOWN ──────────────────────────────────────────────

fig2 = go.Figure()

for col_name, colour, label, w in [
    ("idx_weighted",  "#E67E22", "Severity-weighted", w_weighted),
    ("idx_count",     "#2471A3", "Event share",       w_count),
    ("idx_goldstein", "#1E8449", "Inverted Goldstein", w_goldstein),
]:
    fig2.add_trace(go.Scatter(
        x=df["date"], y=df[col_name].rolling(3, min_periods=1).mean(),
        mode="lines", name=f"{label} ({w:.2f}w)",
        line=dict(color=colour, width=1.8),
        hovertemplate=f"{label}: %{{y:.1f}}<extra></extra>"))

fig2.add_hline(y=100, line_dash="dash", line_color="rgba(100,100,100,0.5)", line_width=1.2)

fig2.update_layout(
    **CHART_LAYOUT,
    height=340,
    title=dict(text="Component breakdown (3M avg)",
               font=dict(size=13), x=0, xanchor="left"),
    yaxis_title=f"{BASE_YEAR}=100",
)

st.plotly_chart(fig2, width="stretch")

# ── CHART 3: CAMEO STACKED BAR ────────────────────────────────────────────────

cameo_colours = {"13": "#AED6F1", "14": "#5DADE2", "15": "#F9E79F",
                 "16": "#F0B27A", "17": "#E59866", "18": "#E74C3C", "19": "#922B21"}
cameo_labels  = {"13": "13: Threaten", "14": "14: Protest/Demand",
                 "15": "15: Force Posture", "16": "16: Reduce Relations",
                 "17": "17: Coerce", "18": "18: Assault", "19": "19: Fight"}

fig3 = go.Figure()

for code in ["13", "14", "15", "16", "17", "18", "19"]:
    if code in df.columns:
        fig3.add_trace(go.Bar(
            x=df["date"], y=df[code], name=cameo_labels[code],
            marker_color=cameo_colours[code],
            hovertemplate=f"{cameo_labels[code]}: %{{y}}<extra></extra>"))

fig3.update_layout(
    **CHART_LAYOUT,
    barmode="stack",
    height=320,
    title=dict(text="Monthly conflict events by CAMEO root code",
               font=dict(size=13), x=0, xanchor="left"),
    yaxis_title="Event count",
)

st.plotly_chart(fig3, width="stretch")

# ── DATA TABLE + METHODOLOGY ──────────────────────────────────────────────────

with st.expander("Underlying data"):
    st.dataframe(
        df[["date", "conflict_events", "weighted_events", "avg_goldstein",
            "events_per_100k", "MTI", "MTI_smooth", "category"]]
        .sort_values("date", ascending=False).reset_index(drop=True)
        .style.format({"MTI": "{:.1f}", "MTI_smooth": "{:.1f}",
                       "avg_goldstein": "{:.2f}", "events_per_100k": "{:.2f}",
                       "weighted_events": "{:.1f}"}),
        width="stretch",
    )

with st.expander("Methodology"):
    st.markdown(f"""
**Pipeline.** GDELT 1.0 daily event files are filtered to events where (a) the
CAMEO root code is 13–19 (threaten → fight), (b) one actor is China and the
other is a claimant or principal counterpart (TWN, JPN, VNM, PHL, USA, MYS,
IDN, BRN), and (c) the event's geocoded location name or source URL contains a
South China Sea / East China Sea / Taiwan Strait keyword. Near-duplicate rows
(same date, dyad, CAMEO code, location) are collapsed into single events.

**Volume normalisation.** GDELT's global coverage grows over time, so raw
counts drift upward independently of real-world tension. Both count components
are expressed as conflict events **per 100k total GDELT events** in the same
month before indexing — the same logic that leads Baker, Bloom & Davis (2016)
to scale newspaper EPU counts by total article volume. This also makes the
current partial month directly comparable to complete months.

**Components.** Severity-weighted intensity (CAMEO roots weighted 1.0→3.5),
event-share intensity, and the inverted mean Goldstein score, each indexed to
the {BASE_YEAR} monthly mean = 100, then combined with the sidebar weights.

**Categories.** Low/Medium/High/Severe are the 25th/75th/90th percentiles of
the full-history MTI — they do not move when you change the date range.

**Caveats.** GDELT measures *media-reported* events via machine coding: it
over-represents English-language and Western coverage, mass-reports salient
incidents, and miscodes some events. The index tracks the intensity of reported
coercive interaction, not an objective ground-truth count of incidents at sea.
""")

st.caption(
    "Source: GDELT 1.0 Event Files (data.gdeltproject.org). Updated daily via "
    f"GitHub Actions. Data through {max_date.strftime('%d %B %Y')}."
)
