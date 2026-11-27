REAL CAPTURES - Fabric Engine 9.4 (GNS3 lab)
============================================

Captured with fabric-engine-parser's tools/capture.py on 2026-11-22 from
FEGNS3.9.4.0.0.GA in privileged mode, and copied here from
`../../../fabric-engine-parser/tests/fixtures/`. Unlike voss_9x/ beside this
folder, nothing here is reconstructed: these are the bytes a 9.4 box sent.

Source switch: `fe-9.4-gns3-lab6-fe-01` (lab build 2 - MLT 10 a trunk LACP
MLT, MLT 20 an SMLT, no vIST, CVLAN I-SIDs on VLANs 40/60, an ELAN I-SID
30100, VRF blue). One file comes from a different capture of the same lab and
says so:

  show_mlt_members_not_up.txt   `fe-9.4-gns3-lab3-fe-01`, the same day. MLT 10
                                carries VLAN 50 while its members are
                                configured but not up, so its PORT MEMBERS
                                column is empty and its VLAN IDS column is
                                not. That pair is what the file is here for.

File names follow this directory's convention (every space and hyphen in the
command becomes '_'), so `show vlan i-sid` is `show_vlan_i_sid.txt`.

Why they are here
-----------------
9.x inserts columns that 8.x does not print - VLAN NAME in `show vlan i-sid`,
ISID NAME / AREA / AREA NAME in the SPBM tables, flag-slot ORIGIN in
`show i-sid` - and shifts the values after them. Every parser in
`switch_migrator/parsers/` had been written and tested against 8.x shapes
only, and four of them read a 9.x column as an 8.x one and produced WRONG
values rather than missing ones (see tests/test_firmware_variants.py).

The estate runs both, so a 9.x capture belongs in the test suite, not only in
the parser library's.
