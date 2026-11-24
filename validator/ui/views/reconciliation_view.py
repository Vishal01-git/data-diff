"""Reconciliation Studio View for Data Validator Streamlit UI."""

import time
import streamlit as st

from validator.config.manifest import (
    DataDiffConfig,
    DefaultsConfig,
    FailFastConfig,
    ReportingConfig,
    SITManifest,
    TableConfig,
    TableEndpointConfig,
)
from validator.orchestrator.runner import BatchRunner
from validator.ui.state import (
    add_table_to_queue,
    clear_table_queue,
    get_connections,
    remove_table_from_queue,
)


def render_reconciliation_view() -> None:
    """Render the Table Reconciliation Configuration and Execution Studio."""
    st.header("🎯 Table Reconciliation Studio")
    st.caption("Select source and target tables using simple dropdowns, build a validation batch, and run parity checks.")

    connections = get_connections()
    if not connections:
        st.warning("⚠️ No database connections found. Please go to the **🔌 Connections** tab first to add your source and target connections.")
        return

    conn_names = list(connections.keys())

    with st.expander("➕ Configure a New Table for Reconciliation", expanded=True):
        col_src, col_tgt = st.columns(2, gap="medium")

        with col_src:
            st.subheader("Source Endpoint")
            src_conn = st.selectbox("Source Connection", conn_names, key="src_conn_select")
            src_table = st.text_input("Source Table Name", placeholder="e.g. sales_db.orders or orders", key="src_tbl_input")
            src_where = st.text_input("Source WHERE Filter (Optional)", placeholder="e.g. dt >= '2026-10-01'", key="src_where_input")

        with col_tgt:
            st.subheader("Target Endpoint")
            # Default target connection to second connection if available, else first
            default_tgt_idx = 1 if len(conn_names) > 1 else 0
            tgt_conn = st.selectbox("Target Connection", conn_names, index=default_tgt_idx, key="tgt_conn_select")
            tgt_table = st.text_input("Target Table Name", placeholder="e.g. curated_db.orders or orders", key="tgt_tbl_input")
            tgt_where = st.text_input("Target WHERE Filter (Optional)", placeholder="e.g. dt >= '2026-10-01'", key="tgt_where_input")

        st.divider()

        col_keys, col_opts = st.columns([1.2, 1], gap="medium")
        with col_keys:
            st.subheader("Keys & Partitions")
            pk_input = st.text_input(
                "Primary Key Column(s) (Comma-separated)",
                placeholder="e.g. id, order_id, order_item_id",
                help="Columns that uniquely identify each row. Leave empty for keyless frequency comparison.",
            )
            part_col = st.text_input(
                "Partition Column (Optional)",
                placeholder="e.g. dt or date_key",
                help="Breaks down Tier 1 counts per partition slice.",
            )
            table_task_name = st.text_input(
                "Task / Alias Name",
                value=f"{src_table}_vs_{tgt_table}" if src_table and tgt_table else "reconciliation_task",
            )

        with col_opts:
            st.subheader("Validation Tiers & Mode")
            tiers = st.multiselect(
                "Active Tiers",
                options=["count", "schema", "data"],
                default=["count", "schema", "data"],
                help="Tier 1: Count & Volume, Tier 2: Schema Types, Tier 3: Row-level Data Checksums.",
            )
            diff_mode = st.selectbox(
                "Data Diff Mode",
                options=["bucket_hash", "auto", "hashdiff", "joindiff"],
                index=0,
                help="bucket_hash groups rows into checksum buckets, taking only 2 queries to verify matching tables.",
            )

        if st.button("➕ Add Table to Validation Batch", type="secondary", use_container_width=True):
            if not src_table.strip() or not tgt_table.strip():
                st.error("Please provide both Source Table and Target Table names.")
            else:
                pks = [k.strip() for k in pk_input.split(",") if k.strip()]
                t_cfg = TableConfig(
                    name=table_task_name.strip(),
                    source=TableEndpointConfig(
                        connection=src_conn,
                        table=src_table.strip(),
                        where=src_where.strip() or None,
                    ),
                    target=TableEndpointConfig(
                        connection=tgt_conn,
                        table=tgt_table.strip(),
                        where=tgt_where.strip() or None,
                    ),
                    primary_keys=pks,
                    partition_column=part_col.strip() or None,
                    tiers=tiers,
                    data_diff=DataDiffConfig(mode=diff_mode),
                )
                add_table_to_queue(t_cfg)
                st.success(f"Added '{t_cfg.name}' to validation queue!")
                st.rerun()

    # Section 2: Validation Batch Queue
    st.subheader("📋 Queued Tables for Validation Batch")
    queue = st.session_state.get("table_queue", [])

    if not queue:
        st.info("No tables in the validation queue. Configure a table above and click **Add Table to Validation Batch**.")
    else:
        st.write(f"**Total Tables in Batch:** `{len(queue)}`")

        for idx, t in enumerate(queue):
            col_info, col_del = st.columns([5, 1])
            with col_info:
                pks_str = ", ".join(t.primary_keys) if t.primary_keys else "keyless"
                st.write(
                    f"**{idx + 1}. {t.name}** — `{t.source.connection}:{t.source.table}` &rarr; `{t.target.connection}:{t.target.table}` "
                    f"| PKs: `[{pks_str}]` | Tiers: `{t.tiers}`"
                )
            with col_del:
                if st.button("Remove", key=f"rm_queue_{idx}", type="secondary"):
                    remove_table_from_queue(idx)
                    st.rerun()

        col_act1, col_act2 = st.columns([1, 4])
        with col_act1:
            if st.button("Clear Queue", type="secondary", use_container_width=True):
                clear_table_queue()
                st.rerun()

        with col_act2:
            if st.button("🚀 Run Batch Validation Now", type="primary", use_container_width=True):
                manifest = SITManifest(
                    version="1.0",
                    run_name=f"ui_run_{int(time.time())}",
                    connections=connections,
                    defaults=DefaultsConfig(
                        tiers=["count", "schema", "data"],
                        fail_fast=FailFastConfig(),
                        data_diff=DataDiffConfig(),
                        reporting=ReportingConfig(output_dir="./reports"),
                    ),
                    tables=queue,
                )

                with st.status("⚡ Executing validation across tables...", expanded=True) as status:
                    st.write("Initializing database connection pools and worker threads...")
                    runner = BatchRunner(manifest=manifest, max_workers=4)
                    result = runner.run()
                    st.session_state["current_result"] = result
                    status.update(
                        label=f"Validation Complete! Status: {result.overall_status}",
                        state="complete" if result.overall_status == "PASSED" else "error",
                    )

                if result.overall_status == "PASSED":
                    st.balloons()
                    st.success(f"🎉 Parity Verified! All {result.total_tables} tables passed with 0 mismatches.")
                else:
                    st.error(f"⚠️ Validation finished with mismatches: {result.failed_tables}/{result.total_tables} tables failed.")

                st.info("👉 Switch to the **📊 Results Dashboard** tab in the sidebar to inspect full details, charts, and download reports.")
