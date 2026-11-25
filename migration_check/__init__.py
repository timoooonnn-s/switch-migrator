"""migration-check: snapshot switches before and after a change, compare by MAC.

from migration_check.compare import compare, load
from migration_check.report import render_text

comparison = compare([load(Path("snapshots/pre"))], load(Path("snapshots/post")))
print(render_text(comparison))
"""

# One repository, one version (D14): this is switch-migrator's, which ships the
# package, and `tests/test_mac_versions.py` keeps the two and pyproject.toml in
# step. It goes into every snapshot's `snapshot.json` as `tool`, which says what
# wrote the folder; snapshot *compatibility* is `SNAPSHOT_FORMAT`, not this.
# migration_check still imports nothing from switch_migrator (decision 0001),
# which is why the string is repeated here instead of read from it.
__version__ = "1.0.0"
