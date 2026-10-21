"""Enable loading custom_components/ in the Home Assistant test harness."""
import pytest

@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield
