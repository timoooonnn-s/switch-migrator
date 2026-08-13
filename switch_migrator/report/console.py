"""Rich console rendering of the report tables."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table as RichTable

from switch_migrator.report.tables import Table

_STYLES = {"ok": "green", "warn": "yellow", "error": "bold red", None: ""}

# tables whose whole point is the exported file, not the terminal
_FILE_ONLY = ("Ports", "MLTs", "Fabric I-SIDs", "Port Info", "Cabling")


def render(tables: list[Table], console: Console | None = None,
           verbose: bool = False) -> None:
    console = console or Console()
    for table in tables:
        # Detail sheets live in the Excel/CSV export; keep the console focused
        # unless -v is given. The two migration sheets are here for the same
        # reason as Ports/MLTs - they are made to be printed and filled in, not
        # read in a shell.
        if not verbose and table.title in _FILE_ONLY:
            continue
        if not table.rows:
            if table.title == "Issues":
                console.print("\n[bold green]No issues found.[/bold green]")
            continue
        headers, rows = table.for_console()
        rich_table = RichTable(title=table.title, title_justify="left",
                               header_style="bold", expand=False)
        for header in headers:
            rich_table.add_column(header, overflow="fold")
        for row, severity in zip(rows, table.severities):
            rich_table.add_row(*[str(cell) for cell in row],
                               style=_STYLES.get(severity, ""))
        console.print()
        console.print(rich_table)
