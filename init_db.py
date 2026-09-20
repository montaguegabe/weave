"""Seed the shared AST codebase database. Run once before launching agents."""

import os
import sys

from astvcs import VCS, VcsError

BASE = """\
class Item:
    def __init__(self, name, price):
        self.name = name
        self.price = price


def total_price(items):
    subtotal = sum(i.price for i in items)
    tax = subtotal * 0.08
    return subtotal + tax


def test_total_price():
    assert abs(total_price([Item('apple', 10.0)]) - 10.8) < 1e-9
    assert total_price([]) == 0
"""


def main():
    path = os.environ.get(
        "ASTDB_PATH", os.path.join(os.path.dirname(__file__), "codebase.db")
    )
    if "--fresh" in sys.argv:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(path + suffix)
            except FileNotFoundError:
                pass
    vcs = VCS(path)
    try:
        vcs.init_codebase(BASE)
        print(f"initialized {path} with base module:")
        print(vcs.tree("main").outline())
    except VcsError as e:
        print(f"skipped: {e}")


if __name__ == "__main__":
    main()
