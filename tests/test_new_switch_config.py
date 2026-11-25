"""The config per new switch, rendered from the plan (new_switch_config.py)."""

import re

from switch_migrator.config import Config, SshSettings
from switch_migrator.models import (
    BOTH,
    TAGGED,
    UNTAGGED,
    MltState,
    Platform,
    PortState,
    SwitchAudit,
    VlanBinding,
    VlanInfo,
)
from switch_migrator.new_switch_config import (
    UNVERIFIED_CVID_MLT,
    UNVERIFIED_LACP_MEMBERS,
    UNVERIFIED_MLT_ENCAP,
    render_all,
)
from switch_migrator.plan import Layout, make_plan

DATA, VOICE, SRV = 695, 735, 2200


def _b(vlan, isid, tagging, note=""):
    return VlanBinding(vlan=vlan, isid=isid, tagging=tagging, isid_note=note)


def _stack() -> SwitchAudit:
    def port(p, bindings, **kw):
        return PortState(port=p, oper_up=True, usage="IN USE", bindings=bindings, **kw)
    lag = [_b(SRV, 2502200, TAGGED)]
    ports = [
        # a desk: voice tagged, data untagged
        port("1/1", [_b(VOICE, 2510735, TAGGED), _b(DATA, 2500695, UNTAGGED)],
             name="desk-12"),
        port("1/2", [_b(100, None, UNTAGGED, "unresolved")]),      # needs a decision
        port("1/3", [_b(31, 1531100, TAGGED), _b(DATA, 2500695, UNTAGGED)]),
        port("1/4", [_b(DATA, 2500695, "")]),                       # tagging unknown
        port("1/5", lag, mlt_id=1, lacp=True), port("2/5", lag, mlt_id=1, lacp=True),
        port("1/6", [_b(DATA, 2500695, BOTH)], admin_up=False),
    ]
    mlts = [MltState(1, "srv-lag", members=["1/5", "2/5"], lacp=True, bindings=lag)]
    vlans = [VlanInfo(100, "Users Floor 1"), VlanInfo(DATA, "E010035016000_21")]
    return SwitchAudit("ers-1", "ers-1", Platform.ERS, reachable=True, ports=ports,
                       mlts=mlts, vlans=vlans)


def _cfg(**kw) -> Config:
    return Config([], [], [2500000, 2510000], kw.pop("explicit", {}), {1},
                  ssh=SshSettings(), **kw)


def _configs(cfg=None):
    audit = _stack()
    plan = make_plan([audit], ["leaf-a", "leaf-b"], Layout(ports=28, uplinks=4))
    return {c.switch: c for c in render_all([audit], plan, cfg or _cfg())}, plan


def _block(text: str, head: str) -> str:
    """The lines of one config block, from `head` to its `exit`."""
    start = text.index(head + "\n")
    return text[start:text.index("\nexit", start)]


def _on(block: str, line_start: str, port: str) -> bool:
    """Is `port` in the comma list of the block's line starting `line_start`?"""
    return any(line.startswith(line_start) and port in line[len(line_start):].split(",")
               for line in block.splitlines())


def test_a_desk_port_gets_its_services_on_the_new_port():
    configs, plan = _configs()
    new = plan.move_for("ers-1", "1/1")
    text = configs[new.new_switch].text
    assert _on(_block(text, "i-sid 2510735 elan"), f"c-vid {VOICE} port ", new.new_port)
    assert _on(_block(text, "i-sid 2500695 elan"), "untagged-traffic port ", new.new_port)
    # tagged traffic on it: phase I encapsulation, then phase II flex-UNI
    phase1 = text[text.index("PHASE I\n"):text.index("PHASE II")]
    assert f"interface GigabitEthernet {new.new_port}\nencapsulation dot1q" in phase1
    port = _block(text[text.index("PHASE II"):], f"interface GigabitEthernet {new.new_port}")
    assert port.splitlines()[1:] == ["default-vlan-id 0", 'name "desk-12"',
                                     "flex-uni enable", "no shutdown",
                                     "slpp-guard enable timeout 0",
                                     "spanning-tree mstp edge-port true"]


def test_an_smlt_gets_the_same_4xx_on_both_peers_with_its_lacp_and_service():
    configs, _ = _configs()
    for switch in ("leaf-a", "leaf-b"):
        text = configs[switch].text
        assert 'mlt 401 enable name "f11"' in text and "mlt 401 member 1/1" in text
        block = _block(text, "interface mlt 401")
        assert block.splitlines()[1:4] == ["smlt", "lacp enable key 401", "flex-uni enable"]
        assert "c-vid 2200 mlt 401" in _block(text, "i-sid 2502200 elan")
        assert {UNVERIFIED_CVID_MLT, UNVERIFIED_MLT_ENCAP, UNVERIFIED_LACP_MEMBERS} \
            <= set(configs[switch].unverified)
        assert "# UNVERIFIED" in text


def test_an_undecided_vlan_is_commented_out_and_asked_for():
    configs, plan = _configs()
    new = plan.move_for("ers-1", "1/2")
    result = configs[new.new_switch]
    assert [d.vlan_id for d in result.decisions] == [100]
    assert _on(result.text, "# untagged-traffic port ", new.new_port)
    assert "# [REVIEW] VLAN 100 (Users Floor 1)" in result.text


def test_your_decision_in_config_yaml_settles_it():
    configs, plan = _configs(_cfg(explicit={100: 2700100}))
    new = plan.move_for("ers-1", "1/2")
    result = configs[new.new_switch]
    assert result.decisions == []
    assert _on(_block(result.text, "i-sid 2700100 elan"), "untagged-traffic port ",
               new.new_port)


def test_reserved_vlans_and_unknown_tagging_are_said_not_guessed():
    configs, plan = _configs()
    text = configs[plan.move_for("ers-1", "1/3").new_switch].text
    assert "VLAN 31" in text and "reserved estate-wide" in text
    assert "i-sid 1531100" not in text
    unknown = plan.move_for("ers-1", "1/4")
    problems = configs[unknown.new_switch].problems
    assert any("did not say whether VLAN 695" in p for p in problems)


def test_an_admin_down_old_port_stays_shut_and_says_so():
    configs, plan = _configs()
    new = plan.move_for("ers-1", "1/6")
    port = _block(configs[new.new_switch].text.split("PHASE II")[1],
                  f"interface GigabitEthernet {new.new_port}")
    assert "shutdown" in port.splitlines() and "was admin-down" in port


def test_nothing_ztp_owns_can_appear():
    configs, _ = _configs()
    forbidden = re.compile(r"^(snmp|ip domain|ip name-server|logging|syslog|ssh|"
                           r"router isis|spbm|boot|sys |radius|vlan create|"
                           r"virtual-ist|isis)", re.IGNORECASE)
    for result in configs.values():
        for line in result.text.splitlines():
            if line.strip() and not line.startswith("#"):
                assert not forbidden.match(line), line


def test_one_untagged_service_per_port():
    audit = _stack()
    audit.ports[0].bindings.append(_b(VOICE, 2510735, UNTAGGED))
    plan = make_plan([audit], ["leaf-a", "leaf-b"], Layout(ports=28, uplinks=4))
    problems = [p for c in render_all([audit], plan, _cfg()) for p in c.problems]
    assert any("untagged traffic would land in I-SID" in p for p in problems)


def test_plan_writes_one_config_per_new_switch(tmp_path):
    import shutil
    from pathlib import Path

    from switch_migrator import cli

    raw = tmp_path / "raw"
    shutil.copytree(Path(__file__).parent / "fixtures" / "ers", raw / "old-ers")
    config = tmp_path / "config.yaml"
    config.write_text("core_switch_patterns: ['dvr-*']\n"
                      "isid_conventions: {offsets: [2500000, 2510000]}\n"
                      "plan: {ports: 28, uplinks: 4, port_lines: ['no spanning-tree mstp']}\n")
    out = tmp_path / "out"
    cli.main(["-c", str(config), "-s", "old-ers:ers", "--no-fabric", "--offline",
              str(raw), "--no-excel", "--plan", "--new-switch", "leaf-a,leaf-b",
              "-o", str(out)])
    new = out / "config" / "new"
    assert sorted(p.name for p in new.glob("*.cfg")) == ["leaf-a.cfg", "leaf-b.cfg"]
    assert "no spanning-tree mstp" in (new / "leaf-a.cfg").read_text()   # port_lines
    assert list(new.glob("*.isid-decisions.txt"))      # no fabric: every VLAN asks
