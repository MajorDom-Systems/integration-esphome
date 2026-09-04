import pytest
import pytest_asyncio
from majordom_integration_sdk.testing import build_test_dependencies

from integration_template.controller import ESPhomeController


@pytest.fixture
def deps():
    return build_test_dependencies()


@pytest_asyncio.fixture
async def controller(deps):
    ctrl = ESPhomeController(deps)
    await ctrl.start()
    yield ctrl
    await ctrl.stop()
