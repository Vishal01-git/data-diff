"""HTML Report generator using self-contained Jinja2 template."""

from pathlib import Path
from typing import Union

from jinja2 import Environment, FileSystemLoader

from validator.orchestrator.runner import BatchValidationResult

TEMPLATES_DIR = Path(__file__).parent / "templates"


def generate_html_report(
    result: BatchValidationResult,
    output_path: Union[str, Path],
) -> Path:
    """Render the self-contained audit-ready SIT HTML report."""
    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
    )
    template = env.get_template("sit_report.html.j2")
    rendered_html = template.render(result=result)

    with open(out_file, "w", encoding="utf-8") as f:
        f.write(rendered_html)

    return out_file
