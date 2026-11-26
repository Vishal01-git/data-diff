"""Executive Results Dashboard View for Data Validator Streamlit UI."""

from pathlib import Path
import tempfile
import streamlit as st

from validator.orchestrator.runner import BatchValidationResult
from validator.reporting.exporters import (
    export_junit_xml,
    export_mismatches_csv,
    export_summary_json,
)
from validator.reporting.html_report import generate_html_report


def render_dashboard_view() -> None:
    """Render the Interactive Results & Executive KPI Dashboard."""
    st.header("📊 Executive Results & Audit Dashboard")
    st.caption("Live metrics, interactive tier drill-downs, and audit sign-off export tools.")

    result: BatchValidationResult = st.session_state.get("current_result")

    if not result:
        st.info("ℹ️ No validation results available yet. Run a reconciliation from the **🎯 Reconciliation Studio** tab to view live metrics.")
        return

    # Section 1: Executive KPI Cards
    kpi_col1, kpi_col2, kpi_col3, kpi_col4, kpi_col5, kpi_col6 = st.columns(6)
    kpi_col1.metric("Total Tables", result.total_tables)
    kpi_col2.metric("Passed", result.passed_tables, delta="Matched" if result.passed_tables > 0 else None)
    kpi_col3.metric(
        "Failed",
        result.failed_tables,
        delta=f"-{result.failed_tables}" if result.failed_tables > 0 else None,
        delta_color="inverse",
    )
    kpi_col4.metric("Total Rows", f"{result.total_rows_compared:,}")
    kpi_col5.metric("Duration", f"{result.total_duration_seconds:.2f}s")
    kpi_col6.metric("Bytes Scanned", f"{result.total_bytes_scanned:,}")

    st.divider()

    # Section 2: Filterable Table Overview
    st.subheader("📋 Table Reconciliation Results Matrix")

    col_search, col_filter = st.columns([3, 1])
    search_q = col_search.text_input("🔍 Search Table Name", placeholder="Filter tables by name...").lower()
    status_filter = col_filter.selectbox("Filter Status", ["ALL", "PASSED", "FAILED", "SKIPPED"])

    filtered_tables = [
        t
        for t in result.table_results
        if (not search_q or search_q in t.table_name.lower())
        and (status_filter == "ALL" or t.status == status_filter)
    ]

    if not filtered_tables:
        st.info("No tables match the current filter criteria.")
    else:
        # Build summary display
        table_rows = []
        for t in filtered_tables:
            src_cnt = f"{t.count_result.source_total_count:,}" if t.count_result else "-"
            tgt_cnt = f"{t.count_result.target_total_count:,}" if t.count_result else "-"
            c_diff = f"{t.count_result.count_diff_pct:.2f}%" if t.count_result else "-"
            data_mismatches = str(t.data_result.differing_rows_count) if t.data_result else "-"
            table_rows.append(
                {
                    "Table Name": t.table_name,
                    "Status": "🟢 PASSED" if t.status == "PASSED" else ("⚪ SKIPPED" if t.status == "SKIPPED" else "🔴 FAILED"),
                    "Source Rows": src_cnt,
                    "Target Rows": tgt_cnt,
                    "Count Delta %": c_diff,
                    "Data Mismatches": data_mismatches,
                    "Duration": f"{t.execution_time_seconds:.2f}s",
                }
            )

        st.dataframe(table_rows, use_container_width=True)

    st.divider()

    # Section 3: Deep-Dive Table Inspector
    st.subheader("🔍 Deep-Dive Table Inspector")
    table_names = [t.table_name for t in result.table_results]

    if table_names:
        selected_tbl_name = st.selectbox("Select Table to Inspect", table_names)
        tbl_match = next((t for t in result.table_results if t.table_name == selected_tbl_name), None)

        if tbl_match:
            st.write(
                f"**Table:** `{tbl_match.table_name}` | "
                f"**Source:** `{tbl_match.source_table}` &rarr; **Target:** `{tbl_match.target_table}` | "
                f"**Status:** `{'🟢 PASSED' if tbl_match.passed else '🔴 FAILED'}` | "
                f"**Elapsed:** `{tbl_match.execution_time_seconds:.2f}s`"
            )

            if tbl_match.error_message:
                st.error(f"**Execution Error:** {tbl_match.error_message}")

            t1, t2, t3 = st.tabs(["Tier 1: Count & Volume", "Tier 2: Schema Parity", "Tier 3: Row Data Diff"])

            with t1:
                if tbl_match.count_result:
                    c_res = tbl_match.count_result
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Source Count", f"{c_res.source_total_count:,}")
                    c2.metric("Target Count", f"{c_res.target_total_count:,}")
                    c3.metric("Count Delta", f"{c_res.count_diff:+,}")
                    c4.metric("Delta %", f"{c_res.count_diff_pct:.2f}%")

                    if c_res.partition_diffs:
                        st.write("##### Partition Breakdown")
                        p_data = [
                            {
                                "Partition Key": p.partition_key,
                                "Source Count": p.source_count,
                                "Target Count": p.target_count,
                                "Delta": p.diff,
                                "Status": p.status,
                            }
                            for p in c_res.partition_diffs
                        ]
                        st.dataframe(p_data, use_container_width=True)
                else:
                    st.info("Tier 1 count validation was not run for this table.")

            with t2:
                if tbl_match.schema_result:
                    s_res = tbl_match.schema_result
                    st.write(f"**Schema Status:** `{'🟢 MATCH' if s_res.passed else '🔴 MISMATCH'}` | Mismatched Columns: `{s_res.type_mismatch_count}`")

                    if s_res.column_diffs:
                        s_data = [
                            {
                                "Column": c.column_name,
                                "Source Type": c.source_raw_type or "-",
                                "Target Type": c.target_raw_type or "-",
                                "Canonical Type": c.source_canonical_type or c.target_canonical_type,
                                "Diff Type": c.diff_type,
                                "Severity": c.severity,
                                "Message": c.message,
                            }
                            for c in s_res.column_diffs
                        ]
                        st.dataframe(s_data, use_container_width=True)
                else:
                    st.info("Tier 2 schema validation was not run for this table.")

            with t3:
                if tbl_match.data_result:
                    d_res = tbl_match.data_result
                    scope_badge = getattr(d_res, "match_scope", "exact")
                    extra_cnt = getattr(d_res, "extra_target_rows_count", 0)
                    st.write(
                        f"**Mode Used:** `{d_res.mode_used}` | "
                        f"**Match Scope:** `{scope_badge}` | "
                        f"**Source Rows Compared:** `{d_res.total_rows_compared:,}` | "
                        f"**Differing Rows:** `{'0' if d_res.passed else d_res.differing_rows_count}`"
                        + (f" | **Extra Target Rows (Allowed):** `{extra_cnt:,}`" if scope_badge == "source_in_target" else "")
                    )

                    if d_res.error:
                        st.error(f"❌ Tier 3 Execution Error: {d_res.error}")
                    elif d_res.mismatch_samples:
                        st.write(f"##### 🔍 Exact Row & Column Mismatches (Top {len(d_res.mismatch_samples)} Rows)")
                        m_rows = []
                        for m in d_res.mismatch_samples:
                            pk_display = ", ".join(f"{k}={v}" for k, v in m.key_values.items())
                            diff_badge = (
                                "🔴 MISSING IN TARGET"
                                if m.diff_type == "EXCLUSIVE_SOURCE"
                                else ("🟡 EXTRA IN TARGET" if m.diff_type == "EXCLUSIVE_TARGET" else "🟠 VALUE DIFF")
                            )
                            if m.column_diffs:
                                for col_name, (s_val, t_val) in m.column_diffs.items():
                                    m_rows.append(
                                        {
                                            "Diff Type": diff_badge,
                                            "Row Key / Sample": pk_display,
                                            "Column": col_name,
                                            "Source Value": str(s_val),
                                            "Target Value": str(t_val),
                                        }
                                    )
                            else:
                                m_rows.append(
                                    {
                                        "Diff Type": diff_badge,
                                        "Row Key / Sample": pk_display,
                                        "Column": "(Entire Row)",
                                        "Source Value": "Present" if m.diff_type == "EXCLUSIVE_SOURCE" else "Missing",
                                        "Target Value": "Missing" if m.diff_type == "EXCLUSIVE_SOURCE" else "Present",
                                    }
                                )
                        st.dataframe(m_rows, use_container_width=True)
                    elif d_res.passed:
                        if scope_badge == "source_in_target":
                            st.success(f"✅ Subset Verified! All {d_res.total_rows_compared:,} Source rows matched in Target.")
                        else:
                            st.success("✅ 100% Data Row Parity! All rows match.")
                    else:
                        st.warning(f"⚠️ Data mismatch detected ({d_res.differing_rows_count} differing rows/buckets; no row samples captured).")
                else:
                    st.info("Tier 3 data validation was skipped or not configured for this table.")

    # Optional Backend Execution Logs Expander
    last_logs = st.session_state.get("last_run_logs", [])
    if last_logs:
        with st.expander("📡 View Last Run Backend Execution Logs", expanded=False):
            st.code("\n".join(last_logs[-100:]), language="log")

    st.divider()

    # Section 4: Report Export Toolbar
    st.subheader("📥 Export & Download Audit Reports")
    col_dl1, col_dl2, col_dl3, col_dl4 = st.columns(4)

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_p = Path(tmp_dir)

        # HTML
        html_p = generate_html_report(result, tmp_p / "sit_report.html")
        html_bytes = html_p.read_bytes()
        col_dl1.download_button(
            label="🌐 Download HTML Report",
            data=html_bytes,
            file_name="sit_report.html",
            mime="text/html",
            use_container_width=True,
        )

        # CSV
        csv_p = export_mismatches_csv(result, tmp_p / "mismatches.csv")
        csv_bytes = csv_p.read_bytes()
        col_dl2.download_button(
            label="📄 Download CSV Mismatches",
            data=csv_bytes,
            file_name="mismatches.csv",
            mime="text/csv",
            use_container_width=True,
        )

        # JSON
        json_p = export_summary_json(result, tmp_p / "summary.json")
        json_bytes = json_p.read_bytes()
        col_dl3.download_button(
            label="📦 Download Summary JSON",
            data=json_bytes,
            file_name="summary.json",
            mime="application/json",
            use_container_width=True,
        )

        # JUnit XML
        xml_p = export_junit_xml(result, tmp_p / "junit.xml")
        xml_bytes = xml_p.read_bytes()
        col_dl4.download_button(
            label="📊 Download JUnit XML",
            data=xml_bytes,
            file_name="junit.xml",
            mime="application/xml",
            use_container_width=True,
        )
