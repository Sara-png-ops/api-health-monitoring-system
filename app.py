"""Streamlit dashboard for the API Health Monitoring System.

Checks run only when the user presses "Run checks now". There is no
background monitoring.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from html import escape

import pandas as pd
import plotly.express as px
import streamlit as st

from api_monitor import (
    DEFAULT_TIMEOUT_SECONDS,
    ERROR,
    HEALTHY,
    RESULT_LABELS,
    SAMPLE_ENDPOINTS,
    UNHEALTHY,
    InvalidURLError,
    build_csv_report,
    check_endpoints,
    default_endpoint_name,
    format_timestamp,
    summarize_latest,
    validate_url,
)
from database import DEFAULT_DB_PATH, Database, DuplicateEndpointError

HISTORY_LIMIT = 1000
STATUS_COLORS = {"Healthy": "#2E8B63", "Unhealthy": "#D1783A", "Error": "#B8425A"}
LINE_COLORS = ["#6B4FBB", "#2E8B63", "#D1783A", "#3E7CB1", "#B8425A", "#8A7BC8", "#5C8A8A"]
ALL_ENDPOINTS = "All endpoints"

st.set_page_config(page_title="API Health Monitoring System", page_icon=":material/monitor_heart:", layout="wide")

STYLES = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;700;800&display=swap');
html, body, .stApp, [data-testid="stMarkdownContainer"], button, input, textarea {
    font-family: 'Manrope', -apple-system, 'Segoe UI', Roboto, sans-serif;
}
.block-container { padding-top: 2rem; max-width: 1200px; }
.page-title { font-size: 2rem; font-weight: 800; color: #2A2440; margin: 0 0 .2rem 0; letter-spacing: -0.02em; }
.page-subtitle { color: #6A6285; margin: 0 0 1rem 0; max-width: 48rem; }
.notice { background: #FFFFFF; border: 1px solid #E3DAF5; border-left: 4px solid #6B4FBB;
          border-radius: 8px; padding: .7rem 1rem; color: #4A4264; font-size: .93rem; margin-bottom: 1.2rem; }
.section-title { font-size: 1.2rem; font-weight: 700; color: #2A2440; margin: 1.6rem 0 .6rem 0; }
.card { background: #FFFFFF; border: 1px solid #E3DAF5; border-radius: 12px; padding: 1rem 1.2rem; }
.card.healthy { border-top: 4px solid #2E8B63; }
.card.unhealthy { border-top: 4px solid #D1783A; }
.card.error { border-top: 4px solid #B8425A; }
.card.neutral { border-top: 4px solid #6B4FBB; }
.metric-label { color: #6A6285; font-size: .88rem; margin-bottom: .15rem; }
.metric-value { color: #2A2440; font-size: 1.8rem; font-weight: 800; line-height: 1.15; }
[data-testid="stSidebar"] { border-right: 1px solid #E3DAF5; }
.stButton > button, [data-testid="stFormSubmitButton"] button, [data-testid="stDownloadButton"] button {
    border-radius: 8px; font-weight: 600;
}
.stButton > button[kind="primary"], [data-testid="stFormSubmitButton"] button[kind="primary"] {
    background: #6B4FBB; border: 0; color: #FFFFFF;
}
[data-testid="stForm"] { background: #FFFFFF; border: 1px solid #E3DAF5; border-radius: 12px; }
</style>
"""


# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #
@st.cache_resource
def get_database() -> Database:
    return Database(os.environ.get("API_MONITOR_DB", DEFAULT_DB_PATH))


def flash(kind: str, message: str) -> None:
    """Remember a message across the rerun that follows a database change."""
    st.session_state["flash"] = (kind, message)


def show_flash() -> None:
    kind, message = st.session_state.pop("flash", (None, None))
    if kind == "success":
        st.success(message)
    elif kind == "warning":
        st.warning(message)
    elif kind == "error":
        st.error(message)


def metric_card(label: str, value: str, tone: str = "neutral") -> str:
    return (
        f'<div class="card {tone}"><div class="metric-label">{escape(label)}</div>'
        f'<div class="metric-value">{escape(value)}</div></div>'
    )


def style_figure(fig) -> None:
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="#FFFFFF", font=dict(color="#2A2440"),
        margin=dict(l=10, r=10, t=30, b=10), legend_title_text="", colorway=LINE_COLORS,
    )
    fig.update_xaxes(gridcolor="#EEE8FA", linecolor="#D8CEF0")
    fig.update_yaxes(gridcolor="#EEE8FA", linecolor="#D8CEF0")


# --------------------------------------------------------------------------- #
# Sidebar: endpoint management
# --------------------------------------------------------------------------- #
def remove_endpoint(db: Database, endpoint_id: int, name: str) -> None:
    """Button callback: runs before the widgets are rebuilt, so it may reset their state."""
    db.remove_endpoint(endpoint_id)
    st.session_state["remove_confirm"] = False
    flash("success", f"Removed {name} and its history.")


def render_sidebar(db: Database) -> None:
    form_version = st.session_state.setdefault("form_version", 0)

    with st.sidebar:
        st.markdown("### Add an endpoint")
        with st.form("add_endpoint_form"):
            name = st.text_input("Name (optional)", max_chars=60, key=f"name_{form_version}")
            url = st.text_input("Endpoint URL", placeholder="https://api.example.com/health",
                                key=f"url_{form_version}")
            submitted = st.form_submit_button("Add endpoint", type="primary")
        if submitted:
            try:
                clean_url = validate_url(url)
                db.add_endpoint(name.strip() or default_endpoint_name(clean_url), clean_url)
            except InvalidURLError as exc:
                st.error(str(exc))
            except DuplicateEndpointError as exc:
                st.warning(str(exc))
            else:
                st.session_state["form_version"] += 1
                flash("success", f"Added {clean_url}. Press Run checks now to check it.")
                st.rerun()

        with st.expander("Sample public endpoints"):
            st.caption(
                "Third-party public APIs for trying the tool. The last one always returns "
                "HTTP 500 on purpose, so you can see an unhealthy result."
            )
            for sample_name, sample_url in SAMPLE_ENDPOINTS:
                st.markdown(f"**{sample_name}**  \n`{sample_url}`")
            if st.button("Add all sample endpoints"):
                added = 0
                for sample_name, sample_url in SAMPLE_ENDPOINTS:
                    try:
                        db.add_endpoint(sample_name, sample_url)
                        added += 1
                    except DuplicateEndpointError:
                        pass
                flash("success" if added else "warning",
                      f"Added {added} sample endpoint(s)." if added else "All sample endpoints are already added.")
                st.rerun()

        endpoints = db.list_endpoints()
        if endpoints:
            st.markdown("### Remove an endpoint")
            by_id = {e["id"]: e for e in endpoints}
            chosen = st.selectbox("Endpoint", list(by_id), format_func=lambda i: by_id[i]["name"],
                                  key="remove_choice")
            confirmed = st.checkbox("Also delete its check history", key="remove_confirm")
            st.button("Remove endpoint", disabled=not confirmed, on_click=remove_endpoint,
                      args=(db, chosen, by_id[chosen]["name"]))


# --------------------------------------------------------------------------- #
# Main page
# --------------------------------------------------------------------------- #
def run_checks(db: Database, endpoints: list[dict], selected_ids: list[int]) -> None:
    targets = [e for e in endpoints if e["id"] in selected_ids]
    with st.spinner(f"Checking {len(targets)} endpoint(s)..."):
        results = check_endpoints([e["url"] for e in targets])
    for endpoint, result in zip(targets, results):
        db.save_check(endpoint["id"], result.checked_at, result.status_code,
                      result.response_time_ms, result.result, result.error_message)
    st.success(f"Checked {len(targets)} endpoint(s) at {datetime.now(timezone.utc):%H:%M:%S} UTC. "
               "Results were saved to the history.")


def render_overview(db: Database) -> None:
    latest = db.get_latest_checks()
    counts = summarize_latest(latest)

    cards = st.columns(4)
    cards[0].markdown(metric_card("Endpoints checked", f"{counts['checked']} of {counts['registered']}"),
                      unsafe_allow_html=True)
    cards[1].markdown(metric_card("Healthy", str(counts[HEALTHY]), "healthy"), unsafe_allow_html=True)
    cards[2].markdown(metric_card("Unhealthy", str(counts[UNHEALTHY]), "unhealthy"), unsafe_allow_html=True)
    cards[3].markdown(metric_card("Errors", str(counts[ERROR]), "error"), unsafe_allow_html=True)
    st.caption("Counts use each endpoint's most recent check. Unhealthy means an HTTP 4xx or 5xx response. "
               "Error means no response was received (timeout, connection failure or invalid URL).")

    table = pd.DataFrame(
        {
            "Name": [r["name"] for r in latest],
            "URL": [r["url"] for r in latest],
            "Status": [RESULT_LABELS.get(r["result"], "Not checked yet") for r in latest],
            "HTTP status": [r["status_code"] for r in latest],
            "Response time (ms)": [r["response_time_ms"] for r in latest],
            "Last checked (UTC)": [format_timestamp(r["checked_at"]) for r in latest],
            "Detail": [r["error_message"] or "" for r in latest],
        }
    )

    st.markdown('<div class="section-title">Endpoint status overview</div>', unsafe_allow_html=True)
    st.dataframe(
        table, hide_index=True,
        column_config={
            "HTTP status": st.column_config.NumberColumn(format="%d"),
            "Response time (ms)": st.column_config.NumberColumn(format="%.0f"),
        },
    )

    st.markdown('<div class="section-title">Latest response times</div>', unsafe_allow_html=True)
    timed = table[table["Response time (ms)"].notna()]
    if timed.empty:
        st.info("No response times yet. Press Run checks now to measure them.")
        return
    fig = px.bar(timed, x="Name", y="Response time (ms)", color="Status", color_discrete_map=STATUS_COLORS,
                 hover_data=["HTTP status", "Last checked (UTC)"])
    fig.update_layout(xaxis_title=None, yaxis_title="Response time (ms)")
    style_figure(fig)
    st.plotly_chart(fig, config={"displayModeBar": False})
    st.caption("Endpoints that returned no response have no response time and are not shown.")


def render_history(db: Database, endpoints: list[dict]) -> None:
    by_id = {e["id"]: e for e in endpoints}
    choice = st.selectbox("Filter by endpoint", [None] + list(by_id), key="history_filter",
                          format_func=lambda i: ALL_ENDPOINTS if i is None else f"{by_id[i]['name']}  ({by_id[i]['url']})")
    rows = db.get_history(endpoint_id=choice, limit=HISTORY_LIMIT)

    if not rows:
        st.info("No checks have been recorded yet. Press Run checks now to create the first entries.")
        return

    frame = pd.DataFrame(rows)
    frame["Status"] = frame["result"].map(RESULT_LABELS)
    frame["Checked at (UTC)"] = frame["checked_at"].map(format_timestamp)

    st.markdown('<div class="section-title">Response-time trend</div>', unsafe_allow_html=True)
    timed = frame[frame["response_time_ms"].notna()].copy()
    if timed.empty:
        st.info("None of the recorded checks received a response, so there are no response times to plot.")
    else:
        timed["Checked at"] = pd.to_datetime(timed["checked_at"])
        timed = timed.sort_values("Checked at")
        fig = px.line(timed, x="Checked at", y="response_time_ms", color="name", markers=True,
                      labels={"response_time_ms": "Response time (ms)", "name": "Endpoint"})
        fig.update_layout(xaxis_title="Time (UTC)")
        style_figure(fig)
        st.plotly_chart(fig, config={"displayModeBar": False})
        st.caption("Each point is one recorded check. A single point appears as a marker only. "
                   "Checks without a response are not plotted.")

    st.markdown('<div class="section-title">Check history</div>', unsafe_allow_html=True)
    shown = frame.rename(columns={"name": "Name", "url": "URL", "status_code": "HTTP status",
                                  "response_time_ms": "Response time (ms)", "error_message": "Detail"})
    shown["Detail"] = shown["Detail"].fillna("")
    st.dataframe(
        shown[["Checked at (UTC)", "Name", "URL", "Status", "HTTP status", "Response time (ms)", "Detail"]],
        hide_index=True,
        column_config={
            "HTTP status": st.column_config.NumberColumn(format="%d"),
            "Response time (ms)": st.column_config.NumberColumn(format="%.0f"),
        },
    )
    suffix = f" Only the {HISTORY_LIMIT:,} most recent checks are loaded." if len(rows) >= HISTORY_LIMIT else ""
    st.caption(f"{len(rows):,} check(s) shown, newest first.{suffix}")

    st.download_button(
        "Download CSV report",
        data=build_csv_report(rows),
        file_name=f"api-health-report-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.csv",
        mime="text/csv",
    )


def main() -> None:
    st.markdown(STYLES, unsafe_allow_html=True)
    db = get_database()
    render_sidebar(db)

    st.markdown('<div class="page-title">API Health Monitoring System</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="page-subtitle">Check whether your API endpoints respond, how fast they answer, '
        "and how that changes over time.</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="notice">Checks run only when you press <b>Run checks now</b>. '
        "This app does not monitor in the background, so the history contains only the checks you started. "
        f"Each request times out after {DEFAULT_TIMEOUT_SECONDS} seconds.</div>",
        unsafe_allow_html=True,
    )
    show_flash()

    endpoints = db.list_endpoints()
    if not endpoints:
        st.info("No endpoints yet. Add a URL in the sidebar, or open Sample public endpoints and add them.")
        return

    by_id = {e["id"]: e for e in endpoints}
    selected = st.multiselect("Endpoints to check", list(by_id), default=list(by_id),
                              format_func=lambda i: by_id[i]["name"])
    if st.button("Run checks now", type="primary", disabled=not selected):
        run_checks(db, endpoints, selected)

    overview_tab, history_tab = st.tabs(["Overview", "History and report"])
    with overview_tab:
        render_overview(db)
    with history_tab:
        render_history(db, endpoints)


main()
