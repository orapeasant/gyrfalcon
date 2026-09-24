"""Gateway test fixtures. Helpers live in `_fakes.py` so test modules can import
them by name; two `conftest.py` files in a package-less tree cannot be."""

import pytest
from _fakes import FakeAgent


@pytest.fixture(autouse=True)
def _reset_fake_agent_counters():
    FakeAgent.active = FakeAgent.max_active = 0
    yield
