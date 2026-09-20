"""Prove the speculative merge queue + dependency-hashed test cache.

The seed module carries a test that takes SUITE_S seconds, simulating slow QA.
Three agents commit disjoint changes concurrently: serial gating would cost
~3x SUITE_S of wall clock; speculation should land all three in ~1x. Then a
buggy commit races a good one: the bug is evicted, the good one lands, main
stays green. Finally, the dependency cache makes an untouched slow test free.
"""

import os
import tempfile
import threading
import time

from astvcs import VCS, run_source
from init_db import BASE

SUITE_S = 0.6
SLOW_BASE = BASE + (
    "\n\ndef test_slow_integration():\n"
    f"    __import__('time').sleep({SUITE_S})\n"
    "    assert total_price([Item('x', 1.0)]) > 1.0\n"
)


def worker(path, results, idx, ops, test_code=None):
    v = VCS(path)  # each thread gets its own connection
    t0 = time.time()
    r = v.commit_to_main(
        ops, f"agent-{idx}", f"tx-{idx}", test_code=test_code, base_rev=1
    )
    r["wall"] = round(time.time() - t0, 2)
    results[idx] = r


def main():
    path = os.path.join(tempfile.mkdtemp(), "codebase.db")
    VCS(path).init_codebase(SLOW_BASE)

    # --- 3 disjoint commits, concurrently --------------------------------
    results = {}
    fns = [
        [
            {
                "action": "append_child",
                "target": 1,
                "code": f"def helper_{i}(x):\n    return x + {i}",
            },
            {
                "action": "append_child",
                "target": 1,
                "code": f"def test_helper_{i}():\n    assert helper_{i}(1) == {1 + i}",
            },
        ]
        for i in range(3)
    ]
    t0 = time.time()
    threads = [
        threading.Thread(target=worker, args=(path, results, i, fns[i]))
        for i in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.time() - t0

    assert all(results[i]["status"] == "committed" for i in range(3)), results
    spec = [i for i in range(3) if results[i].get("speculative")]
    serial_estimate = 3 * SUITE_S
    print(
        f"3 concurrent disjoint commits: wall={wall:.2f}s "
        f"(serial gating would be >= {serial_estimate:.2f}s)"
    )
    print(f"  speculative landings: {spec or 'none (timing-dependent)'}")
    print(
        f"  per-commit: {[(i, results[i]['status'], results[i]['wall']) for i in range(3)]}"
    )
    assert wall < serial_estimate, "speculation should beat serial gating"

    v = VCS(path)
    ok, _ = run_source(v.tree("main").render())
    assert ok, "main must be green"
    for i in range(3):
        assert f"helper_{i}" in v.tree("main").render()

    # --- buggy commit races a good one ------------------------------------
    head = v._head("main")
    results2 = {}
    tax_node = next(
        i for i, n in v.tree("main").nodes.items() if "tax = subtotal" in n.header
    )
    bad = [{"action": "replace", "target": tax_node, "code": "tax = subtotal * 9"}]
    good = [
        {
            "action": "append_child",
            "target": 1,
            "code": "def helper_good(x):\n    return x * 2",
        }
    ]

    def worker2(idx, ops):
        vv = VCS(path)
        r = vv.commit_to_main(ops, f"racer-{idx}", f"race-{idx}", base_rev=head)
        results2[idx] = r

    ts = [
        threading.Thread(target=worker2, args=("bad", bad)),
        threading.Thread(target=worker2, args=("good", good)),
    ]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert results2["bad"]["status"] == "verification_failed", results2["bad"]
    assert results2["good"]["status"] == "committed", results2["good"]
    ok, _ = run_source(v.tree("main").render())
    assert ok and "helper_good" in v.tree("main").render()
    assert "tax = subtotal * 9" not in v.tree("main").render()
    print("race: buggy commit evicted, good commit landed, main green")

    # --- dependency-hash cache: untouched slow test is free ---------------
    head = v._head("main")
    t0 = time.time()
    r = v.commit_to_main(
        [
            {
                "action": "append_child",
                "target": 1,
                "code": "def helper_fast(x):\n    return x - 1",
            },
            {
                "action": "append_child",
                "target": 1,
                "code": "def test_helper_fast():\n    assert helper_fast(2) == 1",
            },
        ],
        "cached",
        "cache demo",
        base_rev=head,
    )
    cached_wall = time.time() - t0
    assert r["status"] == "committed"
    assert "cache-skipped" in r["verification"], r["verification"]
    assert "test_slow_integration" in r["verification"]
    print(
        f"cache: commit not touching slow test verified in {cached_wall:.2f}s "
        f"(suite nominally {SUITE_S}s) — {r['verification'].splitlines()[0]}"
    )
    assert cached_wall < SUITE_S, "slow test should have been cache-skipped"

    print(
        "\nqueue:",
        *[
            f"\n  #{q['ticket']} {q['author']:<9} {q['state']}"
            for q in reversed(v.queue_status())
        ],
    )
    print("\nALL SPECULATIVE-QA CHECKS PASSED")


if __name__ == "__main__":
    main()
