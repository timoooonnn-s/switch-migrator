import os
from pathlib import Path

import pytest

from switch_migrator import paths

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session", autouse=True)
def tool_home(tmp_path_factory):
    """Every default path the tool resolves on its own points into a throwaway
    home directory for the whole test session (`switch_migrator.paths`): no
    test reads the developer's own config.yaml, and none writes into their home
    or into this checkout.
    """
    home = tmp_path_factory.mktemp("tool-home")
    before = os.environ.get(paths.HOME_ENV)
    os.environ[paths.HOME_ENV] = str(home)
    try:
        yield home
    finally:
        if before is None:
            os.environ.pop(paths.HOME_ENV, None)
        else:
            os.environ[paths.HOME_ENV] = before


@pytest.fixture
def fixture():
    def _load(platform: str, name: str) -> str:
        return (FIXTURES / platform / name).read_text()
    return _load
