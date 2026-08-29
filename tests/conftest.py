import pytest

from console.tools import reset_payment_ledgers


@pytest.fixture(autouse=True)
def _clean_payment_ledgers():
    reset_payment_ledgers()
    yield
    reset_payment_ledgers()
