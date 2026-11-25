"""Report: everything the collected state says, as tables - a pure function of
the collection plus options. No device is touched and no file written here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.console import Console

from switch_migrator import health, location, verify
from switch_migrator.cabling_sheet import Sheet
from switch_migrator.compare import compare_switch, resolve_binding_isids
from switch_migrator.config import Config
from switch_migrator.location import LocationRules
from switch_migrator.models import FabricState, SwitchAudit, VlanComparison
from switch_migrator.plan import Layout, MigrationPlan, Pins, PortKey, make_plan
from switch_migrator.report.migration import (
    assign_port_uids,
    build_cabling,
    build_cabling_by_location,
    build_plan_tables,
    build_port_info,
)
from switch_migrator.report.migration_tables import (
    build_health,
    build_health_summary,
    build_verification,
    build_verification_summary,
)
from switch_migrator.report.tables import Table, build_all


@dataclass(frozen=True)
class PlanSetup:
    layout: Layout
    pins: Pins = field(default_factory=dict)
    # the Port IDs of the sheet the pins came from: kept, so they stay stable
    uids: dict[PortKey, str] = field(default_factory=dict)


@dataclass
class ReportOptions:
    """Which sections the report has."""
    fabric_checked: bool = False            # compare against the fabric
    migration_sheets: bool = False
    new_switches: list[str] = field(default_factory=list)
    plan: PlanSetup | None = None           # with migration_sheets: plan the ports
    split: LocationRules | None = None      # one cabling tab per location group
    health: bool = False
    verify_sheet: Sheet | None = None       # check the moved links on this sheet


@dataclass
class Report:
    comparisons: dict[str, list[VlanComparison]] = field(default_factory=dict)
    # the tables, in the order they are written, grouped by what produced them
    sections: dict[str, list[Table]] = field(default_factory=dict)
    plan: MigrationPlan | None = None
    health: health.HealthReport | None = None
    verify: verify.VerifyReport | None = None

    @property
    def tables(self) -> list[Table]:
        return [t for tables in self.sections.values() for t in tables]

    def exit_code(self, audits: list[SwitchAudit], fabric: FabricState,
                  extra_failure: bool = False) -> int:
        """0 clean, 1 something a script must see: an error anywhere in the
        tables, an unreachable device, a DvR error, a health BLOCK, a failed
        link - or `extra_failure` (a MAC check that could not run)."""
        severities = [s for table in self.tables for s in table.severities]
        failed = ("error" in severities or any(not a.reachable for a in audits)
                  or bool(fabric.dvr_errors) or extra_failure)
        if self.health is not None and self.health.verdict == health.BLOCK:
            failed = True
        if self.verify is not None and not self.verify.ok:
            failed = True
        return 1 if failed else 0


def print_plan(plan: MigrationPlan, console: Console) -> None:
    for fill in plan.fill:
        console.print(f"Plan {fill.name}: {fill.dual} dual-homed + {fill.single} "
                      f"single-homed + {fill.pinned} pinned of {fill.access_ports} "
                      f"access ports, {fill.free} free")
    console.print(f"Plan: {len(plan.moves)} link(s) placed, "
                  f"{len(plan.not_moved)} not moved (see the 'Not moved' sheet)")
    for problem in plan.problems:
        console.print(f"  [bold yellow]![/bold yellow] {problem}")
    if plan.hints:
        console.print(f"  [dim]{len(plan.hints)} hint(s) on the 'Plan notes' sheet[/dim]")


def build_report(audits: list[SwitchAudit], fabric: FabricState, cfg: Config,
                 opts: ReportOptions, console: Console | None = None) -> Report:
    report = Report()
    report.comparisons = {} if not opts.fabric_checked else {
        a.name: compare_switch(a, fabric, cfg) for a in audits if a.reachable}
    # the comparison is the only thing that knows which I-SID a VLAN without a
    # local binding lands on - push that answer back into the port/MLT bindings
    # so the sheets and the new configs can use it
    resolve_binding_isids(audits, report.comparisons,
                          fabric_checked=opts.fabric_checked)
    report.sections["audit"] = build_all(audits, fabric, report.comparisons,
                                         no_fabric=not opts.fabric_checked)

    if opts.migration_sheets:
        assign_port_uids(audits, keep=opts.plan.uids if opts.plan else None)
        if opts.plan is not None:
            report.plan = make_plan(audits, opts.new_switches, opts.plan.layout,
                                    mlt_name=cfg.plan.mlt_name, pins=opts.plan.pins)
            if console is not None:
                print_plan(report.plan, console)
        sheets = [build_port_info(audits)]
        if opts.split is not None:
            # no combined sheet next to the split ones: this is a fill-in
            # document, and one link on two sheets means one set of answers
            # gets lost
            sheets += build_cabling_by_location(audits, opts.split,
                                                opts.new_switches, report.plan)
            if console is not None:
                console.print("Cabling sheet split by location:")
                for line in location.describe(opts.split, [a.name for a in audits]):
                    console.print(f"  {line}")
        else:
            sheets.append(build_cabling(audits, opts.new_switches, report.plan))
        if report.plan is not None:
            sheets += build_plan_tables(report.plan)
        report.sections["sheets"] = sheets

    if opts.health:
        report.health = health.check(audits, report.comparisons, cfg)
        report.sections["health"] = [build_health_summary(report.health),
                                     build_health(report.health)]

    if opts.verify_sheet is not None:
        report.verify = verify.verify(opts.verify_sheet, audits)
        extra = verify.unexpected_ports(opts.verify_sheet, audits)
        report.sections["verify"] = [build_verification_summary(report.verify, extra),
                                     build_verification(report.verify)]
    return report
