"""Rich console rendering of the report tables."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table as RichTable

from switch_migrator.report.tables import Table

_STYLES = {"ok": "green", "warn": "yellow", "error": "bold red", None: ""}


def render(tables: list[Table], console: Console | None = None,
           verbose: bool = False) -> None:
    console = console or Console()
    for table in tables:
        # Ports/MLTs/Fabric detail lives in the Excel export; keep the console
        # focused unless -v is given.
        if not verbose and table.title in ("Ports", "MLTs", "Fabric I-SIDs"):
            continue
        if not table.rows:
            if table.title == "Issues":
                console.print("\n[bold green]No issues found.[/bold green]")
            continue
        rich_table = RichTable(title=table.title, title_justify="left",
                               header_style="bold", expand=False)
        for header in table.headers:
            rich_table.add_column(header, overflow="fold")
        for row, severity in zip(table.rows, table.severities):
            rich_table.add_row(*[str(cell) for cell in row],
                               style=_STYLES.get(severity, ""))
        console.print()
        console.print(rich_table)
