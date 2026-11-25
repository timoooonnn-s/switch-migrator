"""Outputs: the report and the collected state, written to disk."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rich.console import Console

from switch_migrator.cabling_sheet import Sheet
from switch_migrator.config import Config
from switch_migrator.config_extract import extract_voss_config
from switch_migrator.config_generate import generate_voss_from_ers
from switch_migrator.isid import build_worksheet
from switch_migrator.models import Platform, SwitchAudit, VlanComparison
from switch_migrator.new_switch_config import render_all
from switch_migrator.parsers.ers_config import parse_ers_config
from switch_migrator.plan import MigrationPlan
from switch_migrator.report.excel import write_csv, write_excel
from switch_migrator.report.migration import build_commands
from switch_migrator.report.rack_sheet import build_rack_sheets
from switch_migrator.workflow.report import Report


@dataclass(frozen=True)
class OutputOptions:
    output_dir: Path
    stamp: str
    title: str = "migration-audit"   # the workbook's name: <title>-<stamp>.xlsx
    excel: bool = True
    csv: bool = False
    extract_config: bool = False     # config/<old switch>.cfg
    commands: bool = False           # migration-commands-<stamp>.txt (with the sheets)
    new_switch: str = ""             # the target named in the commands file


def write_outputs(report: Report, audits: list[SwitchAudit], cfg: Config,
                  opts: OutputOptions, console: Console) -> list[Path]:
    """Every file the report asks for, in a fixed order. Returns what was
    written, for the manifest and the handover bundle."""
    out = opts.output_dir
    written: list[Path] = []
    if opts.excel:
        path = out / f"{opts.title}-{opts.stamp}.xlsx"
        write_excel(report.tables, path)
        written.append(path)
    if opts.csv:
        written += write_csv(report.tables, out / f"csv-{opts.stamp}")
    if opts.extract_config:
        written += write_config_extracts(audits, out, report.comparisons, cfg, console)
    if report.plan is not None:
        written += write_new_switch_configs(audits, report.plan, cfg, out, console)
    if opts.commands:
        path = out / f"migration-commands-{opts.stamp}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(build_commands(audits, new_switch=opts.new_switch),
                        encoding="utf-8")
        written.append(path)
    return written


def write_new_switch_configs(audits: list[SwitchAudit], plan: MigrationPlan,
                             cfg: Config, output_dir: Path,
                             console: Console) -> list[Path]:
    """One config per new switch (+ an I-SID decision worksheet when a VLAN still
    needs one) into <output>/config/new/."""

    out_dir = output_dir / "config" / "new"
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for result in render_all(audits, plan, cfg):
        path = out_dir / f"{result.switch}.cfg"
        path.write_text(result.text, encoding="utf-8")
        written.append(path)
        if result.decisions:
            sheet = out_dir / f"{result.switch}.isid-decisions.txt"
            sheet.write_text(build_worksheet(result.decisions,
                                             device_name=result.switch),
                             encoding="utf-8")
            written.append(sheet)
        flags = []
        if result.decisions:
            flags.append(f"{len(result.decisions)} I-SID decision(s) needed")
        if result.problems:
            flags.append(f"{len(result.problems)} problem(s)")
        if result.unverified:
            flags.append(f"{len(result.unverified)} unverified line kind(s)")
        console.print(f"Config for {result.switch}: {path}"
                      + (f" [yellow]({', '.join(flags)})[/yellow]" if flags else ""))
    return written


def write_config_extracts(audits: list[SwitchAudit], output_dir: Path,
                          comparisons: dict[str, list[VlanComparison]], cfg: Config,
                          console: Console) -> list[Path]:
    """A migration-ready config per OLD switch (--extract-config): VOSS boxes
    are filtered/neutralized; ERS boxes are translated to VOSS flex-UNI with an
    I-SID decision worksheet."""
    written: list[Path] = []
    out_dir = output_dir / "config"
    for a in audits:
        if not a.running_config:
            if a.reachable:
                console.print(f"[yellow]--extract-config: no running-config "
                              f"obtained from {a.name}[/yellow]")
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        if a.platform is Platform.VOSS:
            text = extract_voss_config(a.running_config, device_name=a.name).text
            path = out_dir / f"{a.name}.cfg"
            path.write_text(text, encoding="utf-8")
            written.append(path)
        else:  # ERS -> VOSS flex-UNI (generated draft) + I-SID worksheet
            matched = {c.vlan_id: c.matched_isid
                       for c in comparisons.get(a.name, []) if c.matched_isid}
            model = parse_ers_config(a.running_config)
            res = generate_voss_from_ers(model, cfg, matched_by_vlan=matched,
                                         device_name=a.name)
            path = out_dir / f"{a.name}.cfg"
            path.write_text(res.text, encoding="utf-8")
            written.append(path)
            wpath = out_dir / f"{a.name}.isid-decisions.txt"
            wpath.write_text(build_worksheet(res.decisions, device_name=a.name),
                             encoding="utf-8")
            written.append(wpath)
    return written


def write_rack_sheet(sheet: Sheet, output_dir: Path) -> Path:
    """The rack sheet workbook, next to the other reports."""

    path = output_dir / f"rack-sheet-{datetime.now():%Y%m%d-%H%M%S}.xlsx"
    write_excel(build_rack_sheets(sheet), path)
    return path
