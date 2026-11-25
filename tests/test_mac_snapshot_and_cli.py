from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from mac_check_samples import BOSS_MAC_SAMPLE, FE_LAB2, FE_LAB6
from migration_check.capture import (
    Target,
    capture,
    looks_rejected,
    parse_target,
    read_inventory,
)
from migration_check.cli import main
from migration_check.compare import load
from migration_check.model import Platform
from migration_check.platforms import boss
from migration_check.snapshot import (
    CommandStatus,
    SnapshotError,
    SnapshotWriter,
    command_slug,
    read_snapshot,
)


def test_slug_is_switch_migrators() -> None:
    def switch_migrator(command: str) -> str:  # connection.command_slug, copied
        return re.sub(r"[^a-z0-9]+", "_", command.lower()).strip("_")

    for command in ("show vlan mac-address-entry", "show interfaces gigabitEthernet i-sid"):
        assert command_slug(command) == switch_migrator(command)


def test_written_snapshot_reads_back(tmp_path: Path) -> None:
    writer = SnapshotWriter(tmp_path / "pre", label="pre")
    writer.add_device("old/access 01", Platform.BOSS, "192.0.2.10")
    writer.save("old/access 01", "show mac-address-table", BOSS_MAC_SAMPLE, CommandStatus.OK)
    writer.save(
        "old/access 01", "show mlt", "% Invalid input", CommandStatus.REJECTED, "% Invalid input"
    )
    writer.save("old/access 01", "show vlan", None, CommandStatus.FAILED, "timeout")
    writer.finish()

    snapshot = read_snapshot(tmp_path / "pre")
    device = snapshot.devices[0]
    assert (snapshot.label, device.name, device.platform, device.host) == (
        "pre",
        "old/access 01",
        Platform.BOSS,
        "192.0.2.10",
    )
    assert device.output("show mac-address-table") == BOSS_MAC_SAMPLE
    assert device.why_missing("show vlan") == "'show vlan' failed: timeout"
    assert device.why_missing("show interfaces") == "'show interfaces' not in the snapshot"


def test_a_snapshot_is_never_overwritten(tmp_path: Path) -> None:
    (tmp_path / "pre").mkdir()
    (tmp_path / "pre" / "something").write_text("x")
    with pytest.raises(SnapshotError, match="never overwritten"):
        SnapshotWriter(tmp_path / "pre")


def test_a_foreign_folder_needs_platform_only_when_files_do_not_tell(tmp_path: Path) -> None:
    (tmp_path / "sw").mkdir()
    (tmp_path / "sw" / "show_sys_info.txt").write_text("")
    with pytest.raises(SnapshotError, match="--platform"):
        read_snapshot(tmp_path)
    assert read_snapshot(tmp_path, Platform.BOSS).devices[0].platform is Platform.BOSS
    assert read_snapshot(FE_LAB2, Platform.BOSS).devices[0].platform is Platform.FABRIC_ENGINE


def test_diff_of_a_snapshot_with_itself_is_clean(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["diff", "--before", str(FE_LAB2), "--after", str(FE_LAB2)]) == 0
    assert "all 5 edge MACs seen after the change" in capsys.readouterr().out


def test_diff_lab2_to_lab6_real_rebuild(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # After the lab6 rebuild the end devices had not sent traffic yet: every edge MAC
    # of lab2 is missing. Real captures, and the ageing blind spot in action.
    out_json = tmp_path / "result.json"
    code = main(
        ["diff", "--before", str(FE_LAB2), "--after", str(FE_LAB6), "--json", str(out_json)]
    )
    text = capsys.readouterr().out
    assert code == 1
    assert "5 of 5 edge MACs not seen after the change" in text
    assert "fe-04 port 1/3: 1 MAC(s)" in text
    assert "00:50:79:66:68:01  VLAN 10    10.10.10.14" in text
    result = json.loads(out_json.read_text())
    assert result["summary"]["missing"] == 5
    assert result["summary"]["clean"] is False


def test_bad_options_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["diff", "--before", str(FE_LAB2), "--after", str(FE_LAB2), "--vlan-map", "10:20"])
    assert code == 2
    assert "expected OLD=NEW" in capsys.readouterr().err


def test_inventory_csv_and_device_option(tmp_path: Path) -> None:
    inventory = tmp_path / "switches.csv"
    inventory.write_text("name,host,platform\nold-access-01,192.0.2.10,ers\nleaf-01,,voss\n")
    targets = read_inventory(inventory)
    assert [(t.name, t.host, t.platform) for t in targets] == [
        ("old-access-01", "192.0.2.10", Platform.BOSS),
        ("leaf-01", "leaf-01", Platform.FABRIC_ENGINE),
    ]
    assert parse_target("192.0.2.11=old-02", Platform.BOSS).name == "old-02"


def test_rejection_is_recognised_behind_the_voss_banner() -> None:
    banner = "*" * 40 + "\n\tCommand Execution Time: Sun\n" + "*" * 40 + "\n"
    assert looks_rejected(banner + "% Invalid input detected\n") == "% Invalid input detected"
    assert looks_rejected(banner + "VLAN  STATUS\n") is None


class _FakeSession:
    def __init__(self, _: Target) -> None:
        self.closed = False

    def run(self, command: str) -> str:
        if command == "show mlt":
            raise TimeoutError("read timed out")
        if command == "show vlan":
            return "% Invalid input detected at '^' marker."
        return BOSS_MAC_SAMPLE if command == "show mac-address-table" else ""

    def close(self) -> None:
        self.closed = True


def test_capture_records_every_outcome_and_keeps_going(tmp_path: Path) -> None:
    sessions: list[_FakeSession] = []

    def connect(target: Target) -> _FakeSession:
        if target.name == "unreachable":
            raise ConnectionRefusedError("port 22")
        sessions.append(_FakeSession(target))
        return sessions[-1]

    targets = [parse_target("192.0.2.10=old-1", Platform.BOSS),
               parse_target("192.0.2.99=unreachable", Platform.BOSS)]  # fmt: skip
    log: list[str] = []
    capture(targets, tmp_path / "pre", connect, label="pre", log=log.append)

    meta = json.loads((tmp_path / "pre" / "snapshot.json").read_text())
    statuses = {c: e["status"] for c, e in meta["devices"]["old-1"]["commands"].items()}
    assert statuses == {
        boss.MAC_TABLE: "ok", boss.LLDP: "ok", boss.MLTS: "failed", boss.VLANS: "rejected",
        boss.PORTS: "ok",
    }  # fmt: skip
    unreachable = meta["devices"]["unreachable"]["commands"]
    assert {entry["status"] for entry in unreachable.values()} == {"failed"}
    assert sessions[0].closed
    assert "unreachable: cannot connect: port 22" in log
    assert load(tmp_path / "pre").devices[0].macs  # the rest of the switch still counts
