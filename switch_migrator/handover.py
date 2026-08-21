"""Collect one run's output into a dated folder somebody else can read.

A migration leaves a trail across several files - the workbook, the commands,
the config extracts, the snapshot, the manifest - and each of them assumes you
know what the others are. That is fine for the engineer who produced them and
useless to everyone else: the senior engineer reviewing the plan, the technical
manager asking what this window will do, or you, six months later, opening the
folder for a site you last touched in spring.

The bundle is that folder: every file the run produced, copied (not moved - the
originals stay where they were), plus an index page that says what each one is
and shows the findings inline. The page is plain self-contained HTML with no
external assets, so it opens from a file share, an email attachment or a USB
stick, with no tool and no network.

It contains device data - hostnames, IPs, MAC addresses, and with the config
extracts the device configuration. Treat the folder like the switch output it
is made of.
"""

from __future__ import annotations

import html
import shutil
from datetime import datetime
from pathlib import Path

from switch_migrator import __version__
from switch_migrator.models import SwitchAudit
from switch_migrator.report.tables import Table

# What each file in the bundle is for, keyed by a distinguishing part of its
# name. Anything unmatched is still copied and listed, just without a blurb.
_DESCRIPTIONS = [
    ("migration-audit", "The workbook. Every sheet the run produced, "
                        "including the cabling sheet the technicians fill in."),
    ("inventory-report", "Port, MLT and VLAN state per switch, with no fabric "
                         "comparison."),
    ("migration-commands", "Commands to run on the NEW switch during the "
                           "window, and the config to apply."),
    ("snapshot", "The complete collected state as JSON. Every report in this "
                 "folder can be rebuilt from it without touching a switch."),
    ("manifest", "What the run actually did: every command sent to every "
                 "device and whether it was accepted. No command output, no "
                 "credentials."),
    ("csv-", "The same sheets as CSV, one file each."),
    (".csv", "One report sheet as CSV."),
    ("config/", "Per-device configuration: a neutralized extract (VOSS) or a "
                "generated flex-UNI draft plus its I-SID decisions (ERS)."),
]

# Which report tables are worth showing on the index page itself. The rest are
# in the workbook; these three are the ones somebody skims before opening it.
_INLINE_TABLES = ("Summary", "Coverage", "Issues")

_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 2.5rem 1.5rem 4rem;
       font: 15px/1.6 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       color: #16202a; background: #f7f8fa; }
main { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 1.9rem; margin: 0 0 .3rem; letter-spacing: -.02em; }
h2 { font-size: 1.15rem; margin: 2.4rem 0 .8rem; letter-spacing: -.01em; }
p.sub { margin: 0 0 1.8rem; color: #55636f; }
dl.facts { display: flex; flex-wrap: wrap; gap: 0; margin: 0 0 1rem;
           border: 1px solid #dbe1e6; border-radius: 3px; background: #fff;
           overflow: hidden; }
dl.facts > div { flex: 1 1 170px; padding: .7rem 1rem;
                 border-right: 1px solid #eceff1; }
dl.facts > div:last-child { border-right: 0; }
dt { font-size: .68rem; letter-spacing: .09em; text-transform: uppercase;
     color: #6b7883; margin-bottom: .15rem; }
dd { margin: 0; font-weight: 600; }
table { border-collapse: collapse; width: 100%; background: #fff;
        border: 1px solid #dbe1e6; font-size: .87rem; }
th { background: #1f3864; color: #fff; text-align: left; font-weight: 600;
     padding: .45rem .6rem; position: sticky; top: 0; }
td { padding: .38rem .6rem; border-top: 1px solid #eceff1;
     vertical-align: top; }
tr.ok td { background: #f2fbf4; }
tr.warn td { background: #fff8e6; }
tr.error td { background: #fdf0ef; }
.scroll { overflow-x: auto; border-radius: 3px; }
ul.files { list-style: none; padding: 0; margin: 0; border: 1px solid #dbe1e6;
           border-radius: 3px; background: #fff; }
ul.files li { padding: .7rem 1rem; border-top: 1px solid #eceff1; }
ul.files li:first-child { border-top: 0; }
ul.files a { font-weight: 600; color: #0d6d78; text-decoration: none; }
ul.files a:hover { text-decoration: underline; }
ul.files span { display: block; color: #55636f; font-size: .87rem; }
footer { margin-top: 3rem; color: #6b7883; font-size: .82rem;
         border-top: 1px solid #dbe1e6; padding-top: 1rem; }
@media (prefers-color-scheme: dark) {
  body { background: #12171d; color: #e4e9ee; }
  dl.facts, table, ul.files { background: #191f26; border-color: #2c3640; }
  dl.facts > div, ul.files li, td { border-color: #232b33; }
  p.sub, dt, ul.files span, footer { color: #93a1ad; }
  th { background: #16304a; }
  tr.ok td { background: #14251a; }
  tr.warn td { background: #2a2312; }
  tr.error td { background: #2b1a19; }
  ul.files a { color: #45b7bf; }
  footer { border-color: #2c3640; }
}
"""


def _describe(name: str) -> str:
    for marker, text in _DESCRIPTIONS:
        if marker in name:
            return text
    return ""


def _render_table(table: Table) -> str:
    head = "".join(f"<th>{html.escape(str(h))}</th>" for h in table.headers)
    body = []
    for row, severity in zip(table.rows, table.severities):
        cells = "".join(f"<td>{html.escape(str(c))}</td>" for c in row)
        body.append(f'<tr class="{html.escape(severity or "")}">{cells}</tr>')
    if not body:
        return "<p>Nothing to report.</p>"
    return (f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def _render_index(bundle: Path, files: list[Path], tables: list[Table],
                  audits: list[SwitchAudit], meta: dict) -> str:
    by_title = {t.title: t for t in tables}
    reachable = sum(1 for a in audits if a.reachable)
    errors = sum(len(a.errors) for a in audits)
    warnings = sum(len(a.warnings) for a in audits)

    facts = [
        ("Generated", meta.get("generated", "")),
        ("Switches", f"{reachable}/{len(audits)} reachable"),
        ("Errors", str(errors)),
        ("Warnings", str(warnings)),
        ("Tool version", __version__),
    ]
    if meta.get("config"):
        facts.append(("Config", Path(str(meta["config"])).name))

    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>Migration handover - {html.escape(bundle.name)}</title>",
        f"<style>{_STYLE}</style></head><body><main>",
        f"<h1>Migration handover</h1>",
        f'<p class="sub">{html.escape(bundle.name)} &mdash; everything one '
        f"switch-migrator run produced, with what each file is for. "
        f"Only <code>show</code> commands were sent; no device was changed.</p>",
        '<dl class="facts">',
    ]
    for label, value in facts:
        parts.append(f"<div><dt>{html.escape(label)}</dt>"
                     f"<dd>{html.escape(str(value))}</dd></div>")
    parts.append("</dl>")

    parts.append("<h2>Files</h2><ul class=\"files\">")
    for path in files:
        rel = path.name
        blurb = _describe(rel) or _describe(str(path))
        parts.append(
            f'<li><a href="{html.escape(rel)}">{html.escape(rel)}</a>'
            + (f"<span>{html.escape(blurb)}</span>" if blurb else "")
            + "</li>")
    parts.append("</ul>")

    for title in _INLINE_TABLES:
        table = by_title.get(title)
        if table is None:
            continue
        parts.append(f"<h2>{html.escape(title)}</h2>")
        parts.append(_render_table(table))

    parts.append(
        "<footer>Produced by switch-migrator. The workbook and the snapshot "
        "carry device data (hostnames, IP and MAC addresses, and with the "
        "config extracts the device configuration) - store this folder "
        "accordingly.</footer></main></body></html>")
    return "\n".join(parts)


def build(output_dir: Path, files: list[Path], tables: list[Table],
          audits: list[SwitchAudit], meta: dict | None = None,
          stamp: str | None = None) -> Path:
    """Copy this run's output into a dated folder with an index page.

    Returns the folder. Files are copied rather than moved, so an existing
    workflow that looks for them where they were written keeps working.
    """
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    bundle = output_dir / f"handover-{stamp}"
    bundle.mkdir(parents=True, exist_ok=True)

    config_dir = output_dir / "config"
    copied: list[Path] = []
    for path in files:
        if not path.exists():
            continue
        # the per-device extracts are copied once, with the directory below
        if config_dir in path.parents:
            continue
        target = bundle / path.name
        if path.is_dir():
            # a directory of CSVs; copy it whole, replacing any earlier attempt
            shutil.copytree(path, target, dirs_exist_ok=True)
        else:
            shutil.copy2(path, target)
        copied.append(target)

    # the per-device config extracts live in their own subdirectory
    if config_dir.is_dir():
        target = bundle / "config"
        shutil.copytree(config_dir, target, dirs_exist_ok=True)
        copied.append(target)

    full_meta = {"generated": datetime.now().isoformat(timespec="seconds")}
    full_meta.update(meta or {})
    index = bundle / "index.html"
    index.write_text(_render_index(bundle, copied, tables, audits, full_meta),
                     encoding="utf-8")
    return bundle
