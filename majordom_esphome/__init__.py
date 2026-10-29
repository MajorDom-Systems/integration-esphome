from majordom_integration_sdk import Peripheral, Platform

from .controller import ESPhomeController

__all__ = ["ESPhomeController", "peripherals"]


def peripherals(platform: Platform) -> list[Peripheral]:
    return [ESPhomeController(platform)]
