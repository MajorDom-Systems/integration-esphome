"""integration_template — rename this package to majordom_<protocol> (see README).

Exposes the integration's Controller, the entry point the Hub (or the SDK's standalone
dev runner) instantiates and drives.
"""

from integration_template.controller import ExampleController

__all__ = ["ExampleController"]
