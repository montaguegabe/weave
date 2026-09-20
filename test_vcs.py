"""Exercise astvcs end-to-end: worktrees, commits, clean merge, conflict, resolve."""

import os
import tempfile

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
"""


def node_id(vcs, branch, needle):
    tree = vcs.tree(branch)
    hits = [
        nid for nid, n in tree.nodes.items() if needle in n.header and tree.alive(nid)
    ]
    assert len(hits) == 1, (needle, hits)
    return hits[0]


def main():
    path = os.path.join(tempfile.mkdtemp(), "codebase.db")
    vcs = VCS(path)
    vcs.init_codebase(BASE)

    print("== outline ==")
    print(vcs.tree("main").outline())
    assert vcs.tree("main").render().strip() == BASE.strip(), vcs.tree("main").render()

    # --- two worktrees fork from the same main head -------------------------
    vcs.create_worktree("feat-tax", task="dynamic tax rate")
    vcs.create_worktree("feat-qty", task="quantity support")

    vcs.commit(
        "feat-tax",
        [
            {
                "action": "replace",
                "target": node_id(vcs, "feat-tax", "tax = subtotal"),
                "code": "tax = subtotal * get_tax_rate()",
            },
            {
                "action": "append_child",
                "target": 1,
                "code": "def get_tax_rate():\n    return 0.0925",
            },
        ],
        author="agent-tax",
        message="dynamic tax rate",
    )

    vcs.commit(
        "feat-qty",
        [
            {
                "action": "replace_subtree",
                "target": node_id(vcs, "feat-qty", "def __init__"),
                "code": (
                    "def __init__(self, name, price, quantity=1):\n"
                    "    self.name = name\n"
                    "    self.price = price\n"
                    "    self.quantity = quantity"
                ),
            },
            {
                "action": "replace",
                "target": node_id(vcs, "feat-qty", "subtotal = sum"),
                "code": "subtotal = sum(i.price * i.quantity for i in items)",
            },
        ],
        author="agent-qty",
        message="quantity support",
    )

    vcs.set_status("feat-tax", "ready")
    vcs.set_status("feat-qty", "ready")

    # --- both merge cleanly: different AST nodes ----------------------------
    r1 = vcs.merge("feat-tax")
    assert r1["status"] == "merged", r1
    r2 = vcs.merge("feat-qty")
    assert r2["status"] == "merged", r2
    merged = r2["merged_source"]
    print("\n== merged main ==")
    print(merged)

    ns = {}
    exec(merged, ns)
    total = ns["total_price"]([ns["Item"]("a", 10.0, 2), ns["Item"]("b", 5.0)])
    assert abs(total - 25.0 * 1.0925) < 1e-9, total
    print(f"executed merged code: total_price == {total}  OK")

    # --- now a real conflict -------------------------------------------------
    vcs.create_worktree("feat-region", task="regional tax")
    vcs.create_worktree("feat-exempt", task="tax exemption")
    tax_node = node_id(vcs, "feat-region", "tax = subtotal")
    vcs.commit(
        "feat-region",
        [
            {
                "action": "replace",
                "target": tax_node,
                "code": "tax = subtotal * get_tax_rate(region)",
            },
            {
                "action": "replace",
                "target": node_id(vcs, "feat-region", "def total_price"),
                "code": "def total_price(items, region='CA'):",
            },
            {
                "action": "replace_subtree",
                "target": node_id(vcs, "feat-region", "def get_tax_rate"),
                "code": (
                    "def get_tax_rate(region='CA'):\n"
                    "    return {'CA': 0.0925, 'OR': 0.0}.get(region, 0.08)"
                ),
            },
        ],
        author="agent-region",
        message="regional tax",
    )
    vcs.commit(
        "feat-exempt",
        [
            {
                "action": "replace",
                "target": tax_node,
                "code": "tax = 0 if exempt else subtotal * get_tax_rate()",
            },
            {
                "action": "replace",
                "target": node_id(vcs, "feat-exempt", "def total_price"),
                "code": "def total_price(items, exempt=False):",
            },
        ],
        author="agent-exempt",
        message="tax exemption",
    )

    assert vcs.merge("feat-region")["status"] == "merged"
    res = vcs.merge("feat-exempt")
    assert res["status"] == "conflict", res
    print("\n== conflict report ==")
    for c in res["conflicts"]:
        print(f"  node #{c['node']}")
        print(f"    base:     {c['base_code']}")
        print(f"    main:     {c['main_code']}")
        print(f"    worktree: {c['worktree_code']}")

    # resolve: combine both intents
    resolutions = []
    for c in res["conflicts"]:
        if "def total_price" in c["base_code"]:
            resolutions.append(
                {
                    "target": c["node"],
                    "code": "def total_price(items, region='CA', exempt=False):",
                }
            )
        else:
            resolutions.append(
                {
                    "target": c["node"],
                    "code": "tax = 0 if exempt else subtotal * get_tax_rate(region)",
                }
            )
    done = vcs.resolve_merge("feat-exempt", resolutions, author="merge-agent")
    assert done["status"] == "merged"
    print("\n== resolved main ==")
    print(done["merged_source"])

    ns = {}
    exec(done["merged_source"], ns)
    assert ns["total_price"]([ns["Item"]("a", 10.0, 2)], exempt=True) == 20.0
    assert abs(ns["total_price"]([ns["Item"]("a", 10.0, 2)]) - 20.0 * 1.0925) < 1e-9
    print("executed resolved code with both features active  OK")

    # guards
    try:
        vcs.commit("main", [{"action": "delete", "target": 2}], "x", "nope")
        raise AssertionError("commit to main should fail")
    except VcsError:
        pass
    try:
        vcs.commit("feat-exempt", [{"action": "delete", "target": 2}], "x", "nope")
        raise AssertionError("commit to merged worktree should fail")
    except VcsError:
        pass

    print("\n== log ==")
    for e in vcs.log():
        head = f"  <- head of {e['head_of']}" if e["head_of"] else ""
        mp = f" (merge of rev {e['merge_parent']})" if e["merge_parent"] else ""
        print(
            f"  rev {e['rev']:>2}  parent={e['parent']}  {e['author']:<13} {e['message']}{mp}{head}"
        )

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
