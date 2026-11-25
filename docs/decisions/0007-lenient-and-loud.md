# 0007 Lenient parsing, loud about it; blind spots in every report

**Status:** accepted, 2026-11-23

**Context.** fabric-weft suggests `strict=True` for pre/post checks. In the window, one
unreadable line would then stop the whole comparison.

**Decision.** Parse leniently. Every skipped line, rejected command and missing file is
a *problem* attached to its device, printed at the top of the report, and sets exit
code 1. The report also always prints what the tool cannot see:

- MACs age out; a device that has not sent a frame since the change is `missing` until
  it does. Run `diff` again a few minutes later.
- MACs behind ELAN / Switched UNI endpoints are not in the VLAN FDB on Fabric Engine
  (printed when the after snapshot has ELAN endpoints).
- NAC: a device that fails authentication can show up in another VLAN or not at all.
