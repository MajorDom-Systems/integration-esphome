"""Compile every virtual device sketch: `poetry run python -m tests.virtual.build`."""

from tests.virtual.runner import ENCRYPTED, PLAIN, build

if __name__ == "__main__":
    for sketch in (PLAIN, ENCRYPTED):
        build(sketch)
