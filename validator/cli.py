"""Enterprise Validation Layer CLI powered by Typer and Rich."""

import logging
from pathlib import Path
import time
from typing import List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
import typer

from data_diff import connect
from validator.config.connections import ConnectionResolver, load_manifest
from validator.engine.count_validator import CountValidator
from validator.engine.data_validator import DataValidator
from validator.engine.schema_validator import SchemaValidator
from validator.observability import setup_logging
from validator.orchestrator.runner import BatchRunner
from validator.reporting.exporters import (
    export_junit_xml,
    export_mismatches_csv,
    export_summary_json,
)
from validator.reporting.html_report import generate_html_report
from validator.security import mask_uri, sanitize_error_message

app = typer.Typer(
    name="data-validator",
    help="Enterprise cross-database validation & diff engine for SIT data reconciliation.",
    no_args_is_help=True,
)
console = Console()
logger = logging.getLogger("validator.cli")


def _split_uri_table(uri_with_table: str) -> tuple:
    """Split URI into base connection URI and table name.

    e.g. 'athena://.../sales_db/orders' -> ('athena://.../sales_db', 'orders')
    """
    if "/" not in uri_with_table:
        raise ValueError(f"Invalid URI format: {uri_with_table}. Expected schema/table path.")

    # Remove query string if present
    query = ""
    base = uri_with_table
    if "?" in uri_with_table:
        base, query = uri_with_table.split("?", 1)
        query = f"?{query}"

    parts = base.rstrip("/").rsplit("/", 1)
    if len(parts) == 2:
        return f"{parts[0]}{query}", parts[1]
    return uri_with_table, parts[0]


def _execute_run(
    manifest_path: Path,
    resume: bool = False,
    include_sample_values: bool = False,
    output_dir: Optional[Path] = None,
    max_workers: int = 4,
    log_format: str = "console",
    log_level: str = "INFO",
) -> None:
    """Common implementation for validate and run commands."""
    setup_logging(log_format=log_format, log_level=log_level)

    console.print(
        Panel.fit(
            f"[bold cyan]Starting Batch SIT Validation[/bold cyan]\n"
            f"Manifest: [yellow]{manifest_path}[/yellow]\n"
            f"Resume: [green]{resume}[/green]\n"
            f"Include Sample Values: [magenta]{include_sample_values}[/magenta]\n"
            f"Log Format: [blue]{log_format}[/blue]",
            border_style="cyan",
        )
    )

    try:
        manifest = load_manifest(manifest_path)
    except Exception as e:
        clean_err = sanitize_error_message(e)
        console.print(f"[bold red]Failed to load manifest:[/bold red] {clean_err}")
        raise typer.Exit(code=2)

    runner = BatchRunner(
        manifest=manifest,
        max_workers=max_workers,
        resume=resume,
        include_sample_values=include_sample_values,
    )

    with console.status("[bold green]Executing table validations across workers..."):
        batch_result = runner.run()

    # Print Summary Table
    table = Table(
        title=f"SIT Batch Run: {batch_result.run_name} (Status: {batch_result.overall_status})",
        border_style="blue",
    )
    table.add_column("Table Name", style="cyan", no_wrap=True)
    table.add_column("Status", style="bold")
    table.add_column("Source Rows", justify="right")
    table.add_column("Target Rows", justify="right")
    table.add_column("Count Diff %", justify="right")
    table.add_column("Data Mismatches", justify="right")
    table.add_column("Duration", justify="right")

    for tbl in batch_result.table_results:
        st_style = "green" if tbl.status == "PASSED" else ("dim" if tbl.status == "SKIPPED" else "red")
        src_cnt = f"{tbl.count_result.source_total_count:,}" if tbl.count_result else "-"
        tgt_cnt = f"{tbl.count_result.target_total_count:,}" if tbl.count_result else "-"
        c_diff_pct = f"{tbl.count_result.count_diff_pct:.2f}%" if tbl.count_result else "-"
        data_diffs = str(tbl.data_result.differing_rows_count) if tbl.data_result else "-"
        dur = f"{tbl.execution_time_seconds:.2f}s"

        table.add_row(
            tbl.table_name,
            f"[{st_style}]{tbl.status}[/{st_style}]",
            src_cnt,
            tgt_cnt,
            c_diff_pct,
            data_diffs,
            dur,
        )

    console.print(table)

    # Reporting output generation
    rep_dir = output_dir or Path(manifest.defaults.reporting.output_dir)
    rep_dir.mkdir(parents=True, exist_ok=True)
    formats = manifest.defaults.reporting.formats

    console.print(f"\n[bold]Generating reports in:[/bold] [yellow]{rep_dir.resolve()}[/yellow]")
    if "html" in formats:
        html_path = generate_html_report(batch_result, rep_dir / "sit_report.html")
        console.print(f"  &bull; HTML Report: [green]{html_path}[/green]")
    if "json" in formats:
        json_path = export_summary_json(batch_result, rep_dir / "summary.json")
        console.print(f"  &bull; JSON Summary: [green]{json_path}[/green]")
    if "csv" in formats:
        csv_path = export_mismatches_csv(
            batch_result,
            rep_dir / "mismatches.csv",
            include_sample_values=include_sample_values,
        )
        console.print(f"  &bull; CSV Mismatches: [green]{csv_path}[/green]")
    if "junit" in formats:
        junit_path = export_junit_xml(batch_result, rep_dir / "junit.xml")
        console.print(f"  &bull; JUnit XML: [green]{junit_path}[/green]")

    if batch_result.overall_status != "PASSED":
        raise typer.Exit(code=1)


@app.command()
def validate(
    manifest_path: Path = typer.Option(
        ...,
        "--manifest",
        "-m",
        "--config",
        "-c",
        help="Path to SIT YAML manifest file.",
        exists=True,
        readable=True,
    ),
    resume: bool = typer.Option(
        False,
        "--resume",
        "-r",
        help="Resume an interrupted run, skipping already-passed tables.",
    ),
    include_sample_values: bool = typer.Option(
        False,
        "--include-sample-values",
        help="Include raw differing data values in reports (default: False for PII security).",
    ),
    output_dir: Optional[Path] = typer.Option(
        None,
        "--output-dir",
        "-o",
        help="Override reporting output directory.",
    ),
    max_workers: int = typer.Option(
        4,
        "--workers",
        "-w",
        help="Number of concurrent table validation threads.",
    ),
    log_format: str = typer.Option(
        "console",
        "--log-format",
        help="Logging format: 'console' or 'json'.",
    ),
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        help="Logging verbosity level (DEBUG, INFO, WARNING, ERROR).",
    ),
) -> None:
    """Execute full multi-table batch validation from declarative YAML manifest."""
    _execute_run(
        manifest_path=manifest_path,
        resume=resume,
        include_sample_values=include_sample_values,
        output_dir=output_dir,
        max_workers=max_workers,
        log_format=log_format,
        log_level=log_level,
    )


@app.command()
def run(
    manifest_path: Path = typer.Option(
        ...,
        "--config",
        "-c",
        "--manifest",
        "-m",
        help="Path to SIT YAML manifest file.",
        exists=True,
        readable=True,
    ),
    resume: bool = typer.Option(
        False,
        "--resume",
        "-r",
        help="Resume an interrupted run, skipping already-passed tables.",
    ),
    include_sample_values: bool = typer.Option(
        False,
        "--include-sample-values",
        help="Include raw differing data values in reports (default: False for PII security).",
    ),
    output_dir: Optional[Path] = typer.Option(
        None,
        "--output-dir",
        "-o",
        help="Override reporting output directory.",
    ),
    max_workers: int = typer.Option(
        4,
        "--workers",
        "-w",
        help="Number of concurrent table validation threads.",
    ),
    log_format: str = typer.Option(
        "console",
        "--log-format",
        help="Logging format: 'console' or 'json'.",
    ),
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        help="Logging verbosity level (DEBUG, INFO, WARNING, ERROR).",
    ),
) -> None:
    """Run batch SIT reconciliation (alias for 'validate')."""
    _execute_run(
        manifest_path=manifest_path,
        resume=resume,
        include_sample_values=include_sample_values,
        output_dir=output_dir,
        max_workers=max_workers,
        log_format=log_format,
        log_level=log_level,
    )


@app.command(name="test-connection")
def test_connection(
    config_path: Path = typer.Option(
        ...,
        "--config",
        "-c",
        "--manifest",
        "-m",
        help="Path to SIT YAML manifest file.",
        exists=True,
        readable=True,
    ),
    connection_name: Optional[str] = typer.Option(
        None,
        "--connection",
        help="Optional specific connection name to test.",
    ),
) -> None:
    """Test connectivity to databases declared in the manifest."""
    console.print(f"[bold cyan]Testing database connections from:[/bold cyan] {config_path}")

    try:
        manifest = load_manifest(config_path)
    except Exception as e:
        console.print(f"[bold red]Configuration error:[/bold red] {sanitize_error_message(e)}")
        raise typer.Exit(code=2)

    resolver = ConnectionResolver(manifest.connections)
    conns_to_test = manifest.connections.keys()
    if connection_name:
        if connection_name not in manifest.connections:
            console.print(f"[bold red]Error:[/bold red] Connection '{connection_name}' not found in manifest.")
            raise typer.Exit(code=2)
        conns_to_test = [connection_name]

    table = Table(title="Database Connection Test Results", border_style="cyan")
    table.add_column("Connection Name", style="bold")
    table.add_column("Dialect")
    table.add_column("Endpoint (Masked)")
    table.add_column("Status")
    table.add_column("Latency", justify="right")

    all_passed = True

    for c_name in conns_to_test:
        cfg = manifest.connections[c_name]
        masked_endpoint = mask_uri(cfg.uri or cfg.database or cfg.host or "configured")
        start_t = time.time()
        try:
            db = resolver.get_database(c_name)
            # Lightweight ping
            if hasattr(db, "query"):
                try:
                    db.query("SELECT 1")
                except Exception:
                    pass  # Some engines may not support raw SELECT 1 without table, but connection succeeded
            latency = f"{(time.time() - start_t) * 1000:.1f}ms"
            table.add_row(
                c_name,
                cfg.dialect,
                masked_endpoint,
                "[green]CONNECTED[/green]",
                latency,
            )
        except Exception as e:
            all_passed = False
            latency = f"{(time.time() - start_t) * 1000:.1f}ms"
            table.add_row(
                c_name,
                cfg.dialect,
                masked_endpoint,
                f"[red]FAILED: {sanitize_error_message(e)}[/red]",
                latency,
            )

    console.print(table)
    if not all_passed:
        raise typer.Exit(code=1)


@app.command(name="validate-config")
def validate_config(
    config_path: Path = typer.Option(
        ...,
        "--config",
        "-c",
        "--manifest",
        "-m",
        help="Path to SIT YAML manifest file.",
        exists=True,
        readable=True,
    ),
) -> None:
    """Validate SIT manifest syntax, environment variable resolution, and table mappings."""
    console.print(f"[bold cyan]Validating configuration:[/bold cyan] {config_path}")

    try:
        manifest = load_manifest(config_path)
    except Exception as e:
        console.print(f"[bold red]Configuration validation failed:[/bold red] {sanitize_error_message(e)}")
        raise typer.Exit(code=2)

    table = Table(title=f"Manifest Validation Summary: {manifest.run_name}", border_style="green")
    table.add_column("Table Name", style="cyan")
    table.add_column("Source Endpoint")
    table.add_column("Target Endpoint")
    table.add_column("Primary Keys")
    table.add_column("Tiers")

    for tbl in manifest.tables:
        src_str = f"{tbl.source.connection}:{tbl.source.table}"
        tgt_str = f"{tbl.target.connection}:{tbl.target.table}"
        pk_str = ", ".join(tbl.primary_keys) if tbl.primary_keys else "[dim]keyless[/dim]"
        tiers_str = ", ".join(tbl.tiers or manifest.defaults.tiers)
        table.add_row(tbl.name, src_str, tgt_str, pk_str, tiers_str)

    console.print(table)
    console.print(
        Panel.fit(
            f"[bold green]Configuration manifest is valid![/bold green]\n"
            f"Run: [yellow]{manifest.run_name}[/yellow] &bull; Version: {manifest.version}\n"
            f"Connections: {len(manifest.connections)} &bull; Tables: {len(manifest.tables)}\n"
            f"Fail-Fast Schema: {manifest.defaults.fail_fast.skip_data_on_schema_fail} &bull; "
            f"Count Diff Threshold: {manifest.defaults.fail_fast.skip_data_if_count_diff_pct_gt}%",
            border_style="green",
        )
    )


@app.command()
def diff(
    source_uri: str = typer.Option(..., "--source-uri", "-s", help="Source table URI."),
    target_uri: str = typer.Option(..., "--target-uri", "-t", help="Target table URI."),
    primary_key: List[str] = typer.Option(..., "--primary-key", "-k", help="Primary key column(s)."),
    mode: str = typer.Option("auto", "--mode", "-m", help="Diff mode: auto, bucket_hash, hashdiff, joindiff."),
    where: Optional[str] = typer.Option(None, "--where", "-w", help="WHERE filter clause."),
    bucket_count: int = typer.Option(64, "--buckets", help="Bucket count for bucket_hash mode."),
) -> None:
    """Run ad-hoc row-level data diff on a single table pair."""
    s_uri, s_table = _split_uri_table(source_uri)
    t_uri, t_table = _split_uri_table(target_uri)

    console.print(f"[bold cyan]Comparing[/bold cyan] {s_table} &rarr; {t_table}...")

    s_db = connect(s_uri)
    t_db = connect(t_uri)

    validator = DataValidator()
    res = validator.validate(
        source_db=s_db,
        target_db=t_db,
        source_table=s_table,
        target_table=t_table,
        primary_keys=primary_key,
        mode=mode,
        bucket_count=bucket_count,
        source_where=where,
        target_where=where,
    )

    st = "[green]MATCH[/green]" if res.passed else "[red]MISMATCH[/red]"
    console.print(
        f"Result: {st} | Mode: [yellow]{res.mode_used}[/yellow] | "
        f"Differing Rows: [bold]{res.differing_rows_count}[/bold] | "
        f"Duration: {res.execution_time_seconds:.2f}s"
    )

    if res.mismatch_samples:
        t = Table(title="Sample Mismatches")
        t.add_column("Type")
        t.add_column("Key")
        t.add_column("Diffs")
        for m in res.mismatch_samples[:10]:
            t.add_row(m.diff_type, str(m.key_values), str(m.column_diffs))
        console.print(t)

    if not res.passed:
        raise typer.Exit(code=1)


@app.command(name="check-schema")
def check_schema(
    source_uri: str = typer.Option(..., "--source-uri", "-s", help="Source table URI."),
    target_uri: str = typer.Option(..., "--target-uri", "-t", help="Target table URI."),
    allow_extra_columns: bool = typer.Option(False, "--allow-extra-columns", help="Allow extra target columns."),
) -> None:
    """Run schema comparison between source and target tables."""
    s_uri, s_table = _split_uri_table(source_uri)
    t_uri, t_table = _split_uri_table(target_uri)

    s_db = connect(s_uri)
    t_db = connect(t_uri)

    validator = SchemaValidator()
    res = validator.validate(
        source_db=s_db,
        target_db=t_db,
        source_table=s_table,
        target_table=t_table,
        allow_extra_target_columns=allow_extra_columns,
    )

    table = Table(title=f"Schema Diff: {s_table} vs {t_table}")
    table.add_column("Column")
    table.add_column("Source Type")
    table.add_column("Target Type")
    table.add_column("Canonical Type")
    table.add_column("Diff Type")
    table.add_column("Severity")

    for d in res.column_diffs:
        sev_style = "green" if d.severity == "OK" else ("yellow" if d.severity == "WARN" else "red")
        table.add_row(
            d.column_name,
            d.source_raw_type or "-",
            d.target_raw_type or "-",
            d.source_canonical_type or d.target_canonical_type,
            d.diff_type,
            f"[{sev_style}]{d.severity}[/{sev_style}]",
        )

    console.print(table)
    if not res.passed:
        raise typer.Exit(code=1)


@app.command(name="check-counts")
def check_counts(
    source_uri: str = typer.Option(..., "--source-uri", "-s", help="Source table URI."),
    target_uri: str = typer.Option(..., "--target-uri", "-t", help="Target table URI."),
    partition_col: Optional[str] = typer.Option(None, "--partition-col", "-p", help="Partition column."),
    threshold_pct: float = typer.Option(0.0, "--threshold", help="Count difference % threshold."),
    where: Optional[str] = typer.Option(None, "--where", "-w", help="WHERE filter clause."),
) -> None:
    """Run row count and volume comparison between source and target tables."""
    s_uri, s_table = _split_uri_table(source_uri)
    t_uri, t_table = _split_uri_table(target_uri)

    s_db = connect(s_uri)
    t_db = connect(t_uri)

    validator = CountValidator()
    res = validator.validate(
        source_db=s_db,
        target_db=t_db,
        source_table=s_table,
        target_table=t_table,
        partition_column=partition_col,
        threshold_pct=threshold_pct,
        source_where=where,
        target_where=where,
    )

    st = "[green]PASSED[/green]" if res.passed else "[red]FAILED[/red]"
    console.print(
        f"Count Validation: {st} | "
        f"Source: [bold]{res.source_total_count:,}[/bold] | "
        f"Target: [bold]{res.target_total_count:,}[/bold] | "
        f"Delta: [bold]{res.count_diff:+,}[/bold] ({res.count_diff_pct:.2f}%)"
    )

    if res.partition_diffs:
        t = Table(title=f"Partition Breakdown ({partition_col})")
        t.add_column("Partition")
        t.add_column("Source Count")
        t.add_column("Target Count")
        t.add_column("Delta")
        t.add_column("Status")
        for p in res.partition_diffs:
            s_style = "green" if p.status == "MATCH" else "red"
            t.add_row(
                p.partition_key,
                str(p.source_count),
                str(p.target_count),
                f"{p.diff:+,}",
                f"[{s_style}]{p.status}[/{s_style}]",
            )
        console.print(t)

    if not res.passed:
        raise typer.Exit(code=1)


@app.command(name="ui")
def ui(
    port: int = typer.Option(8501, "--port", "-p", help="Port to run Streamlit web server on."),
    host: str = typer.Option("localhost", "--host", help="Host address to bind to."),
) -> None:
    """Launch the interactive web-based UI application in your browser."""
    import subprocess
    import sys

    ui_script = Path(__file__).parent / "ui" / "app.py"
    if not ui_script.exists():
        console.print(f"[bold red]Error:[/bold red] Streamlit app script not found at {ui_script}")
        raise typer.Exit(code=2)

    console.print(
        Panel.fit(
            f"[bold cyan]Launching Data Validator Web Studio...[/bold cyan]\n"
            f"URL: [green]http://{host}:{port}[/green]\n"
            f"Press [bold yellow]Ctrl+C[/bold yellow] to stop the server.",
            border_style="cyan",
        )
    )

    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(ui_script),
        "--server.port",
        str(port),
        "--server.address",
        host,
    ]
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        console.print("\n[yellow]Streamlit web server stopped.[/yellow]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
