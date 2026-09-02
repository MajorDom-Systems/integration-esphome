"""Basic tests that pass out of the box — they don't depend on protocol logic.

Kept separate from ``test_controller.py`` so the template repo's own CI (which excludes the
prefilled, expected-to-fail controller suite) still has tests to run and stays green.
"""

from integration_template import ExampleController


async def test_starts_and_stops(controller: ExampleController) -> None:
    await controller.start()
    await controller.stop()


async def test_name_and_slug(controller: ExampleController) -> None:
    assert controller.name == "Example"
    assert controller.name_slug == "example"
