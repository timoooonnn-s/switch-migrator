"""Per-run manifest: what was collected, what failed, and what came out.

"We audited the switches before the migration" is worth more when it can be
shown afterwards. The manifest is that evidence in one small JSON file: the
tool version, when the run started and how long it took, which devices
answered, every command that was sent and whether the device accepted it, the
warnings and errors raised, and the files written.

It never contains credentials, and never the command OUTPUT - only the command
text and its outcome. The output belongs in the snapshot and the raw capture.
"""

from __future__ import annotations

import json
import platform as py_platform
import re
import sys
from datetime import datetime
from pathlib import Path

from switch_migrator import __version__
from switch_migrator.models import FabricState, SwitchAudit

# CLI options that carry no information worth recording
_SKIP_ARGS = {"debug", "verbose", "menu"}

# The options block is a copy of whatever the caller parsed, and this file is
# meant to be handed to other people. Credentials never reach argparse today -
# they come from the environment or a prompt - but a manifest that would print
# one if they ever did is a bad bet, so anything whose NAME reads like a secret
# is redacted rather than copied.
_SECRET_ARG = re.compile(r"pass|secret|token|credential|auth|key", re.IGNORECASE)


def _device_entry(audit: SwitchAudit, commands: list[dict]) -> dict:
    sent = len(commands)
    rejected = sum(1 for c in commands if not c["ok"])
    return {
        "name": audit.name,
        "host": audit.host,
        "platform": audit.platform.value,
        "reachable": audit.reachable,
        "uptime_days": audit.uptime_days,
        "counts": {
            "ports": len(audit.ports),
            "ports_up": audit.ports_up,
            "mlts": len(audit.mlts),
            "vlans": len(audit.vlans),
        },
        "commands": {
            "sent": sent,
            "rejected_or_failed": rejected,
            "detail": commands,
        },
        "errors": list(audit.errors),
        "warnings": list(audit.warnings),
    }


def build(audits: list[SwitchAudit], fabric: FabricState, args,
          started: datetime, finished: datetime, written: list[Path],
          commands_by_device: dict[str, list[dict]] | None = None,
          config_path: Path | None = None, exit_code: int | None = None) -> dict:
    commands_by_device = commands_by_device or {}
    options = {}
    for key, value in sorted(vars(args).items()):
        if key in _SKIP_ARGS or value in (None, False, [], ""):
            continue
        if _SECRET_ARG.search(key):
            options[key] = "***redacted***"
            continue
        options[key] = str(value) if isinstance(value, Path) else value
    return {
        "tool": "switch-migrator",
        "tool_version": __version__,
        "python": sys.version.split()[0],
        "host": py_platform.node(),
        "started": started.isoformat(timespec="seconds"),
        "finished": finished.isoformat(timespec="seconds"),
        "duration_seconds": round((finished - started).total_seconds(), 1),
        "config": str(config_path) if config_path else "",
        "options": options,
        "read_only": True,
        "fabric": {
            "controllers_ok": list(fabric.dvrs_ok),
            "controller_errors": list(fabric.dvr_errors),
            "isids": len(fabric.isids),
        },
        "switches": [_device_entry(a, commands_by_device.get(a.name, []))
                     for a in audits],
        "totals": {
            "switches": len(audits),
            "reachable": sum(1 for a in audits if a.reachable),
            "unreachable": sum(1 for a in audits if not a.reachable),
            "commands_sent": sum(len(c) for c in commands_by_device.values()),
            "commands_failed": sum(1 for c in commands_by_device.values()
                                   for r in c if not r["ok"]),
        },
        "files_written": [str(p) for p in written],
        "exit_code": exit_code,
    }


def write(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    return path
