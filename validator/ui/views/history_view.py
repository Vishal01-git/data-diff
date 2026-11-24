"""History and Checkpoint View for Data Validator Streamlit UI."""

from pathlib import Path
import sqlite3
import streamlit as st


def render_history_view() -> None:
    """Render the Run History & Checkpoint Inspector page."""
    st.header("📜 Run History & Checkpoints")
    st.caption("Inspect previous runs and resume interrupted batches.")

    state_dir = Path(".validator_state")
    if not state_dir.exists():
        st.info("No run history or checkpoint databases found yet.")
        return

    db_files = list(state_dir.glob("*.db"))
    if not db_files:
        st.info("No checkpoint runs recorded in `.validator_state/`.")
        return

    st.write(f"Found **{len(db_files)}** historical run checkpoint(s):")

    selected_db = st.selectbox(
        "Select Checkpoint Run",
        options=[f.stem for f in db_files],
    )

    db_path = state_dir / f"{selected_db}.db"

    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT table_name, status, passed, updated_at FROM checkpoints ORDER BY updated_at DESC")
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            st.info("No checkpoint records found in this run database.")
        else:
            table_data = [
                {
                    "Table Name": r[0],
                    "Status": "🟢 PASSED" if r[1] == "PASSED" else ("⚪ SKIPPED" if r[1] == "SKIPPED" else "🔴 FAILED"),
                    "Passed": bool(r[2]),
                    "Updated At (UTC)": r[3],
                }
                for r in rows
            ]
            st.dataframe(table_data, use_container_width=True)

    except Exception as e:
        st.error(f"Error reading checkpoint database: {e}")
