"""astdb: a proof-of-concept "code as AST in a database" store.

Python source is ingested into SQLite as a tree of nodes (module -> defs ->
statements). Nodes have stable integer IDs. Agents edit by committing
transactions of structural operations (replace/insert/delete) that target
node IDs, with optimistic concurrency control: a transaction built against a
stale revision is auto-rebased if its target nodes don't overlap with what
landed in between, and rejected with a precise node-level conflict otherwise.

Text files are just a projection: render() regenerates source on demand.
"""

from __future__ import annotations

import ast
import json
import sqlite3
import textwrap
from dataclasses import dataclass, field

INDENT = "    "

SCHEMA = """
CREATE TABLE nodes (
    id        INTEGER PRIMARY KEY,
    parent_id INTEGER REFERENCES nodes(id),
    position  INTEGER NOT NULL,          -- order among siblings
    kind      TEXT NOT NULL,             -- module | def | class | stmt
    header    TEXT NOT NULL DEFAULT '',  -- 'def foo(x):' for containers, code for stmts
    deleted   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE revisions (
    rev       INTEGER PRIMARY KEY AUTOINCREMENT,
    base_rev  INTEGER,
    author    TEXT,
    ops       TEXT NOT NULL              -- JSON list of operations
);
"""


@dataclass
class Op:
    """One structural edit. target is a node id; code is Python source."""

    action: str  # replace | insert_after | append_child | delete
    target: int
    code: str = ""

    def to_json(self) -> dict:
        return {"action": self.action, "target": self.target, "code": self.code}


class Conflict(Exception):
    def __init__(self, message: str, node_id: int, theirs: str, ours: str):
        super().__init__(message)
        self.node_id = node_id
        self.theirs = theirs
        self.ours = ours


@dataclass
class Transaction:
    author: str
    base_rev: int
    ops: list[Op] = field(default_factory=list)

    def replace(self, node_id: int, code: str):
        self.ops.append(Op("replace", node_id, code))

    def insert_after(self, node_id: int, code: str):
        self.ops.append(Op("insert_after", node_id, code))

    def append_child(self, node_id: int, code: str):
        self.ops.append(Op("append_child", node_id, code))

    def delete(self, node_id: int):
        self.ops.append(Op("delete", node_id))


class AstDB:
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    # ---------------------------------------------------------------- ingest

    def ingest(self, source: str) -> int:
        """Parse Python source and store it as the module tree. Returns rev 0."""
        tree = ast.parse(source)
        lines = source.splitlines()
        root = self._add_node(None, 0, "module", "")
        self._ingest_body(tree.body, lines, root)
        self.db.execute(
            "INSERT INTO revisions (base_rev, author, ops) VALUES (NULL, 'ingest', '[]')"
        )
        self.db.commit()
        return self.head()

    def _ingest_body(self, body: list[ast.stmt], lines: list[str], parent: int):
        for pos, node in enumerate(body):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                first_child_line = node.body[0].lineno
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                header = "\n".join(lines[start - 1 : first_child_line - 1]).strip()
                kind = "class" if isinstance(node, ast.ClassDef) else "def"
                nid = self._add_node(parent, pos, kind, header)
                self._ingest_body(node.body, lines, nid)
            else:
                seg = ast.get_source_segment("\n".join(lines), node) or ast.unparse(
                    node
                )
                self._add_node(parent, pos, "stmt", textwrap.dedent(seg))

    def _add_node(self, parent: int | None, pos: int, kind: str, header: str) -> int:
        cur = self.db.execute(
            "INSERT INTO nodes (parent_id, position, kind, header) VALUES (?, ?, ?, ?)",
            (parent, pos, kind, header),
        )
        return cur.lastrowid

    # ---------------------------------------------------------------- query

    def head(self) -> int:
        return self.db.execute(
            "SELECT COALESCE(MAX(rev), 0) FROM revisions"
        ).fetchone()[0]

    def find(self, containing: str, under: int | None = None) -> int:
        """Return the id of the single live node whose code contains `containing`."""
        sql = "SELECT id FROM nodes WHERE deleted = 0 AND header LIKE ?"
        args: list = [f"%{containing}%"]
        if under is not None:
            sql += " AND parent_id = ?"
            args.append(under)
        rows = self.db.execute(sql, args).fetchall()
        if len(rows) != 1:
            raise LookupError(f"{len(rows)} nodes match {containing!r}")
        return rows[0][0]

    def path(self, node_id: int) -> str:
        """Human-readable path like total_price/stmt[1]."""
        parts = []
        while node_id is not None:
            kind, header, parent, pos = self.db.execute(
                "SELECT kind, header, parent_id, position FROM nodes WHERE id = ?",
                (node_id,),
            ).fetchone()
            if kind in ("def", "class"):
                parts.append(
                    header.split("(")[0]
                    .removeprefix("def ")
                    .removeprefix("class ")
                    .rstrip(":")
                )
            elif kind == "stmt":
                parts.append(f"stmt[{pos}]")
            node_id = parent
        return "/".join(reversed(parts)) or "<module>"

    # ---------------------------------------------------------------- commit

    def commit(self, tx: Transaction) -> int:
        """Apply a transaction with optimistic concurrency control.

        If other transactions landed since tx.base_rev, tx is rebased: it
        applies cleanly unless it touches a node those transactions already
        replaced or deleted — in which case a node-level Conflict is raised.
        """
        landed_since = self._touched_since(tx.base_rev)
        for op in tx.ops:
            if op.target in landed_since:
                other_author, other_code = landed_since[op.target]
                raise Conflict(
                    f"{tx.author}: node {op.target} ({self.path(op.target)}) was "
                    f"already modified by {other_author} since rev {tx.base_rev}",
                    node_id=op.target,
                    theirs=other_code,
                    ours=op.code,
                )
        self._apply(tx.ops)
        self.db.execute(
            "INSERT INTO revisions (base_rev, author, ops) VALUES (?, ?, ?)",
            (tx.base_rev, tx.author, json.dumps([o.to_json() for o in tx.ops])),
        )
        self.db.commit()
        return self.head()

    def _touched_since(self, base_rev: int) -> dict[int, tuple[str, str]]:
        touched: dict[int, tuple[str, str]] = {}
        for author, ops_json in self.db.execute(
            "SELECT author, ops FROM revisions WHERE rev > ?", (base_rev,)
        ):
            for op in json.loads(ops_json):
                if op["action"] in ("replace", "delete"):
                    touched[op["target"]] = (author, op.get("code", ""))
        return touched

    def _apply(self, ops: list[Op]):
        for op in ops:
            if op.action == "replace":
                self.db.execute(
                    "UPDATE nodes SET header = ? WHERE id = ?", (op.code, op.target)
                )
            elif op.action == "delete":
                self.db.execute(
                    "UPDATE nodes SET deleted = 1 WHERE id = ?", (op.target,)
                )
            elif op.action == "insert_after":
                parent, pos = self.db.execute(
                    "SELECT parent_id, position FROM nodes WHERE id = ?", (op.target,)
                ).fetchone()
                self.db.execute(
                    "UPDATE nodes SET position = position + 1 WHERE parent_id = ? AND position > ?",
                    (parent, pos),
                )
                self._insert_code(parent, pos + 1, op.code)
            elif op.action == "append_child":
                (maxpos,) = self.db.execute(
                    "SELECT COALESCE(MAX(position), -1) FROM nodes WHERE parent_id = ?",
                    (op.target,),
                ).fetchone()
                self._insert_code(op.target, maxpos + 1, op.code)
            else:
                raise ValueError(f"unknown op {op.action}")

    def _insert_code(self, parent: int, pos: int, code: str):
        """Insert a snippet (possibly a whole def) as a subtree at parent/pos."""
        code = textwrap.dedent(code).strip("\n")
        stmts = ast.parse(code).body
        lines = code.splitlines()
        if len(stmts) == 1 and isinstance(
            stmts[0], (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            node = stmts[0]
            kind = "class" if isinstance(node, ast.ClassDef) else "def"
            header = "\n".join(lines[: node.body[0].lineno - 1]).strip()
            nid = self._add_node(parent, pos, kind, header)
            self._ingest_body(node.body, lines, nid)
        else:
            self._add_node(parent, pos, "stmt", code)

    # ---------------------------------------------------------------- render

    def render(self, at_ops: list[Op] | None = None) -> str:
        """Project the tree back to Python source text.

        With at_ops, renders a hypothetical: current state + those ops,
        rolled back afterwards (a 'branch preview' without committing).
        """
        if at_ops is not None:
            self.db.execute("SAVEPOINT preview")
            try:
                self._apply(at_ops)
                return self._render_children(self._root(), 0)
            finally:
                self.db.execute("ROLLBACK TO preview")
                self.db.execute("RELEASE preview")
        return self._render_children(self._root(), 0)

    def _root(self) -> int:
        return self.db.execute(
            "SELECT id FROM nodes WHERE parent_id IS NULL"
        ).fetchone()[0]

    def _render_children(self, parent: int, depth: int) -> str:
        out: list[str] = []
        rows = self.db.execute(
            "SELECT id, kind, header FROM nodes WHERE parent_id = ? AND deleted = 0 "
            "ORDER BY position, id",
            (parent,),
        ).fetchall()
        for nid, kind, header in rows:
            block = textwrap.indent(header, INDENT * depth)
            if kind in ("def", "class"):
                body = self._render_children(nid, depth + 1)
                block += "\n" + body if body else "\n" + INDENT * (depth + 1) + "pass"
                if depth == 0:
                    block += "\n"
            out.append(block)
        sep = "\n\n" if depth == 0 else "\n"
        return sep.join(out).rstrip("\n") + ("\n" if depth == 0 else "")

    # ---------------------------------------------------------------- misc

    def log(self) -> list[tuple[int, int | None, str, str]]:
        return self.db.execute(
            "SELECT rev, base_rev, author, ops FROM revisions"
        ).fetchall()

    def dump_nodes(self) -> list[tuple]:
        return self.db.execute(
            "SELECT id, parent_id, position, kind, deleted, header FROM nodes ORDER BY id"
        ).fetchall()
