"""Demo: two agents edit adjacent lines of the same function.

Git (text) => merge conflict. AstDB (structural transactions) => clean merge.
Then a genuinely semantic conflict, which AstDB reports at node granularity.
"""

import subprocess
import tempfile
from pathlib import Path

from astdb import AstDB, Conflict, Transaction

BASE = """\
def total_price(items):
    subtotal = sum(i.price for i in items)
    tax = subtotal * 0.08
    return subtotal + tax
"""


def banner(title: str):
    print(f"\n{'=' * 64}\n{title}\n{'=' * 64}")


def show(label: str, source: str):
    print(f"\n--- {label} ---")
    print(source.rstrip())


def git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.email=demo@demo", "-c", "user.name=demo", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


def git_merge_demo(base: str, ours: str, theirs: str) -> tuple[bool, str]:
    """Commit base, branch two edits, merge. Returns (conflicted, file_contents)."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        f = repo / "inventory.py"
        git("init", "-q", "-b", "main", cwd=repo)
        f.write_text(base)
        git("add", ".", cwd=repo)
        git("commit", "-qm", "base", cwd=repo)
        git("checkout", "-qb", "agent-b", cwd=repo)
        f.write_text(theirs)
        git("commit", "-qam", "agent B", cwd=repo)
        git("checkout", "-q", "main", cwd=repo)
        f.write_text(ours)
        git("commit", "-qam", "agent A", cwd=repo)
        merge = git("merge", "agent-b", cwd=repo, check=False)
        return merge.returncode != 0, f.read_text()


def main():
    banner("Code lives in SQLite as an AST, not in a file")
    db = AstDB()
    rev0 = db.ingest(BASE)
    show(f"base (rev {rev0}), rendered from the node store", db.render())
    print("\nnode table:")
    for nid, parent, pos, kind, deleted, header in db.dump_nodes():
        print(
            f"  #{nid} parent={parent} pos={pos} {kind:6} {header.splitlines()[0] if header else ''!r}"
        )

    # Both agents build transactions against the SAME base revision,
    # targeting nodes they located by querying the program graph.
    tax_stmt = db.find("tax = subtotal")
    subtotal_stmt = db.find("subtotal = sum")

    tx_a = Transaction(author="agent-A", base_rev=rev0)
    tx_a.replace(tax_stmt, "tax = subtotal * get_tax_rate()")
    tx_a.append_child(db._root(), "def get_tax_rate():\n    return 0.0925")

    tx_b = Transaction(author="agent-B", base_rev=rev0)
    tx_b.replace(subtotal_stmt, "subtotal = sum(i.price * i.quantity for i in items)")

    ours = db.render(at_ops=tx_a.ops)
    theirs = db.render(at_ops=tx_b.ops)
    show("agent A's branch (projected to text)", ours)
    show("agent B's branch (projected to text)", theirs)

    banner("1) The same two edits, merged by git on text")
    conflicted, contents = git_merge_demo(BASE, ours, theirs)
    print(f"\ngit merge conflicted: {conflicted}")
    show("inventory.py after git merge", contents)

    banner("2) The same two edits, merged as structural transactions")
    rev_a = db.commit(tx_a)
    print(f"\nagent A committed cleanly -> rev {rev_a}")
    print(
        f"agent B commits against stale base rev {tx_b.base_rev} "
        f"(head is {rev_a}) -> OCC rebase..."
    )
    rev_b = db.commit(tx_b)
    print(
        f"agent B committed cleanly -> rev {rev_b}  (edits touch different AST nodes)"
    )
    merged = db.render()
    show("merged source, projected from the database", merged)

    # Prove the merged program is real, working code containing both edits.
    ns: dict = {}
    exec(merged, ns)

    class Item:
        def __init__(self, price, quantity):
            self.price, self.quantity = price, quantity

    result = ns["total_price"]([Item(10.0, 2), Item(5.0, 1)])
    expected = 25.0 * 1.0925
    assert abs(result - expected) < 1e-9, (result, expected)
    print(
        f"\nexecuted merged code: total_price(...) == {result} "
        f"(both agents' changes active) ✓"
    )

    banner("3) A real semantic conflict is still caught — at node granularity")
    tx_c = Transaction(author="agent-C", base_rev=rev_b)
    tx_d = Transaction(author="agent-D", base_rev=rev_b)
    tax_stmt = db.find("tax = subtotal")
    tx_c.replace(tax_stmt, "tax = compute_tax(subtotal, region)")
    tx_d.replace(tax_stmt, "tax = 0  # tax-exempt")
    db.commit(tx_c)
    try:
        db.commit(tx_d)
    except Conflict as e:
        print(f"\nConflict: {e}")
        print(f"  node    : #{e.node_id}  path: {db.path(e.node_id)}")
        print(f"  theirs  : {e.theirs}")
        print(f"  ours    : {e.ours}")
        print("\n(Only genuinely overlapping edits conflict — and the system knows")
        print(" exactly which node and which two operations, so a third agent could")
        print(" be handed just this one reconciliation.)")

    banner("Transaction log (the real history — files were never canonical)")
    for rev, base_rev, author, ops in db.log():
        print(f"  rev {rev}  base={base_rev}  {author:8} {ops}")


if __name__ == "__main__":
    main()
