"""Connection Manager View for Data Validator Streamlit UI."""

import streamlit as st

from validator.config.manifest import ConnectionConfig
from validator.ui.introspect import check_db_connectivity, get_available_okta_profiles
from validator.ui.state import delete_connection, get_connections, save_connection


def render_connections_view() -> None:
    """Render the Visual Connection Manager page."""
    st.header("🔌 Database Connection Manager")
    st.caption("Configure and test database connections without editing code or YAML files.")

    col_form, col_list = st.columns([1.2, 1], gap="large")

    with col_form:
        st.subheader("Add or Edit Connection")
        conn_name = st.text_input("Connection Name", placeholder="e.g. athena_prod or postgres_dw")

        dialect_options = [
            "AWS Athena (Okta / IAM)",
            "PostgreSQL",
            "Microsoft SQL Server (MSSQL)",
            "Alibaba Cloud MaxCompute (ODPS)",
            "DuckDB / SQLite (Local)",
        ]
        selected_dialect = st.selectbox("Database Dialect", dialect_options)

        cfg: ConnectionConfig

        if "Athena" in selected_dialect:
            st.info("💡 **Okta Authentication:** Select the AWS profile created by your Okta login (e.g. saml2aws, gimme-aws-creds).")
            okta_profiles = get_available_okta_profiles()
            chosen_profile = st.selectbox("Okta AWS Profile", okta_profiles)
            region = st.text_input("AWS Region", value="us-east-1")
            s3_staging = st.text_input("S3 Staging Directory", placeholder="s3://your-bucket/staging/")
            workgroup = st.text_input("Athena Workgroup", value="primary")

            cfg = ConnectionConfig(
                type="athena",
                profile_name=chosen_profile,
                region=region,
                s3_staging_dir=s3_staging,
                workgroup=workgroup,
            )

        elif "PostgreSQL" in selected_dialect:
            col_h, col_p = st.columns([2, 1])
            host = col_h.text_input("Host", value="localhost")
            port = col_p.number_input("Port", value=5432, step=1)
            database = st.text_input("Database Name")
            schema = st.text_input("Schema", value="public")
            user = st.text_input("Username")
            password = st.text_input("Password", type="password")

            cfg = ConnectionConfig(
                type="postgres",
                host=host,
                port=int(port),
                database=database,
                schema_name=schema,
                user=user,
                password=password,
            )

        elif "SQL Server" in selected_dialect:
            col_h, col_p = st.columns([2, 1])
            host = col_h.text_input("Host", value="localhost")
            port = col_p.number_input("Port", value=1433, step=1)
            database = st.text_input("Database Name")
            schema = st.text_input("Schema", value="dbo")
            user = st.text_input("Username")
            password = st.text_input("Password", type="password")

            cfg = ConnectionConfig(
                type="mssql",
                host=host,
                port=int(port),
                database=database,
                schema_name=schema,
                user=user,
                password=password,
            )

        elif "MaxCompute" in selected_dialect:
            endpoint = st.text_input("ODPS Endpoint", value="https://service.odps.aliyun.com/api")
            project = st.text_input("Project Name")
            access_id = st.text_input("Access ID")
            access_key = st.text_input("Access Key", type="password")

            cfg = ConnectionConfig(
                type="maxcompute",
                endpoint=endpoint,
                project=project,
                access_id=access_id,
                access_key=access_key,
            )

        else:  # DuckDB
            fp = st.text_input("Database File Path", value=":memory:")
            cfg = ConnectionConfig(
                type="duckdb",
                database=fp,
            )

        btn_col1, btn_col2 = st.columns(2)
        with btn_col1:
            if st.button("⚡ Test Connection", use_container_width=True):
                with st.spinner("Pinging database..."):
                    ok, latency, err = check_db_connectivity(cfg)
                    if ok:
                        st.success(f"🟢 Connected successfully! ({latency}ms)")
                    else:
                        st.error(f"🔴 Connection failed: {err}")

        with btn_col2:
            if st.button("💾 Save Connection", type="primary", use_container_width=True):
                if not conn_name.strip():
                    st.warning("Please provide a Connection Name before saving.")
                else:
                    save_connection(conn_name.strip(), cfg)
                    st.success(f"Connection '{conn_name.strip()}' saved!")
                    st.rerun()

    with col_list:
        st.subheader("Configured Connections")
        connections = get_connections()

        if not connections:
            st.info("No connections configured yet. Use the form on the left to add your first database connection.")
        else:
            for c_name, c_cfg in list(connections.items()):
                with st.expander(f"**{c_name}** ({c_cfg.type})", expanded=True):
                    st.write(f"- **Type:** `{c_cfg.type}`")
                    if c_cfg.profile_name:
                        st.write(f"- **AWS Profile:** `{c_cfg.profile_name}`")
                    if c_cfg.region:
                        st.write(f"- **Region:** `{c_cfg.region}`")
                    if c_cfg.host:
                        st.write(f"- **Host:** `{c_cfg.host}`")
                    if c_cfg.database:
                        st.write(f"- **Database:** `{c_cfg.database}`")

                    c_act1, c_act2 = st.columns(2)
                    with c_act1:
                        if st.button("Test", key=f"test_{c_name}", use_container_width=True):
                            ok, lat, err = check_db_connectivity(c_cfg)
                            if ok:
                                st.success(f"Connected ({lat}ms)")
                            else:
                                st.error(f"Failed: {err}")
                    with c_act2:
                        if st.button("Delete", key=f"del_{c_name}", type="secondary", use_container_width=True):
                            delete_connection(c_name)
                            st.rerun()
