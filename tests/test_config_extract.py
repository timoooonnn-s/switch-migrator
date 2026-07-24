"""VOSS -> VOSS running-config extraction (slice 1)."""

from pathlib import Path

from switch_migrator.config_extract import (
    extract_voss_config,
    split_sections,
    _norm,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _cfg() -> str:
    return (FIXTURES / "voss" / "show_running_config.txt").read_text()


def test_split_sections_finds_banner_sections():
    names = {_norm(s.name) for s in split_sections(_cfg()) if s.name}
    assert "VLAN CONFIGURATION" in names
    assert "I-SID CONFIGURATION" in names
    assert "PORT CONFIGURATION - PHASE II" in names
    # the device header lines (# box type ...) are NOT a banner section
    assert "BOX TYPE" not in names


def test_extract_keeps_only_l2_service_sections():
    res = extract_voss_config(_cfg(), device_name="old-leaf-01")
    assert set(res.kept) == {
        "MLT CONFIGURATION", "VLAN CONFIGURATION", "MLT INTERFACE CONFIGURATION",
        "PORT CONFIGURATION - PHASE I", "PORT CONFIGURATION - PHASE II",
        "I-SID CONFIGURATION",
    }
    body = res.text
    # kept content is present verbatim
    assert 'mlt 197 enable name "srv-lag"' in body
    assert 'vlan create 4051 name "BVLAN-1" type spbm-bvlan' in body
    assert "i-sid 2500695 elan" in body
    assert "c-vid 695 port 1/4" in body
    assert "flex-uni enable" in body


def test_extract_drops_all_secrets_and_identity():
    res = extract_voss_config(_cfg(), device_name="old-leaf-01")
    # check the kept config BODY (everything after the comment header) - concrete
    # config lines/values, so the header's own explanatory text can't false-match
    body = "\n".join(l for l in res.text.splitlines() if not l.startswith("#"))
    for leak in ("supersecret", "secretuser", "f.30.42", "00bb.0000.0000",
                 'prompt "leaf-01"', "password hash sha1", "boot config flags",
                 "snmp-server", "radius server host"):
        assert leak not in body, f"leaked into kept config: {leak!r}"


def test_router_vlan_sections_are_not_mistaken_for_vlan_config():
    # 'OSPF VLAN CONFIGURATION' contains the word VLAN but must NOT be kept
    res = extract_voss_config(_cfg())
    assert "OSPF VLAN CONFIGURATION" not in res.kept
    assert "router ospf" not in res.text


def test_uplink_ports_are_annotated_access_ports_are_not():
    res = extract_voss_config(_cfg())
    lines = res.text.splitlines()
    # the IS-IS uplink 2/1 gets a REVIEW line immediately before its block
    idx = next(i for i, l in enumerate(lines)
               if l.strip() == "interface GigabitEthernet 2/1")
    assert "[REVIEW] fabric uplink" in lines[idx - 1]
    # the access port 1/4 (flex-uni, no isis) is NOT annotated
    access = [i for i, l in enumerate(lines)
              if l.strip() == "interface GigabitEthernet 1/4"]
    for i in access:
        assert "[REVIEW]" not in lines[i - 1]


def test_header_lists_omitted_sections_with_content():
    res = extract_voss_config(_cfg(), device_name="old-leaf-01")
    # sections that were present WITH content and dropped are listed for the human
    assert "RADIUS CONFIGURATION" in res.omitted_with_content
    assert "ISIS SPBM CONFIGURATION" in res.omitted_with_content
    # empty dropped sections (TACACS here) are not noise in the list
    assert "TACACS CONFIGURATION" not in res.omitted_with_content
    assert "Source box    : VSP-7254XSQ (VOSS 8.10.9.0)" in res.text
    assert "old-leaf-01" in res.text


def test_cli_extract_config_end_to_end(tmp_path):
    # --extract-config in --no-fabric offline mode writes a per-switch .cfg
    import shutil
    from switch_migrator.cli import main
    raw = tmp_path / "raw"
    shutil.copytree(FIXTURES / "voss", raw / "leaf-01")
    (tmp_path / "config.yaml").write_text("{}\n")
    out = tmp_path / "out"
    rc = main([
        "-c", str(tmp_path / "config.yaml"), "--no-fabric", "--offline", str(raw),
        "-s", "leaf-01:voss", "-o", str(out), "--no-excel", "--extract-config",
    ])
    assert rc == 0
    cfg = out / "config" / "leaf-01.cfg"
    assert cfg.is_file()
    text = cfg.read_text()
    assert "i-sid 2500695 elan" in text                 # kept service config
    assert "[REVIEW] fabric uplink" in text             # uplink annotated
    assert "supersecret" not in text                    # secret dropped
