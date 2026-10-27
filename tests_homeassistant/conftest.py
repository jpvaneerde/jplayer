import custom_components  # noqa: F401  (Home Assistant finds the integration through this package)
import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield
