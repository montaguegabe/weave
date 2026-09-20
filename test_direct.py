"""Exercise the direct-to-main model: concurrent features A, B, C where B is
buggy. Verification runs at commit time against the exact landing state, so
A and C land, B is rejected, and main is green at every revision."""

import os
import tempfile

from astvcs import VCS
from init_db import BASE


def nid(vcs, needle, branch="main"):
    tree = vcs.tree(branch)
    hits = [i for i, n in tree.nodes.items() if needle in n.header and tree.alive(i)]
    assert len(hits) == 1, (needle, hits)
    return hits[0]


def main():
    path = os.path.join(tempfile.mkdtemp(), "codebase.db")
    vcs = VCS(path)
    rev0 = vcs.init_codebase(BASE)

    # All three "agents" read main at rev0 and work concurrently.

    # --- A: item_count helper + its test -> lands ---------------------------
    a = vcs.commit_to_main(
        [
            {
                "action": "append_child",
                "target": 1,
                "code": "def item_count(items):\n    return len(items)",
            },
            {
                "action": "append_child",
                "target": 1,
                "code": "def test_item_count():\n    assert item_count([1, 2]) == 2",
            },
        ],
        author="A",
        message="item_count",
        base_rev=rev0,
        test_code="assert item_count([]) == 0",
    )
    assert a["status"] == "committed", a
    print(f"A: committed rev {a['rev']}  ({a['verification']})")

    # --- B: buggy tax change (breaks stored test_total_price) -> rejected ---
    b = vcs.commit_to_main(
        [
            {
                "action": "replace",
                "target": nid(vcs, "tax = subtotal"),
                "code": "tax = subtotal * 8",
            }
        ],  # bug: 8 instead of 0.08
        author="B",
        message="tax tweak",
        base_rev=rev0,
    )
    assert b["status"] == "verification_failed", b
    assert vcs._head("main") == a["rev"], "B must not have landed"
    print(f"B: rejected ({b['status']}); main still at rev {a['rev']}")
    print(
        "   failure output includes stored suite:",
        "test_total_price" in b["output"] or "AssertionError" in b["output"],
    )

    # --- C: disjoint change from the same stale rev0 -> lands ---------------
    c = vcs.commit_to_main(
        [
            {
                "action": "replace",
                "target": nid(vcs, "self.name"),
                "code": "self.name = name.strip()",
            },
            {
                "action": "append_child",
                "target": 1,
                "code": "def test_name_stripped():\n    assert Item(' x ', 1.0).name == 'x'",
            },
        ],
        author="C",
        message="strip names",
        base_rev=rev0,
    )
    assert c["status"] == "committed", c
    print(
        f"C: committed rev {c['rev']} from stale base_rev {rev0} (disjoint nodes; OCC rebase)"
    )

    # --- D: stale base_rev, overlapping C's node -> conflict, nothing lands -
    d = vcs.commit_to_main(
        [
            {
                "action": "replace",
                "target": nid(vcs, "self.name"),
                "code": "self.name = name.upper()",
            }
        ],
        author="D",
        message="upper names",
        base_rev=rev0,
    )
    assert d["status"] == "conflict", d
    print(
        f"D: conflict on node {d['conflicts'][0]['node']} "
        f"(current: {d['conflicts'][0]['current_code']!r}) — must re-read and adapt"
    )

    # --- pending-transaction path still works, now suite-gated --------------
    vcs.create_worktree("wt-bad", task="breaks suite")
    vcs.commit(
        "wt-bad",
        [
            {
                "action": "replace",
                "target": nid(vcs, "tax = subtotal", "wt-bad"),
                "code": "tax = subtotal",
            },  # 100% tax: breaks test_total_price
        ],
        author="W",
        message="bad tax",
    )
    m = vcs.merge("wt-bad")
    assert m["status"] == "verification_failed", m
    print("worktree merge blocked by suite:", m["status"])
    vcs.commit(
        "wt-bad",
        [
            {
                "action": "replace",
                "target": nid(vcs, "tax = subtotal", "wt-bad"),
                "code": "tax = subtotal * 0.08",
            },
        ],
        author="W",
        message="fix",
    )
    m = vcs.merge("wt-bad")
    assert m["status"] == "merged", m
    print("after fix, worktree merged:", m["status"])

    # --- every revision of main is green by construction --------------------
    from astvcs import run_source

    for rev in vcs._chain(vcs._head("main")):
        ok, _ = run_source(vcs.tree_at(rev).render())
        assert ok, f"main rev {rev} not green!"
    print(f"every main revision green: {vcs._chain(vcs._head('main'))}")

    print("\nfinal main:")
    print(vcs.tree("main").render())
    print("ALL DIRECT-COMMIT CHECKS PASSED")


if __name__ == "__main__":
    main()
