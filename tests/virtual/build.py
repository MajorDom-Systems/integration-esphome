"""Compile every virtual device sketch: `poetry run python -m tests.virtual.build`."""

from tests.virtual.runner import SKETCHES, build

if __name__ == "__main__":
    for sketch in SKETCHES:
        build(sketch)
