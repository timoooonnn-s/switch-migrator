from switch_migrator.parsers import voss_parsers
from switch_migrator.parsers.common import name_says_ist, parse_lldp_neighbors


def test_parse_ports(fixture):
    # full `show interfaces gigabitEthernet` output with Port Interface,
    # Port Name and Port Config sections - only the first may be parsed
    ports = voss_parsers.parse_ports(
        fixture("voss", "show_interfaces_gigabitethernet.txt"))
    assert len(ports) == 5
    by_port = {p.port: p for p in ports}
    assert by_port["1/1"].admin_up is True and by_port["1/1"].oper_up is True
    assert by_port["1/2"].admin_up is True and by_port["1/2"].oper_up is False
    assert by_port["1/48"].admin_up is False
    assert by_port["2/1/1"].oper_up is True  # channelized port
    # descriptions come from the Port Interface section, not Port Name
    assert by_port["1/1"].description == "1000BaseTX"


def test_parse_ports_without_section_banner(fixture):
    # partial capture: just the data lines, no banners -> still parses
    full = fixture("voss", "show_interfaces_gigabitethernet.txt")
    section = full.split("Port Name")[0]
    data_only = "\n".join(l for l in section.splitlines() if l[:1].isdigit())
    ports = voss_parsers.parse_ports(data_only)
    assert len(ports) == 5


def test_parse_mlt(fixture):
    # full real-world output: 4 tables, footers, wrapped VLAN IDS column
    mlts = voss_parsers.parse_mlt(fixture("voss", "show_mlt.txt"))
    # exactly one entry per MLT: the LACP / port-members / ENCAP tables and
    # the 'All N out of M' footers must not create extra or duplicate rows
    assert [m.mlt_id for m in mlts] == [1, 2, 10]
    ist = mlts[0]
    assert ist.is_ist
    assert ist.members == ["1/47", "1/48"]
    assert mlts[1].name == "MLT002.s.1/1/2"  # names may contain slashes
    assert mlts[1].members == ["1/1", "1/2"]  # trailing VLAN IDS column ignored
    assert mlts[1].admin == "smlt"
    assert mlts[2].mlt_type == "access"
    assert mlts[2].members == ["2/1/1"]


def test_parse_mlt_wrapped_vlan_ids_continuation():
    # a wrapped VLAN IDS continuation line must never become an MLT,
    # regardless of how many VLAN ids it carries
    output = (
        "38  6181  s129         trunk   smlt   smlt     1/29              174 695 735\n"
        "2246 2901 2952\n"
        "2600\n"
    )
    mlts = voss_parsers.parse_mlt(output)
    assert [m.mlt_id for m in mlts] == [38]
    assert mlts[0].members == ["1/29"]


def test_parse_port_state(fixture):
    ports = voss_parsers.parse_port_state(
        fixture("voss", "show_interfaces_gigabitethernet_state.txt"))
    assert len(ports) == 5
    by_port = {p.port: p for p in ports}
    assert by_port["1/1"].admin_up is True and by_port["1/1"].oper_up is True
    assert by_port["1/2"].admin_up is True and by_port["1/2"].oper_up is False
    assert by_port["1/48"].admin_up is False
    assert by_port["1/48"].state_reason == "SSH"
    assert by_port["1/47"].state_reason == ""
    assert by_port["2/1/1"].oper_up is True


def test_parse_port_isid(fixture):
    """Traditional `vlan i-sid` model: C-VID is N/A, so the platform VLAN IS
    the customer VLAN and stays the answer."""
    rows = voss_parsers.parse_port_isid(
        fixture("voss", "show_interfaces_gigabitethernet_i_sid.txt"))
    assert [(r["port"], r["isid"], r["vlan"]) for r in rows] == [
        ("1/1", 10100, 100),
        ("1/2", 10200, 200),
        ("2/1/1", 77777, 300),          # CVLAN row
    ]
    assert all(r["cvid"] is None for r in rows)
    assert [r["platform_vlan"] for r in rows] == [100, 200, 300]


_PORT_ISID_HEAD = (
    "PORTNUM IFINDEX ID       VLANID C-VID  TYPE     ORIGIN     NAME\n"
    "-------------------------------------------------------------\n")


def test_the_cvid_is_the_vlan_not_the_platform_vlan():
    """The one the end device actually tags is the one that has to exist on
    the new switch. On a flex-UNI leaf the platform VLAN is an internal number
    no host has ever sent, and putting it on the cabling sheet makes a
    technician recreate the wrong VLAN."""
    row = voss_parsers.parse_port_isid(
        _PORT_ISID_HEAD +
        "1/4     195     2500695  4048   c695   ELAN     C  ---      svc-695\n"
    )[0]
    assert row["vlan"] == 695            # what the host tags
    assert row["cvid"] == 695
    assert row["untagged"] is False
    assert row["platform_vlan"] == 4048  # kept, but never the answer


def test_an_untagged_port_reports_no_vlan_at_all():
    """'u' means the port is added untagged - nothing is tagged on the wire,
    so naming a VLAN would put one on the sheet that is not on the link. The
    platform VLAN in particular must not leak out as if it were one."""
    row = voss_parsers.parse_port_isid(
        _PORT_ISID_HEAD +
        "1/5     196     2510735  4049   u      ELAN     C  ---      svc-735\n"
    )[0]
    assert row["untagged"] is True
    assert row["vlan"] is None and row["cvid"] is None
    assert row["isid"] == 2510735        # the service is still known
    assert row["platform_vlan"] == 4049


def test_the_cvid_cell_is_read_in_every_shape_the_releases_print():
    rows = voss_parsers.parse_port_isid(
        _PORT_ISID_HEAD +
        "1/4     195     2500695  4048   c695     ELAN   C  ---      a\n"
        "1/6     197     2500696  4050   c696:1/6 ELAN   C  ---      b\n"
        "1/7     198     2500697  N/A    697      ELAN   C  ---      c\n"
        "1/8     199     2500698  N/A    N/A      ELAN   C  ---      d\n")
    assert [r["cvid"] for r in rows] == [695, 696, 697, None]
    assert [r["vlan"] for r in rows] == [695, 696, 697, None]
    assert not any(r["untagged"] for r in rows)


def test_port_isid_row_without_either_vlan_has_none():
    out = ("PORTNUM IFINDEX ID      VLANID C-VID TYPE   ORIGIN\n"
           "1/9     200     2500695 N/A    N/A   ELAN   C  ---\n")
    row = voss_parsers.parse_port_isid(out)[0]
    assert row["vlan"] is None and row["isid"] == 2500695


def test_parse_virtual_ist(fixture):
    ist = voss_parsers.parse_virtual_ist(fixture("voss", "show_virtual_ist.txt"))
    assert ist is not None
    assert ist.peer_ip == "192.168.255.2"
    assert ist.vlan == 4000
    assert ist.enabled is True
    assert ist.session_up is True


def test_parse_virtual_ist_absent():
    assert voss_parsers.parse_virtual_ist("no data") is None


def test_parse_vlan_isid(fixture):
    vlans = voss_parsers.parse_vlan_isid(fixture("voss", "show_vlan_i_sid.txt"))
    by_id = {v.vlan_id: v for v in vlans}
    # footer '5 out of 5 Total ...' (real format, no leading 'All') must not
    # become VLAN 5
    assert set(by_id) == {1, 100, 200, 300, 4000}
    assert by_id[100].isid == 10100
    # the third column is the I-SID name, kept separate from the VLAN name
    # (which comes from 'show vlan basic')
    assert by_id[100].isid_name == "Server-VLAN-100"
    assert by_id[100].name == ""
    assert by_id[200].isid == 10200
    assert by_id[300].isid == 77777
    assert by_id[300].isid_name == "quarantaine"
    assert by_id[1].isid is None
    assert by_id[4000].isid is None


def test_parse_isid_local(fixture):
    isids = voss_parsers.parse_isid_local(fixture("voss", "show_i_sid.txt"))
    # footer '4 out of 4 Total ...' must not become I-SID 4
    assert set(isids) == {10100, 10200, 20300, 2502201}
    assert isids[10100]["cvids"] == {100}
    assert isids[10200]["cvids"] == {200}
    assert isids[20300]["cvids"] == {300}
    assert isids[10100]["name"] == "Server-100"
    # CVLAN type rows and lowercase names must work
    assert isids[2502201]["cvids"] == {2201}
    assert isids[2502201]["name"] == "quarantaine"


def test_parse_isid_local_unknown_type_and_vlanid_column():
    # future/unknown TYPE values must still anchor a new I-SID (never leak
    # endpoints into the previous one), and the VLANID column of newer
    # releases must be harvested; names starting with 'c' are real names
    output = (
        "10100    ELAN       100    c100:1/10           CONFIG   Server-100\n"
        "99999    FANCYNEW   555    -                   CONFIG   cvlan-names-ok\n"
        "3 out of 3 Total Num of i-sids displayed\n"
    )
    isids = voss_parsers.parse_isid_local(output)
    assert set(isids) == {10100, 99999}
    assert isids[10100]["cvids"] == {100}
    assert isids[99999]["cvids"] == {555}
    assert isids[99999]["name"] == "cvlan-names-ok"


def test_parse_isis_spbm_isid(fixture):
    rows = voss_parsers.parse_isis_spbm_isid(
        fixture("voss", "show_isis_spbm_i_sid_all.txt"))
    assert len(rows) == 5
    assert {r["isid"] for r in rows} == {10100, 10200, 20300, 20400}
    assert rows[0] == {"isid": 10100, "type": "config", "host": "dvr-01"}
    assert rows[1] == {"isid": 10100, "type": "discover", "host": "beb-07"}


def test_parse_dvr_interfaces(fixture):
    rows = voss_parsers.parse_dvr_interfaces(
        fixture("voss", "show_dvr_interfaces.txt"))
    assert rows == [
        {"l2isid": 10100, "vlan": 100},
        {"l2isid": 10200, "vlan": 200},
        {"l2isid": 1501050, "vlan": 1050},  # L3VSN row: L3ISID 55501, VRF 5
    ]


def test_parse_lldp_neighbors_voss(fixture):
    neighbors = parse_lldp_neighbors(fixture("voss", "show_lldp_neighbor.txt"))
    assert {p: n.sysname for p, n in neighbors.items()} == {
        "1/1": "core-01", "1/47": "old-agg-02"}


def test_parse_lldp_neighbors_captures_ip_name_and_descr():
    # real VOSS 8.10.9 block form: NIC advertises an adapter model as SysName
    # (no IP), a server has an EMPTY SysName but an IP + SysDescr, and a switch
    # neighbor has all three. Two blocks on one port: the named one wins.
    from switch_migrator.parsers.common import parse_lldp_neighbors as p
    out = (
        "Port: 1/3       Index    : 17\n"
        "                SysName  : 10/25Gb 2-port SFP28 BCM57414 OCP3 Adapter fw_version:AFW_1\n"
        "                PortDescr: NIC 1/10/25Gb\n"
        "                SysDescr : 235.1.164.2 fw_version:AFW_1\n"
        "                Address  : 0.0.0.0\n"
        "           IPv6 Address  : 0:0:0:0:0:0:0:0\n"
        "Port: 1/7       Index    : 6\n"
        "                SysName  :\n"
        "                PortDescr: Embedded ALOM, Port 1\n"
        "                SysDescr : HPE ProLiant DL380 Gen10\n"
        "                Address  : 10.0.48.192\n"
        "Port: 1/9       Index    : 2\n"
        "                SysName  :\n"
        "                Address  : 0.0.0.0\n"
        "Port: 1/9       Index    : 18\n"
        "                SysName  : esx-host-01\n"
        "                SysDescr : VMware ESXi\n"
        "                Address  : 0.0.0.0\n"
        "Port: 2/1       Index    : 14\n"
        "                SysName  : core-s72-q4\n"
        "                SysDescr : VSP-7254XSQ (8.10.9.0)\n"
        "                Address  : 10.0.148.88\n"
    )
    n = p(out)
    assert n["1/3"].sysname.startswith("10/25Gb 2-port SFP28") and n["1/3"].ip == ""
    assert n["1/7"].sysname == "" and n["1/7"].ip == "10.0.48.192"
    assert n["1/7"].sys_descr == "HPE ProLiant DL380 Gen10"
    # port 1/9 has two blocks - the one advertising a name wins, no field mixing
    assert n["1/9"].sysname == "esx-host-01"
    assert n["2/1"].sysname == "core-s72-q4" and n["2/1"].ip == "10.0.148.88"


def test_parse_mlt_in_datapath(fixture):
    # the data-path table (LOCAL / LOCAL & REMOTE) is parsed onto each MLT so we
    # have a liveness signal even when no per-port state can be read
    mlts = {m.mlt_id: m for m in
            voss_parsers.parse_mlt(fixture("voss", "show_mlt.txt"))}
    assert mlts[1].in_datapath is True    # LOCAL & REMOTE
    assert mlts[10].in_datapath is True   # LOCAL


def test_parse_mlt_datapath_helper_reads_only_its_own_table():
    out = (
        "MLTID NAME   CREATED    PORT MEMBERS   PORT MEMBERS   IN DATA PATH\n"
        "-------------------------------------------------------------------\n"
        "1    a  LOC & REM  1/1  1/1  LOCAL\n"
        "2    b  LOC ONLY   1/2  -    NONE\n"
        "3    c  LOC & REM  1/3  1/3  LOCAL & REMOTE\n"
        "\nAll 3 out of 3 Total Num of mlt displayed\n"
        # a following table whose last column is up/down must NOT be misread
        "MLTID IFINDEX PORTS ADMIN OPER\n"
        "1  6144  1/1  enable  up\n"
    )
    assert voss_parsers._parse_mlt_datapath(out) == {1: True, 2: False, 3: True}


def test_parse_vlan_members(fixture):
    m = voss_parsers.parse_vlan_members(fixture("voss", "show_vlan_members.txt"))
    assert m[1] == []                                   # NONE -> no ports
    assert m[100] == ["1/1", "1/2", "2/1/1"]            # PORT MEMBER col, channelized
    assert m[200] == ["1/2", "1/47", "1/48"]
    assert m[4000] == ["1/47", "1/48"]
    # the 'N out of N Total' footer must not become a phantom VLAN
    assert 5 not in m or m[5] != []


# ---------------------------------------------------------------------------
# table-identification regressions in `show mlt`
# ---------------------------------------------------------------------------

# same shape as the field capture, but MLT 1 is NAMED with 'LACP' in it and the
# uplink is named 'dist-uplink' - two ordinary names that used to poison the
# parse
_MLT_TRICKY_NAMES = """\
====================================================================================================
                                    Mlt Info
====================================================================================================
                        PORT    MLT   MLT        PORT         VLAN
MLTID IFINDEX NAME      TYPE   ADMIN CURRENT    MEMBERS       IDS
-----------------------------------------------------------------------------------------------------------
1   6144  LACP-esx01   trunk   norm   norm     1/8,1/16          4051 4052
196 6339  dist-uplink  trunk   smlt   smlt     1/10              735

All 2 out of 2 Total Num of mlt displayed

               DESIGNATED   LACP      LACP
MLTID IFINDEX  PORTS        ADMIN     OPER
-----------------------------------------------------------------------------------------------------------
1      6144    1/8           disable   down
196    6339    1/10          enable      up

All 2 out of 2 Total Num of mlt displayed

                                                            WHICH PORTS
             WHERE      LOCAL             REMOTE            PROGRAMMED
MLTID NAME   CREATED    PORT MEMBERS      PORT MEMBERS      IN DATA PATH
-----------------------------------------------------------------------------------------------------------
1    LACP-esx01 LOC & REM  1/8,1/16          1/8,1/16          LOCAL
196  dist-uplink LOC & REM  1/10             1/10              LOCAL & REMOTE

All 2 out of 2 Total Num of mlt displayed

               ENCAP                          PVLAN        VID
MLTID IFINDEX  DOT1Q     LOSSLESS   PVLAN     TYPE         TYPE         FLEX-UNI
-----------------------------------------------------------------------------------------------------------
1     6144     enable    disable    disable   -            -            disable
196   6339     disable   disable    disable   -            -            disable

All 2 out of 2 Total Num of mlt displayed
"""


def test_mlt_name_containing_lacp_does_not_make_the_encap_table_the_lacp_table():
    """An MLT *named* '...LACP...' appears as a data row in the Mlt Info and
    data-path tables. Remembering that the word appeared made the parser read
    the NEXT header (ENCAP DOT1Q, also enable/disable) as the LACP table, so
    the DOT1Q state was recorded as the LACP state - here exactly inverted."""
    lacp = voss_parsers.parse_mlt_lacp(_MLT_TRICKY_NAMES)
    assert lacp == {1: False, 196: True}


def test_mlt_name_containing_ist_is_not_an_ist_peer_link():
    mlts = {m.mlt_id: m for m in voss_parsers.parse_mlt(_MLT_TRICKY_NAMES)}
    assert mlts[196].name == "dist-uplink"
    assert not mlts[196].is_ist          # 'dist' is not 'ist'
    assert not mlts[1].is_ist


def test_real_ist_names_are_still_recognised():
    for name in ("vIST", "MLT-IST", "ist", "ist-peer", "core_vist_1"):
        assert name_says_ist(name), name
    for name in ("dist-uplink", "twist", "sister", "distribution", ""):
        assert not name_says_ist(name), name


# ---------------------------------------------------------------------------
# `show vlan basic` footer
# ---------------------------------------------------------------------------

def test_vlan_basic_footer_without_leading_all_is_not_a_vlan():
    out = """\
VLAN                                MSTP
ID    NAME             TYPE         INST_ID PROTOCOLID   SUBNETADDR
------------------------------------------------------------------------
1     Default          byPort       0       none         N/A
99    quarantine       byPort       0       none         N/A
4051  BVLAN-1          spbm-bvlan   62      none         N/A

39 out of 39 Total Num of Vlans displayed
"""
    names = voss_parsers.parse_vlan_basic(out)
    assert names == {1: "Default", 99: "quarantine", 4051: "BVLAN-1"}
    assert 39 not in names          # was VLAN 39 named 'out'


# ---------------------------------------------------------------------------
# the DESCRIPTION column of the Port Interface table
# ---------------------------------------------------------------------------

def test_port_interface_description_is_media_or_empty_never_a_shifted_column():
    out = """\
                                      Port Interface
PORT                               LINK  PORT           PHYSICAL          STATUS
NUM      INDEX DESCRIPTION         TRAP  LOCK     MTU   ADDRESS           ADMIN  OPERATE
------------------------------------------------------------------------------------------
1/1      192   10GbSR              true  false    1950  b0:ad:aa:41:b4:01 up     up
1/9      200                       true  false    1950  b0:ad:aa:41:b4:09 down   down
"""
    ports = {p.port: p for p in voss_parsers.parse_ports(out)}
    assert ports["1/1"].description == "10GbSR"
    assert ports["1/9"].description == ""     # blank media, not 'true'


# ---------------- endpoints in `show i-sid`: c<vid> and u ------------------

_ISID_WITH_UNTAGGED = """\
ISID                PORT               MLT
ID       TYPE       INTERFACES         INTERFACES         ORIGIN     ISID NAME
------------------------------------------------------------------------------
2500695  ELAN       c695:1/10,u:1/36   c695:2             CONFIG     svc-695
2510735  ELAN       u:1/12             -                  CONFIG     svc-735
2502201  CVLAN      c2201:1/36         -                  CONFIG     quarantaine

c: customer vid   u: untagged-traffic

3 out of 3 Total Num of i-sids displayed
"""


def test_untagged_endpoints_are_read_not_ignored():
    """The device's own legend says 'u: untagged-traffic'. A port put into a
    service untagged used to contribute nothing at all, so the service looked
    like it had no endpoint there."""
    result = voss_parsers.parse_isid_local(_ISID_WITH_UNTAGGED)
    assert result[2500695]["untagged"] == {"1/36"}
    assert result[2510735]["untagged"] == {"1/12"}
    assert result[2502201]["untagged"] == set()


def test_an_untagged_endpoint_contributes_no_customer_vlan():
    """Untagged means the host tags nothing - there is no c-vid to record.
    Inventing one would put a VLAN on the sheet that is not on the wire."""
    result = voss_parsers.parse_isid_local(_ISID_WITH_UNTAGGED)
    assert result[2500695]["cvids"] == {695}      # from c695:, not from u:
    assert result[2510735]["cvids"] == set()      # untagged only


def test_the_legend_line_is_not_read_as_data():
    result = voss_parsers.parse_isid_local(_ISID_WITH_UNTAGGED)
    assert set(result) == {2500695, 2510735, 2502201}
    assert result[2500695]["name"] == "svc-695"
