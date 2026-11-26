"""Reconciliation Studio View for Data Validator Streamlit UI."""

import csv
import io
import time
import streamlit as st

from validator.config.connections import ConnectionResolver
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
from validator.ui.introspect import get_columns_and_types, get_schemas, get_tables
from validator.ui.log_stream import run_batch_with_live_logs
from validator.ui.state import (
    add_table_to_queue,
    clear_table_queue,
    get_connections,
    remove_table_from_queue,
)
from validator.ui.storage import (
    get_session_reports_dir,
    load_saved_presets,
    save_preset_to_disk,
)


def render_reconciliation_view() -> None:
    """Render the Table Reconciliation Configuration and Execution Studio."""
    st.header("🎯 Table Reconciliation Studio")
    st.caption("Configure single tables or bulk-load 50+ tables, choose your comparison strategy, and watch live execution.")

    connections = get_connections()
    if not connections:
        st.warning("⚠️ No database connections found. Please go to the **🔌 Connections** tab first to add your source and target connections.")
        return

    conn_names = list(connections.keys())

    # --------------------------------------------------------------------------
    # Section 1: Plain-English Comparison Strategy & Algorithm Guide
    # --------------------------------------------------------------------------
    with st.expander("🧭 Comparison Strategy & Algorithm Guide (When to Use What)", expanded=False):
        st.markdown(
            """
| Use Case | Recommended Setting | How It Works |
| :--- | :--- | :--- |
| **Table has Primary Key(s) & should match 100%** | **Scope:** `Exact Parity` <br>**Algo:** `bucket_hash` | Groups rows into 64 checksum buckets in **2 queries** total. Only fetches rows from differing buckets. Ideal for **Athena / MaxCompute / large tables**. |
| **Source has 100 rows, Target has 200 rows** (Verify all 100 Source rows exist in Target) | **Scope:** `Subset (Source in Target)` <br>**Algo:** `bucket_hash` or `keyless` | Passes if **100% of Source rows match in Target**, while ignoring extra historical/appended rows in Target. |
| **Table has NO Primary Key** | **Algo:** `keyless` | Computes a full-row hash for every row, compares frequency counts per hash bucket, and surfaces sample rows missing on either side. |
| **PostgreSQL / SQL Server cross-DB** | **Algo:** `auto` (`hashdiff` / `joindiff`) | Uses fast binary-tree bisection or direct SQL joins. |
            """
        )

    col_strat1, col_strat2, col_strat3 = st.columns([1.2, 1.2, 1])
    with col_strat1:
        scope_label = st.radio(
            "1️⃣ Match Scope",
            options=[
                "🎯 Exact Parity (Source == Target)",
                "📥 Subset Check (Source ⊆ Target, e.g. 100 in 200)",
            ],
            index=0,
            help="Choose 'Subset Check' when Target is allowed to have extra rows beyond Source.",
        )
        match_scope = "source_in_target" if "Subset" in scope_label else "exact"

    with col_strat2:
        algo_choice = st.selectbox(
            "2️⃣ Comparison Algorithm",
            options=[
                "auto (Smart Recommended)",
                "bucket_hash (Fastest Cloud Checksum — 2 Queries)",
                "keyless (No Primary Key Required)",
                "hashdiff (Cross-DB Tree Bisection)",
                "joindiff (Same-Engine SQL Join)",
            ],
            index=0,
        )
        selected_mode = algo_choice.split(" ")[0]

    with col_strat3:
        st.write("3️⃣ Validation Options")
        tiers = st.multiselect(
            "Active Tiers",
            options=["count", "schema", "data"],
            default=["count", "schema", "data"],
        )
        show_exact_values = st.checkbox(
            "🔍 Show Exact Mismatched Values (Developer Triage)",
            value=True,
            help="Displays exact differing Source vs Target values in the UI so developers can fix issues immediately.",
        )

    st.divider()

    # --------------------------------------------------------------------------
    # Section 2: Add Tables (Single, Schema Browser 50+, Bulk Paste / CSV)
    # --------------------------------------------------------------------------
    tab_single, tab_browser, tab_bulk = st.tabs([
        "➕ Single Table",
        "🗂️ Schema Browser (Bulk 50+ Tables)",
        "📝 Comma / CSV Bulk Loader",
    ])

    # --- TAB A: Single Table ---
    with tab_single:
        col_src, col_tgt = st.columns(2, gap="medium")
        with col_src:
            st.subheader("Source Endpoint")
            src_conn = st.selectbox("Source Connection", conn_names, key="single_src_conn")
            src_table = st.text_input("Source Table Name", placeholder="e.g. sales_db.orders", key="single_src_tbl")
            src_where = st.text_input("Source WHERE Filter (Optional)", placeholder="e.g. dt >= '2026-10-01'", key="single_src_where")

        with col_tgt:
            st.subheader("Target Endpoint")
            default_tgt_idx = 1 if len(conn_names) > 1 else 0
            tgt_conn = st.selectbox("Target Connection", conn_names, index=default_tgt_idx, key="single_tgt_conn")
            tgt_table = st.text_input("Target Table Name", placeholder="e.g. curated_db.orders", key="single_tgt_tbl")
            tgt_where = st.text_input("Target WHERE Filter (Optional)", placeholder="e.g. dt >= '2026-10-01'", key="single_tgt_where")

        if st.button("🔎 Fetch Columns from Source Table", key="fetch_cols_btn"):
            if not src_table.strip():
                st.warning("Enter a Source Table Name first (e.g. db.table_name).")
            else:
                with st.spinner(f"Fetching columns for {src_table}..."):
                    try:
                        resolver = ConnectionResolver({src_conn: connections[src_conn]})
                        db = resolver.get_database(src_conn)
                        parts = src_table.strip().split(".")
                        schema_part = parts[0] if len(parts) > 1 else (getattr(db, "default_schema", "default") or "default")
                        tbl_part = parts[-1]
                        cols_map = get_columns_and_types(db, schema_part, tbl_part)
                        resolver.close_all()
                        if cols_map:
                            st.session_state["fetched_columns"] = list(cols_map.keys())
                            st.success(f"Loaded {len(cols_map)} columns from `{src_table}`!")
                        else:
                            st.warning("No columns returned. Check table name or permissions.")
                    except Exception as e:
                        st.error(f"Could not fetch columns: {e}")

        fetched_cols = st.session_state.get("fetched_columns", [])

        col_keys, col_meta = st.columns(2, gap="medium")
        with col_keys:
            if fetched_cols:
                pks_selected = st.multiselect(
                    "Primary Key Column(s) (Leave empty for Keyless mode)",
                    options=fetched_cols,
                )
                exclude_selected = st.multiselect(
                    "Exclude Columns (Optional)",
                    options=fetched_cols,
                )
            else:
                pk_input = st.text_input(
                    "Primary Key Column(s) (Comma-separated, optional)",
                    placeholder="e.g. id, order_id (leave blank if keyless)",
                )
                pks_selected = [k.strip() for k in pk_input.split(",") if k.strip()]
                exclude_input = st.text_input(
                    "Exclude Columns (Comma-separated, optional)",
                    placeholder="e.g. etl_loaded_at",
                )
                exclude_selected = [k.strip() for k in exclude_input.split(",") if k.strip()]

        with col_meta:
            if fetched_cols:
                part_col = st.selectbox("Partition Column (Optional)", options=[""] + fetched_cols)
            else:
                part_col = st.text_input("Partition Column (Optional)", placeholder="e.g. dt")
            table_task_name = st.text_input(
                "Task / Alias Name",
                value=f"{src_table}_vs_{tgt_table}" if src_table and tgt_table else "reconciliation_task",
            )

        if st.button("➕ Add Table to Validation Batch", type="secondary", use_container_width=True):
            if not src_table.strip() or not tgt_table.strip():
                st.error("Please provide both Source Table and Target Table names.")
            else:
                t_cfg = TableConfig(
                    name=table_task_name.strip(),
                    source=TableEndpointConfig(connection=src_conn, table=src_table.strip(), where=src_where.strip() or None),
                    target=TableEndpointConfig(connection=tgt_conn, table=tgt_table.strip(), where=tgt_where.strip() or None),
                    primary_keys=pks_selected,
                    partition_column=part_col.strip() or None,
                    exclude_columns=exclude_selected,
                    tiers=tiers,
                    match_scope=match_scope,
                    data_diff=DataDiffConfig(mode=selected_mode, match_scope=match_scope),
                )
                add_table_to_queue(t_cfg)
                st.success(f"Added '{t_cfg.name}' to validation queue!")
                st.rerun()

    # --- TAB B: Schema Browser (Multi-select 50+ tables) ---
    with tab_browser:
        st.caption("Discover all tables in a database schema and queue dozens of tables with checkboxes.")
        b_col1, b_col2 = st.columns(2)
        with b_col1:
            b_src_conn = st.selectbox("Source Connection", conn_names, key="b_src_conn")
            b_src_schema = st.text_input("Source Schema / Database", placeholder="e.g. sales_db or public", key="b_src_schema")
        with b_col2:
            default_tgt_idx = 1 if len(conn_names) > 1 else 0
            b_tgt_conn = st.selectbox("Target Connection", conn_names, index=default_tgt_idx, key="b_tgt_conn")
            b_tgt_schema = st.text_input("Target Schema / Database", placeholder="e.g. curated_db or public", key="b_tgt_schema")

        if st.button("🔎 Load Tables from Schema", key="load_schema_tables"):
            if not b_src_schema.strip():
                st.warning("Please enter a Source Schema / Database name.")
            else:
                with st.spinner(f"Discovering tables in `{b_src_schema}`..."):
                    try:
                        resolver = ConnectionResolver({b_src_conn: connections[b_src_conn]})
                        db = resolver.get_database(b_src_conn)
                        tbls = get_tables(db, b_src_schema.strip())
                        resolver.close_all()
                        if tbls:
                            tgt_sch = b_tgt_schema.strip() or b_src_schema.strip()
                            st.session_state["browser_tables"] = [
                                {
                                    "Select": True,
                                    "Source Table": f"{b_src_schema.strip()}.{t}",
                                    "Target Table": f"{tgt_sch}.{t}",
                                    "Primary Keys": "",
                                    "Partition Column": "",
                                }
                                for t in tbls
                            ]
                            st.success(f"Found {len(tbls)} tables in `{b_src_schema}`!")
                        else:
                            st.warning(f"No tables found in `{b_src_schema}`.")
                    except Exception as e:
                        st.error(f"Failed to discover tables: {e}")

        browser_rows = st.session_state.get("browser_tables", [])
        if browser_rows:
            edited_rows = st.data_editor(
                browser_rows,
                use_container_width=True,
                num_rows="fixed",
                column_config={
                    "Select": st.column_config.CheckboxColumn("Select", default=True),
                    "Primary Keys": st.column_config.TextColumn("Primary Keys (comma-sep, optional)"),
                },
            )
            if st.button("➕ Add Checked Tables to Batch Queue", type="primary", use_container_width=True):
                added = 0
                for row in edited_rows:
                    if row.get("Select"):
                        s_t = row["Source Table"].strip()
                        t_t = row["Target Table"].strip()
                        pks = [k.strip() for k in str(row.get("Primary Keys", "")).split(",") if k.strip()]
                        p_col = str(row.get("Partition Column", "")).strip() or None
                        add_table_to_queue(
                            TableConfig(
                                name=s_t.split(".")[-1],
                                source=TableEndpointConfig(connection=b_src_conn, table=s_t),
                                target=TableEndpointConfig(connection=b_tgt_conn, table=t_t),
                                primary_keys=pks,
                                partition_column=p_col,
                                tiers=tiers,
                                match_scope=match_scope,
                                data_diff=DataDiffConfig(mode=selected_mode, match_scope=match_scope),
                            )
                        )
                        added += 1
                st.success(f"Queued {added} table(s) into the validation batch!")
                st.rerun()

    # --- TAB C: Comma-Separated / CSV Bulk Loader ---
    with tab_bulk:
        st.caption("Quickly queue 50+ tables by pasting a list or uploading a CSV.")
        k_col1, k_col2 = st.columns(2)
        with k_col1:
            bulk_src_conn = st.selectbox("Source Connection", conn_names, key="bulk_src_conn")
            bulk_src_prefix = st.text_input("Default Source Schema Prefix (Optional)", placeholder="e.g. raw_db", key="bulk_src_pre")
        with k_col2:
            default_tgt_idx = 1 if len(conn_names) > 1 else 0
            bulk_tgt_conn = st.selectbox("Target Connection", conn_names, index=default_tgt_idx, key="bulk_tgt_conn")
            bulk_tgt_prefix = st.text_input("Default Target Schema Prefix (Optional)", placeholder="e.g. curated_db", key="bulk_tgt_pre")

        bulk_text = st.text_area(
            "Paste Table Names (comma-separated or one per line)",
            placeholder="orders, customers, products\n# Or explicit mapping:\nraw_db.orders -> curated_db.orders | order_id",
            height=130,
        )

        if st.button("📥 Parse & Add Pasted Tables", use_container_width=True):
            raw_tokens = []
            for line in bulk_text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "->" in line or "|" in line:
                    raw_tokens.append(line)
                else:
                    raw_tokens.extend([tok.strip() for tok in line.split(",") if tok.strip()])

            count_added = 0
            for tok in raw_tokens:
                pks = []
                if "|" in tok:
                    tbl_part, pk_part = tok.split("|", 1)
                    pks = [k.strip() for k in pk_part.split(",") if k.strip()]
                else:
                    tbl_part = tok

                if "->" in tbl_part:
                    s_raw, t_raw = [x.strip() for x in tbl_part.split("->", 1)]
                else:
                    s_raw = f"{bulk_src_prefix.strip()}.{tbl_part.strip()}" if bulk_src_prefix.strip() and "." not in tbl_part else tbl_part.strip()
                    t_raw = f"{bulk_tgt_prefix.strip()}.{tbl_part.strip()}" if bulk_tgt_prefix.strip() and "." not in tbl_part else tbl_part.strip()

                add_table_to_queue(
                    TableConfig(
                        name=s_raw.split(".")[-1],
                        source=TableEndpointConfig(connection=bulk_src_conn, table=s_raw),
                        target=TableEndpointConfig(connection=bulk_tgt_conn, table=t_raw),
                        primary_keys=pks,
                        tiers=tiers,
                        match_scope=match_scope,
                        data_diff=DataDiffConfig(mode=selected_mode, match_scope=match_scope),
                    )
                )
                count_added += 1

            if count_added:
                st.success(f"Added {count_added} table(s) to batch queue!")
                st.rerun()
            else:
                st.warning("No valid table entries found to parse.")

        st.markdown("**Or Upload a CSV File** (`source_table,target_table,primary_keys,partition_column`)")
        sample_csv = "source_table,target_table,primary_keys,partition_column\nsales_db.orders,curated_db.orders,order_id,dt\nsales_db.customers,curated_db.customers,customer_id,\n"
        st.download_button("📄 Download CSV Template", data=sample_csv, file_name="bulk_tables_template.csv", mime="text/csv")
        csv_file = st.file_uploader("Upload Bulk Tables CSV", type=["csv"], key="bulk_csv_upload")
        if csv_file is not None and st.button("🚀 Import CSV into Queue", use_container_width=True):
            reader = csv.DictReader(io.StringIO(csv_file.getvalue().decode("utf-8")))
            csv_added = 0
            for row in reader:
                s_t = (row.get("source_table") or "").strip()
                t_t = (row.get("target_table") or s_t).strip()
                if not s_t:
                    continue
                pks = [k.strip() for k in (row.get("primary_keys") or "").split(",") if k.strip()]
                p_col = (row.get("partition_column") or "").strip() or None
                add_table_to_queue(
                    TableConfig(
                        name=s_t.split(".")[-1],
                        source=TableEndpointConfig(connection=bulk_src_conn, table=s_t),
                        target=TableEndpointConfig(connection=bulk_tgt_conn, table=t_t),
                        primary_keys=pks,
                        partition_column=p_col,
                        tiers=tiers,
                        match_scope=match_scope,
                        data_diff=DataDiffConfig(mode=selected_mode, match_scope=match_scope),
                    )
                )
                csv_added += 1
            st.success(f"Imported {csv_added} table(s) from CSV!")
            st.rerun()

    st.divider()

    # --------------------------------------------------------------------------
    # Section 3: Validation Batch Queue, Saved Presets & Live Runner
    # --------------------------------------------------------------------------
    st.subheader("📋 Validation Batch Queue")

    # Preset Save/Load toolbar
    presets = load_saved_presets()
    p_col1, p_col2, p_col3 = st.columns([1.5, 1.5, 1])
    with p_col1:
        if presets:
            chosen_preset = st.selectbox("Load Saved Batch Preset", [""] + list(presets.keys()))
            if chosen_preset and st.button("📂 Load Preset"):
                clear_table_queue()
                for tbl in presets[chosen_preset]:
                    add_table_to_queue(tbl)
                st.success(f"Loaded preset '{chosen_preset}'!")
                st.rerun()
    with p_col2:
        preset_name = st.text_input("Save Current Queue as Preset", placeholder="e.g. nightly_athena_50")
    with p_col3:
        st.write("")
        st.write("")
        if st.button("💾 Save Preset") and preset_name.strip():
            q_now = st.session_state.get("table_queue", [])
            if q_now:
                save_preset_to_disk(preset_name.strip(), q_now)
                st.success(f"Saved preset '{preset_name.strip()}'!")
                st.rerun()

    queue = st.session_state.get("table_queue", [])
    if not queue:
        st.info("No tables in the validation queue yet. Use one of the tabs above to add tables.")
        return

    st.write(f"**Queued Tables:** `{len(queue)}`")
    for idx, t in enumerate(queue):
        c_info, c_del = st.columns([6, 1])
        pks_str = ", ".join(t.primary_keys) if t.primary_keys else "keyless"
        t_scope = t.match_scope or "exact"
        c_info.write(
            f"**{idx + 1}. {t.name}** — `{t.source.connection}:{t.source.table}` &rarr; `{t.target.connection}:{t.target.table}` "
            f"| PKs: `[{pks_str}]` | Scope: `{t_scope}`"
        )
        if c_del.button("Remove", key=f"rm_queue_{idx}"):
            remove_table_from_queue(idx)
            st.rerun()

    col_act1, col_act2 = st.columns([1, 4])
    with col_act1:
        if st.button("Clear Queue", use_container_width=True):
            clear_table_queue()
            st.rerun()

    with col_act2:
        run_clicked = st.button("🚀 Run Batch Validation Now", type="primary", use_container_width=True)

    if run_clicked:
        session_id = st.session_state.get("session_id", "local")
        reports_dir = get_session_reports_dir(session_id)

        manifest = SITManifest(
            version="1.0",
            run_name=f"ui_run_{session_id}_{int(time.time())}",
            connections=connections,
            defaults=DefaultsConfig(
                tiers=tiers or ["count", "schema", "data"],
                fail_fast=FailFastConfig(),
                data_diff=DataDiffConfig(mode=selected_mode, match_scope=match_scope),
                reporting=ReportingConfig(
                    output_dir=str(reports_dir),
                    include_sample_values=show_exact_values,
                ),
            ),
            tables=queue,
        )

        runner = BatchRunner(
            manifest=manifest,
            max_workers=4,
            include_sample_values=show_exact_values,
        )

        st.subheader("📡 Live Execution Progress & Logs")
        prog_bar = st.progress(0.0, text="Initializing worker threads...")
        status_box = st.empty()
        log_box = st.empty()

        result = run_batch_with_live_logs(
            runner=runner,
            total_tables=len(queue),
            progress_bar=prog_bar,
            status_placeholder=status_box,
            log_placeholder=log_box,
        )
        st.session_state["current_result"] = result

        if result.overall_status == "PASSED":
            st.balloons()
            st.success(f"🎉 Parity Verified! All {result.total_tables} tables passed.")
        else:
            st.error(f"⚠️ Validation finished with mismatches: {result.failed_tables}/{result.total_tables} tables failed.")

        st.info("👉 Switch to the **📊 Results Dashboard** tab in the sidebar to inspect exact differing rows, columns, and download reports.")
