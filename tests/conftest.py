from pathlib import Path

import pytest


@pytest.fixture
def resources() -> Path:
    return Path(__file__).parent.absolute() / "resources"
