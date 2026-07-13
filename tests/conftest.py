from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture():
    def _load(platform: str, name: str) -> str:
        return (FIXTURES / platform / name).read_text()
    return _load
