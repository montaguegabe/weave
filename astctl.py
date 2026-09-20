#!/usr/bin/env python3
"""Tiny CLI for the AST codebase — for the human at the keyboard.

python3 astctl.py init                    seed the database (idempotent)
python3 astctl.py status                  worktrees + their status
python3 astctl.py render [branch]         project a branch to source text
python3 astctl.py outline [branch]        node tree with ids
python3 astctl.py log                     revision history
python3 astctl.py queue                   speculative commit queue
python3 astctl.py worktree <name> [task]  pre-create a worktree from main
"""

import os
import sys

from astvcs import VCS

DB = os.environ.get(
    "ASTDB_PATH", os.path.join(os.path.dirname(__file__), "codebase.db")
)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "init":
        import init_db

        init_db.main()
        return
    v = VCS(DB)
    if cmd == "status":
        for w in v.worktrees():
            print(
                f"{w['name']:<12} rev {w['head_rev']:<4} {w['status']:<8} {w['task']}"
            )
    elif cmd == "render":
        print(v.tree(sys.argv[2] if len(sys.argv) > 2 else "main").render())
    elif cmd == "outline":
        print(v.tree(sys.argv[2] if len(sys.argv) > 2 else "main").outline())
    elif cmd == "log":
        for e in v.log():
            mp = f" (merges rev {e['merge_parent']})" if e["merge_parent"] else ""
            head = f"  <- {e['head_of']}" if e["head_of"] else ""
            print(
                f"rev {e['rev']:>3}  parent={e['parent']}  {e['author']:<12} {e['message']}{mp}{head}"
            )
    elif cmd == "queue":
        for q in reversed(v.queue_status()):
            print(
                f"#{q['ticket']:<3} {q['state']:<8} {q['author']:<12} "
                f"{q['message']}  (base {q['base_head']}, {q['age_s']}s ago)"
            )
    elif cmd == "worktree":
        name = sys.argv[2]
        task = " ".join(sys.argv[3:])
        head = v.create_worktree(name, task)
        print(f"worktree {name!r} created from main @ rev {head}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
