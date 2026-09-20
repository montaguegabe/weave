"""astvcs: version-controlled AST-in-SQLite code store with worktrees.

Everything is an operation log. The codebase is a tree of nodes (module ->
def/class -> statements) that never exists as a file: every revision is a list
of structural ops, and any state is materialized by replaying the op chain.

Concepts:
  - revision: {parent_rev, ops} — a committed transaction of structural ops.
  - branch/worktree: a named head pointer. 'main' is the trunk; worktrees are
    created from main's head, accumulate commits, get marked 'ready', and are
    merged back by a merge agent.
  - merge: find common ancestor, take the worktree's ops since the fork, check
    node-level overlap with what landed on main since the fork. No overlap ->
    ops are replayed onto main (a rebase). Overlap -> node-level conflict
    report for an LLM to resolve.

Ops (targets are stable node ids):
  replace(target, code)          - swap a statement's code / a def's header line
  replace_subtree(target, code)  - delete node+children, insert new code there
  insert_after(target, code)     - new sibling(s) after target
  append_child(target, code)     - new node(s) at end of a module/def/class body
  delete(target)
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import sqlite3
import textwrap
import time
import traceback
from dataclasses import dataclass, field

INDENT = "    "
ROOT_ID = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS revisions (
    rev          INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_rev   INTEGER,
    merge_parent INTEGER,
    author       TEXT,
    message      TEXT,
    ops          TEXT NOT NULL,
    created_at   REAL
);
CREATE TABLE IF NOT EXISTS branches (
    name       TEXT PRIMARY KEY,
    head_rev   INTEGER NOT NULL,
    status     TEXT NOT NULL,   -- trunk | open | ready | merged
    task       TEXT DEFAULT '',
    created_at REAL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS queue (
    ticket     INTEGER PRIMARY KEY AUTOINCREMENT,
    author     TEXT,
    message    TEXT,
    ops        TEXT NOT NULL,     -- prepared ops (node ids assigned)
    test_code  TEXT,
    state      TEXT NOT NULL,     -- pending | landed | evicted
    base_head  INTEGER,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS test_cache (
    test_name    TEXT NOT NULL,
    closure_hash TEXT NOT NULL,   -- hash of the test's transitive dependencies
    PRIMARY KEY (test_name, closure_hash)
);
"""

STALE_TICKET_S = 600  # pending tickets older than this are presumed orphaned

MODIFYING = ("replace", "replace_subtree", "delete")


class VcsError(Exception):
    pass


# --------------------------------------------------------------- snippets


def snippet_protos(code: str) -> list[dict]:
    """Parse a code snippet into a preorder list of node prototypes.

    Each proto: {parent: index into this list, or -1 for a root; kind; header}.
    Deterministic for a given code string (replay relies on this).
    """
    code = textwrap.dedent(code).strip("\n")
    tree = ast.parse(code)
    lines = code.splitlines()
    protos: list[dict] = []

    def walk(body: list[ast.stmt], parent_idx: int):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                header = textwrap.dedent(
                    "\n".join(lines[start - 1 : node.body[0].lineno - 1])
                ).strip("\n")
                kind = "class" if isinstance(node, ast.ClassDef) else "def"
                idx = len(protos)
                protos.append({"parent": parent_idx, "kind": kind, "header": header})
                walk(node.body, idx)
            else:
                seg = ast.get_source_segment(code, node) or ast.unparse(node)
                protos.append(
                    {
                        "parent": parent_idx,
                        "kind": "stmt",
                        "header": textwrap.dedent(seg),
                    }
                )

    walk(tree.body, -1)
    if not protos:
        raise VcsError("snippet contains no statements")
    return protos


def check_header_or_stmt(code: str, kind: str):
    """Validate replacement code: full statement(s) for stmt nodes, a bare
    'def f(...):' / 'class C:' header (no body) for container nodes."""
    code = textwrap.dedent(code).strip("\n")
    if not code:
        raise VcsError("empty code")
    if kind == "stmt":
        try:
            ast.parse(code)
        except SyntaxError as e:
            raise VcsError(f"replacement does not parse: {e}") from e
        return
    # container header: must parse once a dummy body is attached...
    try:
        ast.parse(code + "\n" + INDENT + "pass")
    except SyntaxError as e:
        raise VcsError(f"header does not parse: {e}") from e
    # ...and must NOT already carry a real body (that's replace_subtree's job)
    try:
        mod = ast.parse(code)
    except SyntaxError:
        return  # bare header alone doesn't parse -> it's a pure header, good
    stmt = mod.body[0] if mod.body else None
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        raise VcsError(
            "replace on a def/class node takes only the header line "
            "(e.g. 'def foo(x, y):'); to rewrite the whole definition use "
            "replace_subtree"
        )


# --------------------------------------------------------------- verification


def _run_suite(
    source: str,
    extra_test_code: str | None = None,
    skip: set[str] | frozenset[str] = frozenset(),
) -> tuple[bool, str, list[str]]:
    """Execute source in-memory, then the stored suite (every top-level test_*
    function, minus `skip`), then optional extra asserts.
    Returns (ok, output, tests_actually_run)."""
    buf = io.StringIO()
    # NOTE: no contextlib.redirect_stdout here — it swaps the process-global
    # sys.stdout and is not thread-safe (concurrent speculative verifiers run
    # in parallel threads/processes). Shadowing print in the exec namespace
    # captures test output without touching global state.
    ns: dict = {
        "__name__": "__main__",
        "print": lambda *a, **k: print(
            *a, file=buf, **{x: y for x, y in k.items() if x != "file"}
        ),
    }
    tests_run: list[str] = []
    skipped = sorted(n for n in skip if n.startswith("test_"))
    try:
        exec(compile(source, "<astdb>", "exec"), ns)
        for name in sorted(ns):
            if name.startswith("test_") and callable(ns[name]) and name not in skip:
                ns[name]()
                tests_run.append(name)
        if extra_test_code:
            exec(compile(extra_test_code, "<extra-asserts>", "exec"), ns)
    except BaseException:
        stdout = buf.getvalue().strip()
        return (
            False,
            (f"stdout:\n{stdout}\n" if stdout else "")
            + f"suite so far: {tests_run or 'none'}\n{traceback.format_exc(limit=3)}",
            tests_run,
        )
    summary = f"suite passed — ran: {', '.join(tests_run) if tests_run else 'none'}"
    if skipped:
        summary += f"; cache-skipped (dependencies unchanged): {', '.join(skipped)}"
    if extra_test_code:
        summary += "; extra asserts passed"
    stdout = buf.getvalue().strip()
    return True, summary + (f"\nstdout:\n{stdout}" if stdout else ""), tests_run


def run_source(source: str, extra_test_code: str | None = None) -> tuple[bool, str]:
    """Uncached full-suite run. Returns (ok, output)."""
    ok, output, _ = _run_suite(source, extra_test_code)
    return ok, output


def test_closures(source: str) -> dict[str, str]:
    """For each top-level test_* function, hash its dependency closure: the
    source of every top-level def/class it transitively references, plus all
    module-level plain statements (constants etc.). A test whose closure hash
    is unchanged cannot behave differently (assuming the pure-function
    convention this store uses) — its cached pass result stays valid."""
    mod = ast.parse(source)
    tops: dict[str, tuple[str, set[str]]] = {}
    globals_blob: list[str] = []
    for node in mod.body:
        seg = ast.get_source_segment(source, node) or ast.unparse(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            refs = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            tops[node.name] = (seg, refs)
        else:
            globals_blob.append(seg)
    closures: dict[str, str] = {}
    for name in tops:
        if not name.startswith("test_"):
            continue
        seen: set[str] = set()
        stack = [name]
        while stack:
            cur = stack.pop()
            if cur in seen or cur not in tops:
                continue
            seen.add(cur)
            stack.extend(tops[cur][1])
        blob = "\n".join([tops[n][0] for n in sorted(seen)] + sorted(globals_blob))
        closures[name] = hashlib.sha256(blob.encode()).hexdigest()
    return closures


# --------------------------------------------------------------- tree state


@dataclass
class Node:
    kind: str
    header: str
    parent: int | None
    deleted: bool = False


@dataclass
class Tree:
    nodes: dict[int, Node] = field(default_factory=dict)
    children: dict[int, list[int]] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "Tree":
        t = cls()
        t.nodes[ROOT_ID] = Node("module", "", None)
        t.children[ROOT_ID] = []
        return t

    def alive(self, nid: int) -> bool:
        n = self.nodes.get(nid)
        while n is not None:
            if n.deleted:
                return False
            n = self.nodes.get(n.parent) if n.parent is not None else None
        return nid in self.nodes

    def _materialize(
        self, protos: list[dict], ids: list[int], attach: str, target: int
    ):
        for proto, nid in zip(protos, ids):
            parent = ids[proto["parent"]] if proto["parent"] >= 0 else None
            self.nodes[nid] = Node(proto["kind"], proto["header"], parent)
            self.children[nid] = []
            if parent is not None:
                self.children[parent].append(nid)
        roots = [nid for proto, nid in zip(protos, ids) if proto["parent"] < 0]
        if attach == "append_child":
            for r in roots:
                self.nodes[r].parent = target
                self.children[target].append(r)
        else:  # insert_after / in_place
            parent = self.nodes[target].parent
            idx = self.children[parent].index(target)
            for offset, r in enumerate(roots, start=1):
                self.nodes[r].parent = parent
                self.children[parent].insert(idx + offset, r)

    def apply(self, op: dict):
        action, target = op["action"], op.get("target")
        if action == "replace":
            self.nodes[target].header = textwrap.dedent(op["code"]).strip("\n")
        elif action == "delete":
            self.nodes[target].deleted = True
        elif action == "replace_subtree":
            protos = snippet_protos(op["code"])
            self._materialize(protos, op["new_ids"], "insert_after", target)
            self.nodes[target].deleted = True
        elif action in ("insert_after", "append_child"):
            protos = snippet_protos(op["code"])
            self._materialize(protos, op["new_ids"], action, target)
        else:
            raise VcsError(f"unknown action {action!r}")

    def validate(self, op: dict):
        action, target = op.get("action"), op.get("target")
        if action not in (
            "replace",
            "delete",
            "replace_subtree",
            "insert_after",
            "append_child",
        ):
            raise VcsError(f"unknown action {action!r}")
        if target not in self.nodes or not self.alive(target):
            raise VcsError(
                f"op {action}: node #{target} does not exist (or was deleted)"
            )
        node = self.nodes[target]
        if action == "replace":
            check_header_or_stmt(op.get("code", ""), node.kind)
        elif action in ("replace_subtree", "insert_after"):
            if target == ROOT_ID:
                raise VcsError(
                    f"{action} cannot target the module root; use append_child"
                )
            snippet_protos(op.get("code", ""))
        elif action == "append_child":
            if node.kind == "stmt":
                raise VcsError("append_child target must be the module or a def/class")
            snippet_protos(op.get("code", ""))

    # ------------------------------------------------------------- output

    def render(self) -> str:
        return self._render_children(ROOT_ID, 0)

    def _render_children(self, parent: int, depth: int) -> str:
        out = []
        for nid in self.children.get(parent, []):
            node = self.nodes[nid]
            if node.deleted:
                continue
            block = textwrap.indent(node.header, INDENT * depth)
            if node.kind in ("def", "class"):
                body = self._render_children(nid, depth + 1)
                block += "\n" + (body if body else INDENT * (depth + 1) + "pass")
                if depth == 0:
                    block += "\n"
            out.append(block)
        sep = "\n\n" if depth == 0 else "\n"
        rendered = sep.join(out).rstrip("\n")
        return rendered + ("\n" if depth == 0 and rendered else "")

    def outline(self) -> str:
        lines: list[str] = []

        def walk(nid: int, depth: int):
            node = self.nodes[nid]
            if node.deleted:
                return
            first = node.header.splitlines()[0] if node.header else "<module>"
            lines.append(f"{'  ' * depth}#{nid} [{node.kind}] {first}")
            for c in self.children.get(nid, []):
                walk(c, depth + 1)

        walk(ROOT_ID, 0)
        return "\n".join(lines)


# --------------------------------------------------------------- the store


class VCS:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, timeout=15)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=15000")
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ------------------------------------------------------------ helpers

    def _next_ids(self, n: int) -> list[int]:
        row = self.db.execute(
            "SELECT value FROM meta WHERE key='next_node_id'"
        ).fetchone()
        start = int(row[0]) if row else ROOT_ID + 1
        self.db.execute(
            "INSERT INTO meta (key, value) VALUES ('next_node_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(start + n),),
        )
        return list(range(start, start + n))

    def _head(self, branch: str) -> int:
        row = self.db.execute(
            "SELECT head_rev FROM branches WHERE name=?", (branch,)
        ).fetchone()
        if not row:
            raise VcsError(f"no branch/worktree named {branch!r}")
        return row[0]

    def _chain(self, rev: int | None) -> list[int]:
        chain = []
        while rev is not None:
            chain.append(rev)
            row = self.db.execute(
                "SELECT parent_rev FROM revisions WHERE rev=?", (rev,)
            ).fetchone()
            if row is None:
                raise VcsError(f"revision {rev} not found")
            rev = row[0]
        return list(reversed(chain))

    def _ops_of(self, rev: int) -> list[dict]:
        (ops,) = self.db.execute(
            "SELECT ops FROM revisions WHERE rev=?", (rev,)
        ).fetchone()
        return json.loads(ops)

    def tree_at(self, rev: int) -> Tree:
        tree = Tree.empty()
        for r in self._chain(rev):
            for op in self._ops_of(r):
                tree.apply(op)
        return tree

    def tree(self, branch: str) -> Tree:
        return self.tree_at(self._head(branch))

    # ------------------------------------------------------------ init

    def init_codebase(self, source: str, author: str = "init") -> int:
        if self.db.execute("SELECT 1 FROM branches WHERE name='main'").fetchone():
            raise VcsError("codebase already initialized")
        op = {"action": "append_child", "target": ROOT_ID, "code": source}
        return self._commit_to("main", [op], author, "initial codebase", create="trunk")

    # ------------------------------------------------------------ commits

    def _prepare(self, tree: Tree, raw_ops: list[dict]) -> list[dict]:
        """Validate ops against a tree state and assign new node ids."""
        if not raw_ops:
            raise VcsError("empty transaction")
        prepared = []
        for raw in raw_ops:
            op = {k: raw[k] for k in ("action", "target", "code") if k in raw}
            tree.validate(op)
            if op["action"] in ("insert_after", "append_child", "replace_subtree"):
                op["new_ids"] = self._next_ids(len(snippet_protos(op["code"])))
            tree.apply(op)  # apply as we go so later ops can target earlier inserts
            prepared.append(op)
        return prepared

    def _commit_to(
        self,
        branch: str,
        raw_ops: list[dict],
        author: str,
        message: str,
        create: str | None = None,
        merge_parent: int | None = None,
        prepared: bool = False,
        expected_parent: int | None = None,
    ) -> int:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if create:
                if self.db.execute(
                    "SELECT 1 FROM branches WHERE name=?", (branch,)
                ).fetchone():
                    raise VcsError(f"branch {branch!r} already exists")
                parent = None
                tree = Tree.empty()
            else:
                parent = self._head(branch)
                if expected_parent is not None and parent != expected_parent:
                    raise VcsError(
                        f"{branch} advanced (rev {expected_parent} -> {parent}) "
                        "during verification; retry the merge"
                    )
                tree = self.tree_at(parent)
            ops = raw_ops if prepared else self._prepare(tree, raw_ops)
            cur = self.db.execute(
                "INSERT INTO revisions (parent_rev, merge_parent, author, message, ops, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (parent, merge_parent, author, message, json.dumps(ops), time.time()),
            )
            rev = cur.lastrowid
            if create:
                self.db.execute(
                    "INSERT INTO branches (name, head_rev, status, created_at) VALUES (?, ?, ?, ?)",
                    (branch, rev, create, time.time()),
                )
            else:
                self.db.execute(
                    "UPDATE branches SET head_rev=? WHERE name=?", (rev, branch)
                )
            self.db.commit()
            return rev
        except BaseException:
            self.db.rollback()
            raise

    def commit(
        self, worktree: str, raw_ops: list[dict], author: str, message: str
    ) -> int:
        status = self.worktree_status(worktree)
        if worktree == "main" or status == "trunk":
            raise VcsError(
                "use commit_to_main for main; worktree commits are for pending transactions"
            )
        if status == "merged":
            raise VcsError(f"worktree {worktree!r} is already merged")
        return self._commit_to(worktree, raw_ops, author, message)

    def commit_to_main(
        self,
        raw_ops: list[dict],
        author: str,
        message: str,
        test_code: str | None = None,
        base_rev: int | None = None,
        dry_run: bool = False,
        wait_timeout: float = 120.0,
    ) -> dict:
        """Verified OCC transaction on main via a speculative merge queue.

        Enqueue happens under a millisecond write lock (conflict checks + node
        id assignment). Verification runs OUTSIDE the lock, concurrently with
        other committers, against the predicted landing state: landed head +
        queued-ahead transactions + these ops. Landing is a hash check — if
        the state that passed verification is byte-identical to the real
        landing state, it lands instantly with no re-run; an eviction ahead
        invalidates the hash and triggers re-verification. Main only ever
        advances through verified states, in ticket order.
        """
        if dry_run:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                tree = self.tree_at(self._head("main"))
                self._prepare(tree, raw_ops)
                src = tree.render()
            finally:
                self.db.rollback()  # discard id allocation; nothing persists
            ok, output = self.verify(src, test_code)
            return {
                "status": "would_commit" if ok else "verification_failed",
                "dry_run": True,
                "output": output,
            }

        # ---- phase 1: conflict checks + enqueue (brief write lock) --------
        self.db.execute("BEGIN IMMEDIATE")
        try:
            head = self._head("main")
            conflict = self._landed_conflicts(raw_ops, base_rev, head)
            if conflict:
                self.db.rollback()
                return conflict
            mine = {op.get("target") for op in raw_ops if op.get("action") in MODIFYING}
            pending = self.db.execute(
                "SELECT ticket, author, ops FROM queue WHERE state='pending' ORDER BY ticket"
            ).fetchall()
            for t, a, ops_json in pending:
                theirs = {
                    o["target"]
                    for o in json.loads(ops_json)
                    if o["action"] in MODIFYING
                }
                overlap = sorted(mine & theirs)
                if overlap:
                    self.db.rollback()
                    return {
                        "status": "conflict",
                        "conflicts": [
                            {"node": n, "queued_ticket": t, "queued_author": a}
                            for n in overlap
                        ],
                        "hint": "a queued (not yet landed) transaction modifies these "
                        "nodes; wait for it to resolve, re-read main, and retry",
                    }
            tree = self.tree_at(head)
            for _t, _a, ops_json in pending:
                for op in json.loads(ops_json):
                    tree.apply(op)
            ops = self._prepare(tree, raw_ops)
            cur = self.db.execute(
                "INSERT INTO queue (author, message, ops, test_code, state, base_head, created_at) "
                "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (author, message, json.dumps(ops), test_code, head, time.time()),
            )
            ticket = cur.lastrowid
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

        # ---- phases 2+3: speculative verify (no lock), land by hash match --
        deadline = time.time() + wait_timeout
        verified_hash: str | None = None
        verified_ok = False
        verified_out = ""
        was_speculative = False
        while True:
            self.db.execute("BEGIN")  # consistent snapshot of head + queue
            head = self._head("main")
            ahead_rows = self.db.execute(
                "SELECT ticket, ops, created_at FROM queue "
                "WHERE state='pending' AND ticket < ? ORDER BY ticket",
                (ticket,),
            ).fetchall()
            self.db.execute("COMMIT")
            for t, _o, created in ahead_rows:  # unblock orphaned tickets
                if time.time() - created > STALE_TICKET_S:
                    self._evict(t)
            ahead = [
                (t, o) for t, o, c in ahead_rows if time.time() - c <= STALE_TICKET_S
            ]
            try:
                tree = self.tree_at(head)
                for _t, ops_json in ahead:
                    for op in json.loads(ops_json):
                        tree.apply(op)
                for op in ops:
                    tree.apply(op)
                src = tree.render()
            except Exception as e:
                if ahead:
                    time.sleep(0.2)
                    continue
                self._evict(ticket)
                return {
                    "status": "verification_failed",
                    "output": f"ops no longer apply to main: {e}",
                }
            h = hashlib.sha256(src.encode()).hexdigest()
            if h != verified_hash:
                verified_ok, verified_out = self.verify(src, test_code)
                verified_hash = h
                was_speculative = bool(ahead)
            if not verified_ok:
                if ahead:
                    # may be contamination from a speculated-in transaction
                    # ahead of us; wait for the queue to resolve, then retest
                    if time.time() > deadline:
                        self._evict(ticket)
                        return {"status": "timeout", "output": verified_out}
                    time.sleep(0.2)
                    continue
                self._evict(ticket)
                return {
                    "status": "verification_failed",
                    "output": verified_out,
                    "hint": "the candidate state (current main + your ops) fails "
                    "verification; nothing was committed. Fix and retry.",
                }
            # green: land iff we're at the queue head and the real landing
            # state is exactly the state we verified
            self.db.execute("BEGIN IMMEDIATE")
            try:
                still_ahead = self.db.execute(
                    "SELECT COUNT(*) FROM queue WHERE state='pending' AND ticket < ?",
                    (ticket,),
                ).fetchone()[0]
                if still_ahead == 0:
                    cur_head = self._head("main")
                    try:
                        land_tree = self.tree_at(cur_head)
                        for op in ops:
                            land_tree.apply(op)
                        land_hash = hashlib.sha256(
                            land_tree.render().encode()
                        ).hexdigest()
                    except Exception:
                        land_hash = None
                    if land_hash == verified_hash:
                        cur = self.db.execute(
                            "INSERT INTO revisions (parent_rev, merge_parent, author, message, ops, created_at) "
                            "VALUES (?, NULL, ?, ?, ?, ?)",
                            (cur_head, author, message, json.dumps(ops), time.time()),
                        )
                        self.db.execute(
                            "UPDATE branches SET head_rev=? WHERE name='main'",
                            (cur.lastrowid,),
                        )
                        self.db.execute(
                            "UPDATE queue SET state='landed' WHERE ticket=?", (ticket,)
                        )
                        self.db.commit()
                        return {
                            "status": "committed",
                            "rev": cur.lastrowid,
                            "verification": verified_out,
                            "speculative": was_speculative,
                            "main_source": self.tree("main").render(),
                        }
                self.db.rollback()
            except BaseException:
                self.db.rollback()
                raise
            if time.time() > deadline:
                self._evict(ticket)
                return {"status": "timeout", "output": "timed out waiting to land"}
            time.sleep(0.1)

    def _landed_conflicts(
        self, raw_ops: list[dict], base_rev: int | None, head: int
    ) -> dict | None:
        """OCC check against landed history: nodes I modify that someone else
        modified since base_rev. Returns a conflict result dict, or None."""
        if base_rev is None or base_rev == head:
            return None
        chain = self._chain(head)
        if base_rev not in chain:
            raise VcsError(f"base_rev {base_rev} is not an ancestor of main")
        landed: dict[int, int] = {}
        for r in chain[chain.index(base_rev) + 1 :]:
            for op in self._ops_of(r):
                if op["action"] in MODIFYING:
                    landed[op["target"]] = r
        mine = {op.get("target") for op in raw_ops if op.get("action") in MODIFYING}
        overlap = sorted(mine & set(landed))
        if not overlap:
            return None
        base_t, head_t = self.tree_at(base_rev), self.tree_at(head)

        def code(t: Tree, nid: int) -> str:
            if nid not in t.nodes or not t.alive(nid):
                return "<deleted or rewritten>"
            return t.nodes[nid].header

        return {
            "status": "conflict",
            "conflicts": [
                {
                    "node": nid,
                    "landed_in_rev": landed[nid],
                    "base_code": code(base_t, nid),
                    "current_code": code(head_t, nid),
                }
                for nid in overlap
            ],
            "hint": "someone changed these nodes since you read main; re-read "
            "(list_nodes/read_source), adapt your ops to the current code, and "
            "retry with the new rev as base_rev",
        }

    def _evict(self, ticket: int):
        self.db.execute(
            "UPDATE queue SET state='evicted' WHERE ticket=? AND state='pending'",
            (ticket,),
        )
        self.db.commit()

    def queue_status(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT ticket, author, message, state, base_head, created_at "
            "FROM queue ORDER BY ticket DESC LIMIT 20"
        ).fetchall()
        now = time.time()
        return [
            {
                "ticket": t,
                "author": a,
                "message": m,
                "state": s,
                "base_head": b,
                "age_s": round(now - c, 1),
            }
            for t, a, m, s, b, c in rows
        ]

    # ------------------------------------------------------------ verification

    def verify(
        self, source: str, extra_test_code: str | None = None
    ) -> tuple[bool, str]:
        """Run the suite with dependency-hashed caching: a test_* function is
        skipped when its dependency-closure hash already has a recorded pass
        (the code it can observe is unchanged, so the result cannot differ).
        Passes recorded here are shared by every process on this store —
        advisory and speculative runs warm the cache for the landing gate."""
        closures = test_closures(source)
        skip = {
            name
            for name, ch in closures.items()
            if self.db.execute(
                "SELECT 1 FROM test_cache WHERE test_name=? AND closure_hash=?",
                (name, ch),
            ).fetchone()
        }
        ok, output, ran = _run_suite(source, extra_test_code, skip)
        recorded = [(name, closures[name]) for name in ran if name in closures]
        if recorded:
            self.db.executemany(
                "INSERT OR IGNORE INTO test_cache (test_name, closure_hash) VALUES (?, ?)",
                recorded,
            )
            self.db.commit()
        return ok, output

    # ------------------------------------------------------------ worktrees

    def create_worktree(self, name: str, task: str = "") -> int:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.db.execute(
                "SELECT 1 FROM branches WHERE name=?", (name,)
            ).fetchone():
                raise VcsError(f"worktree {name!r} already exists")
            head = self._head("main")
            self.db.execute(
                "INSERT INTO branches (name, head_rev, status, task, created_at) "
                "VALUES (?, ?, 'open', ?, ?)",
                (name, head, task, time.time()),
            )
            self.db.commit()
            return head
        except BaseException:
            self.db.rollback()
            raise

    def worktree_status(self, name: str) -> str:
        row = self.db.execute(
            "SELECT status FROM branches WHERE name=?", (name,)
        ).fetchone()
        if not row:
            raise VcsError(f"no branch/worktree named {name!r}")
        return row[0]

    def set_status(self, name: str, status: str):
        if self.worktree_status(name) == "trunk":
            raise VcsError("cannot change main's status")
        self.db.execute("UPDATE branches SET status=? WHERE name=?", (status, name))
        self.db.commit()

    def worktrees(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT name, head_rev, status, task FROM branches ORDER BY created_at"
        ).fetchall()
        return [
            {"name": n, "head_rev": h, "status": s, "task": t} for n, h, s, t in rows
        ]

    # ------------------------------------------------------------ merging

    def _fork_data(self, worktree: str):
        main_head, wt_head = self._head("main"), self._head(worktree)
        main_chain, wt_chain = self._chain(main_head), self._chain(wt_head)
        ancestors = set(main_chain)
        anc = next(r for r in reversed(wt_chain) if r in ancestors)
        main_ops = [
            op
            for r in main_chain[main_chain.index(anc) + 1 :]
            for op in self._ops_of(r)
        ]
        wt_ops = [
            op for r in wt_chain[wt_chain.index(anc) + 1 :] for op in self._ops_of(r)
        ]
        return anc, main_head, wt_head, main_ops, wt_ops

    @staticmethod
    def _modified(ops: list[dict]) -> set[int]:
        return {op["target"] for op in ops if op["action"] in MODIFYING}

    def find_conflicts(self, worktree: str) -> list[dict]:
        anc, main_head, wt_head, main_ops, wt_ops = self._fork_data(worktree)
        overlap = self._modified(main_ops) & self._modified(wt_ops)
        if not overlap:
            return []
        base_t, main_t, wt_t = (
            self.tree_at(anc),
            self.tree_at(main_head),
            self.tree_at(wt_head),
        )

        def state(tree: Tree, nid: int) -> str:
            if nid not in tree.nodes or not tree.alive(nid):
                return "<deleted or rewritten>"
            return tree.nodes[nid].header

        conflicts = []
        for nid in sorted(overlap):
            main_code, wt_code = state(main_t, nid), state(wt_t, nid)
            if main_code == wt_code:
                continue  # both sides made the identical change
            conflicts.append(
                {
                    "node": nid,
                    "base_code": state(base_t, nid),
                    "main_code": main_code,
                    "worktree_code": wt_code,
                }
            )
        return conflicts

    def merge(self, worktree: str, author: str = "merge") -> dict:
        if self.worktree_status(worktree) == "merged":
            raise VcsError(f"worktree {worktree!r} is already merged")
        conflicts = self.find_conflicts(worktree)
        if conflicts:
            return {
                "status": "conflict",
                "conflicts": conflicts,
                "main_source": self.tree("main").render(),
                "worktree_source": self.tree(worktree).render(),
            }
        anc, main_head, wt_head, _main_ops, wt_ops = self._fork_data(worktree)
        if not wt_ops:
            self.set_status(worktree, "merged")
            return {
                "status": "merged",
                "note": "worktree had no commits",
                "rev": main_head,
            }
        candidate = self.tree_at(main_head)
        for op in wt_ops:
            candidate.apply(op)
        ok, output = self.verify(candidate.render())
        if not ok:
            return {
                "status": "verification_failed",
                "output": output,
                "hint": "structurally mergeable, but the merged state fails the "
                "test suite; commit a fix to the worktree and merge again",
                "candidate_source": candidate.render(),
            }
        rev = self._commit_to(
            "main",
            wt_ops,
            author,
            f"merge worktree {worktree}",
            merge_parent=wt_head,
            prepared=True,
            expected_parent=main_head,
        )
        self.set_status(worktree, "merged")
        return {
            "status": "merged",
            "rev": rev,
            "merged_source": self.tree("main").render(),
        }

    def resolve_merge(
        self,
        worktree: str,
        resolutions: list[dict],
        author: str = "merge",
        extra_ops: list[dict] | None = None,
    ) -> dict:
        """Merge a conflicted worktree: apply its non-conflicting ops, then the
        given resolutions ({target, code} replaces valid against main's tree),
        then any extra ops (e.g. adding a helper both sides now need)."""
        conflicts = self.find_conflicts(worktree)
        if not conflicts:
            raise VcsError("no conflicts to resolve; call merge instead")
        conflicted = {c["node"] for c in conflicts}
        anc, main_head, wt_head, _mo, wt_ops = self._fork_data(worktree)
        kept = [
            op
            for op in wt_ops
            if not (op["action"] in MODIFYING and op["target"] in conflicted)
        ]
        tree = self.tree_at(main_head)
        for op in kept:
            tree.apply(op)  # already validated + ids assigned on the worktree
        res_ops = self._prepare(
            tree,
            [
                {"action": "replace", "target": r["target"], "code": r["code"]}
                for r in resolutions
            ]
            + (extra_ops or []),
        )
        ok, output = self.verify(tree.render())
        if not ok:
            return {
                "status": "verification_failed",
                "output": output,
                "hint": "the resolved state fails the test suite; nothing was "
                "committed — adjust resolutions/extra_ops and retry",
                "candidate_source": tree.render(),
            }
        rev = self._commit_to(
            "main",
            kept + res_ops,
            author,
            f"merge worktree {worktree} (resolved {sorted(conflicted)})",
            merge_parent=wt_head,
            prepared=True,
            expected_parent=main_head,
        )
        self.set_status(worktree, "merged")
        return {
            "status": "merged",
            "rev": rev,
            "merged_source": self.tree("main").render(),
        }

    # ------------------------------------------------------------ history

    def log(self) -> list[dict]:
        heads = {
            h: n for n, h, *_ in self.db.execute("SELECT name, head_rev FROM branches")
        }
        rows = self.db.execute(
            "SELECT rev, parent_rev, merge_parent, author, message, ops FROM revisions ORDER BY rev"
        ).fetchall()
        return [
            {
                "rev": rev,
                "parent": parent,
                "merge_parent": mp,
                "author": author,
                "message": message,
                "ops": json.loads(ops),
                "head_of": heads.get(rev),
            }
            for rev, parent, mp, author, message, ops in rows
        ]
