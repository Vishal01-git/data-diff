"""Manifest Studio View for YAML Import and Export in Data Validator Streamlit UI."""

from pathlib import Path
import time
import streamlit as st
import yaml

from validator.config.connections import load_manifest
from validator.config.manifest import (
    DataDiffConfig,
    DefaultsConfig,
    FailFastConfig,
    ReportingConfig,
    SITManifest,
)
from validator.ui.state import add_table_to_queue, clear_table_queue, get_connections, save_connection


def render_manifest_view() -> None:
    """Render the Manifest Import / Export Studio."""
    st.header("📑 Manifest Studio (YAML Interoperability)")
    st.caption("Export your visual configuration to a production SIT manifest for CI/CD, or import existing YAML manifests.")

    tab_export, tab_import = st.tabs(["📤 Export UI to Manifest YAML", "📥 Import Existing Manifest YAML"])

    with tab_export:
        st.subheader("Generate Manifest from Current UI State")
        connections = get_connections()
        queue = st.session_state.get("table_queue", [])

        if not connections or not queue:
            st.info("To generate a manifest, please configure at least one connection and add at least one table in the **🎯 Reconciliation Studio**.")
        else:
            run_name = st.text_input("Run Name", value=f"sit_batch_{int(time.time())}")
            manifest = SITManifest(
                version="1.0",
                run_name=run_name,
                connections=connections,
                defaults=DefaultsConfig(
                    tiers=["count", "schema", "data"],
                    fail_fast=FailFastConfig(),
                    data_diff=DataDiffConfig(),
                    reporting=ReportingConfig(output_dir="./reports"),
                ),
                tables=queue,
            )

            # Dump manifest to YAML
            manifest_dict = manifest.model_dump(exclude_none=True)
            # Clean up empty properties
            for c_name, c_dict in manifest_dict.get("connections", {}).items():
                if "dialect" in c_dict:
                    del c_dict["dialect"]

            yaml_str = yaml.dump(manifest_dict, sort_keys=False, indent=2)

            st.code(yaml_str, language="yaml")

            col_btn1, col_btn2 = st.columns(2)
            with col_btn1:
                st.download_button(
                    label="📥 Download sit_manifest.yaml",
                    data=yaml_str,
                    file_name=f"{run_name}.yaml",
                    mime="text/yaml",
                    use_container_width=True,
                )

            with col_btn2:
                if st.button("💾 Save to Local configs/ folder", use_container_width=True):
                    configs_dir = Path("configs")
                    configs_dir.mkdir(parents=True, exist_ok=True)
                    out_path = configs_dir / f"{run_name}.yaml"
                    out_path.write_text(yaml_str, encoding="utf-8")
                    st.success(f"Saved manifest to `{out_path.resolve()}`!")

    with tab_import:
        st.subheader("Import an Existing Manifest File")
        uploaded_file = st.file_uploader("Upload YAML Manifest", type=["yaml", "yml"])

        if uploaded_file is not None:
            try:
                import tempfile

                with tempfile.NamedTemporaryFile("wb", delete=False, suffix=".yaml") as tmp:
                    tmp.write(uploaded_file.getvalue())
                    tmp_path = Path(tmp.name)

                loaded_manifest = load_manifest(tmp_path)
                tmp_path.unlink(missing_ok=True)

                st.success(f"Successfully parsed manifest: **{loaded_manifest.run_name}** (v{loaded_manifest.version})")
                st.write(f"- **Connections found:** `{len(loaded_manifest.connections)}` ({', '.join(loaded_manifest.connections.keys())})")
                st.write(f"- **Tables found:** `{len(loaded_manifest.tables)}`")

                if st.button("🚀 Load into Reconciliation Studio", type="primary"):
                    # Save connections into state
                    for c_name, c_cfg in loaded_manifest.connections.items():
                        save_connection(c_name, c_cfg)

                    # Replace table queue
                    clear_table_queue()
                    for tbl in loaded_manifest.tables:
                        add_table_to_queue(tbl)

                    st.success("Loaded all connections and tables into the Studio!")
                    st.info("Switch to the **🎯 Reconciliation Studio** to run or inspect them.")

            except Exception as e:
                st.error(f"Failed to parse manifest: {e}")
