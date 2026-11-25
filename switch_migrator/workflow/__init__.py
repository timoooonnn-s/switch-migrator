"""The pipeline both front ends drive (council decision D7).

    collect  -> report  -> outputs
    switches    compare,   files on disk
    fabric      sheets,
    MACs        plan, ...

`cli.py` turns flags into the option types defined here, `menu.py` turns a session
into the same types, and neither does any of the work itself. Nothing in this
package reads `argparse` or prompts a human: a step that needs a decision gets it
as an option.
"""
