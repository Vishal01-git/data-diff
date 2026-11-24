"""Exporters for machine-readable summary.json, mismatches.csv, and junit.xml."""

import csv
import json
from pathlib import Path
from typing import Optional, Union
from xml.etree import ElementTree as ET

from validator.orchestrator.runner import BatchValidationResult


def export_summary_json(
    result: BatchValidationResult,
    output_path: Union[str, Path],
) -> Path:
    """Export machine-readable JSON summary of the validation run."""
    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    data = result.model_dump()
    if not getattr(result, "include_sample_values", False):
        # Ensure sample values are redacted in JSON if include_sample_values is False
        for tbl in data.get("table_results", []):
            d_res = tbl.get("data_result")
            if d_res and d_res.get("mismatch_samples"):
                for sample in d_res["mismatch_samples"]:
                    if sample.get("column_diffs"):
                        sample["column_diffs"] = {
                            col: ["[REDACTED]", "[REDACTED]"]
                            for col in sample["column_diffs"]
                        }

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)

    return out_file


def export_mismatches_csv(
    result: BatchValidationResult,
    output_path: Union[str, Path],
    include_sample_values: Optional[bool] = None,
) -> Path:
    """Export flattened CSV containing all row and column mismatches."""
    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    if include_sample_values is None:
        include_sample_values = getattr(result, "include_sample_values", False)

    fieldnames = [
        "table_name",
        "diff_type",
        "primary_keys",
        "column_name",
        "source_value",
        "target_value",
    ]

    with open(out_file, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for tbl in result.table_results:
            if tbl.data_result and tbl.data_result.mismatch_samples:
                for sample in tbl.data_result.mismatch_samples:
                    pk_str = json.dumps(sample.key_values)
                    if sample.column_diffs:
                        for col, (s_val, t_val) in sample.column_diffs.items():
                            src_val_out = str(s_val) if include_sample_values else "[REDACTED]"
                            tgt_val_out = str(t_val) if include_sample_values else "[REDACTED]"
                            writer.writerow(
                                {
                                    "table_name": tbl.table_name,
                                    "diff_type": sample.diff_type,
                                    "primary_keys": pk_str,
                                    "column_name": col,
                                    "source_value": src_val_out,
                                    "target_value": tgt_val_out,
                                }
                            )
                    else:
                        writer.writerow(
                            {
                                "table_name": tbl.table_name,
                                "diff_type": sample.diff_type,
                                "primary_keys": pk_str,
                                "column_name": "-",
                                "source_value": "-",
                                "target_value": "-",
                            }
                        )

    return out_file


def export_junit_xml(
    result: BatchValidationResult,
    output_path: Union[str, Path],
) -> Path:
    """Export standard JUnit XML test report for CI/CD pipeline integration."""
    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    testsuites = ET.Element("testsuites", name="SIT-Validation-Run")
    testsuite = ET.SubElement(
        testsuites,
        "testsuite",
        name=result.run_name,
        tests=str(result.total_tables),
        failures=str(result.failed_tables),
        errors=str(sum(1 for t in result.table_results if t.status == "ERROR")),
        skipped=str(result.skipped_tables),
        time=f"{result.total_duration_seconds:.3f}",
        timestamp=result.end_time_iso,
    )

    for tbl in result.table_results:
        testcase = ET.SubElement(
            testsuite,
            "testcase",
            classname=f"data_validator.sit.{result.run_name}",
            name=tbl.table_name,
            time=f"{tbl.execution_time_seconds:.3f}",
        )

        if tbl.status == "SKIPPED":
            ET.SubElement(testcase, "skipped", message="Skipped due to checkpoint resume")
        elif tbl.status == "ERROR":
            err = ET.SubElement(testcase, "error", message=tbl.error_message or "Execution error")
            err.text = tbl.error_message or "Unknown error"
        elif not tbl.passed or tbl.status == "FAILED":
            fail_msgs = []
            if tbl.count_result and not tbl.count_result.passed:
                fail_msgs.append(f"Count diff: {tbl.count_result.count_diff_pct:.2f}%")
            if tbl.schema_result and not tbl.schema_result.passed:
                fail_msgs.append(f"Schema mismatches: {tbl.schema_result.type_mismatch_count}")
            if tbl.data_result and not tbl.data_result.passed:
                fail_msgs.append(f"Differing rows: {tbl.data_result.differing_rows_count}")

            msg_str = "; ".join(fail_msgs) or "Validation failed"
            failure = ET.SubElement(testcase, "failure", message=msg_str)
            failure.text = msg_str

    tree = ET.ElementTree(testsuites)
    tree.write(str(out_file), encoding="utf-8", xml_declaration=True)
    return out_file
