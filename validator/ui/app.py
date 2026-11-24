"""Main entrypoint for the Data Validator Streamlit Web Interface."""

import streamlit as st

from validator.ui.state import get_connections, init_session_state
from validator.ui.views.connections_view import render_connections_view
from validator.ui.views.dashboard_view import render_dashboard_view
from validator.ui.views.history_view import render_history_view
from validator.ui.views.manifest_view import render_manifest_view
from validator.ui.views.reconciliation_view import render_reconciliation_view

# Configure Streamlit page layout and title
st.set_page_config(
    page_title="Data Validator Studio",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Initialize persistent session state
init_session_state()

# Inject subtle enterprise styling
st.markdown(
    """
    <style>
    .main-header {
        font-size: 1.8rem;
        font-weight: 700;
        color: #1E293B;
        margin-bottom: 0.2rem;
    }
    .metric-card {
        background-color: #F8FAFC;
        border: 1px solid #E2E8F0;
        border-radius: 8px;
        padding: 1rem;
        text-align: center;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# Sidebar Navigation
with st.sidebar:
    st.markdown("## 🛡️ **Data Validator**")
    st.caption("Enterprise Cross-Database Reconciliation")
    st.divider()

    nav_selection = st.radio(
        "Navigation",
        options=[
            "🔌 Connections",
            "🎯 Reconciliation Studio",
            "📊 Results Dashboard",
            "📑 Manifest Studio (YAML)",
            "📜 Run History",
        ],
        index=0,
    )

    st.divider()

    # Sidebar Live Status Card
    connections = get_connections()
    queue = st.session_state.get("table_queue", [])
    current_res = st.session_state.get("current_result")

    st.markdown("##### 📌 **Session Status**")
    st.write(f"- **Connections:** `{len(connections)}` configured")
    st.write(f"- **Batch Queue:** `{len(queue)}` table(s)")
    if current_res:
        st_color = "🟢" if current_res.overall_status == "PASSED" else "🔴"
        st.write(f"- **Last Run:** {st_color} `{current_res.overall_status}` ({current_res.total_tables} tbls)")

# Main Display Routing
if nav_selection == "🔌 Connections":
    render_connections_view()
elif nav_selection == "🎯 Reconciliation Studio":
    render_reconciliation_view()
elif nav_selection == "📊 Results Dashboard":
    render_dashboard_view()
elif nav_selection == "📑 Manifest Studio (YAML)":
    render_manifest_view()
elif nav_selection == "📜 Run History":
    render_history_view()
