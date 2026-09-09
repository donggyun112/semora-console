import pytest

from console import server


@pytest.fixture(autouse=True)
def _fresh_rate_window():
    """Give every test its own rate-limit window.

    The suite drives the metered endpoints far harder than any visitor will, all from one
    address, so without this the limiter starts answering 429 partway through a run and
    the failure looks like a bug in whatever test happened to be next. Clearing per test
    keeps the limiter switched on — the tests that care about it set their own cap.
    """
    server._hits.clear()
    yield
    server._hits.clear()
