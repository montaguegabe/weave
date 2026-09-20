#!/usr/bin/env python3
"""MCP server (stdio) exposing the AST-in-SQLite codebase to coding agents.

No filesystem involved: code lives only in the SQLite store, agents edit it
with structural operations, and 'running the program' executes the rendered
projection in-memory.

Env:
  ASTDB_PATH   path to the shared SQLite codebase (required)
  ASTDB_AGENT  author name recorded on commits (default: 'agent')
"""

import json
import os
import sys
import traceback

from astvcs import VCS, VcsError

DB_PATH = os.environ.get("ASTDB_PATH", "codebase.db")
AGENT = os.environ.get("ASTDB_AGENT", "agent")

OPS_SCHEMA = {
    "type": "array",
    "description": "Structural operations, applied in order.",
    "items": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "replace",
                    "replace_subtree",
                    "insert_after",
                    "append_child",
                    "delete",
                ],
            },
            "target": {"type": "integer", "description": "node id from list_nodes"},
            "code": {
                "type": "string",
                "description": "Python source (not needed for delete)",
            },
        },
        "required": ["action", "target"],
    },
}

TOOLS = [
    {
        "name": "list_worktrees",
        "description": "List all branches/worktrees with head revision, status (trunk|open|ready|merged) and task.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_worktree",
        "description": "Create a new worktree forked from main's current head. Do this once, then commit your work to it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "task": {"type": "string", "description": "what this worktree is for"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "read_source",
        "description": "Render a branch/worktree's code as Python source text (a projection; the AST database is canonical).",
        "inputSchema": {
            "type": "object",
            "properties": {"branch": {"type": "string", "default": "main"}},
        },
    },
    {
        "name": "list_nodes",
        "description": "Show a branch's AST node tree with node ids. Use these ids as op targets. Format: #id [kind] first-line-of-code.",
        "inputSchema": {
            "type": "object",
            "properties": {"branch": {"type": "string", "default": "main"}},
        },
    },
    {
        "name": "commit_to_main",
        "description": (
            "PREFERRED for self-contained changes: commit structural ops DIRECTLY to main as a "
            "verified transaction. The candidate state (current main + your ops) must pass the "
            "entire stored test suite (every top-level test_* function) plus your test_code "
            "asserts, or nothing lands — main only advances through verified states. Include ops "
            "that add/extend a test_* function covering your change so the suite grows. Pass "
            "base_rev = the main rev you read (shown by read_source/list_nodes); if nodes you "
            "modify changed since then you get status 'conflict' — re-read main, adapt, retry. "
            "On 'verification_failed' fix your ops and retry. No worktree or merge needed. "
            "Concurrency: commits go through a speculative merge queue — verification runs in "
            "parallel with other agents' pending commits against the predicted landing state, "
            "so the call may block briefly while transactions ahead of you land. Tests whose "
            "dependencies didn't change are skipped via a shared content-hash cache. Set "
            "dry_run=true to run the full verification WITHOUT committing (free trial run)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "ops": OPS_SCHEMA,
                "test_code": {
                    "type": "string",
                    "description": "extra assert statements run after the suite (optional but recommended)",
                },
                "base_rev": {
                    "type": "integer",
                    "description": "the main revision your ops were built against",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "verify the candidate state without committing anything",
                },
            },
            "required": ["message", "ops"],
        },
    },
    {
        "name": "queue_status",
        "description": "Show the speculative commit queue: recent tickets with author, message, and state (pending | landed | evicted).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "commit",
        "description": (
            "Commit structural ops to YOUR OWN pending worktree (for multi-step work only; "
            "prefer commit_to_main for self-contained changes). Not verified until merge. Actions: "
            "replace = swap one statement's code, or a def/class node's header line only; "
            "replace_subtree = rewrite a whole def/class/statement (new code replaces node and its body); "
            "insert_after = add sibling statement(s)/def(s) after target; "
            "append_child = add statement(s)/def(s) at the end of a module/def/class body (module root is node #1); "
            "delete = remove node and its body. "
            "Get node ids from list_nodes on your worktree first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "worktree": {"type": "string"},
                "message": {"type": "string"},
                "ops": OPS_SCHEMA,
            },
            "required": ["worktree", "message", "ops"],
        },
    },
    {
        "name": "run_program",
        "description": (
            "Execute a branch's rendered code in-memory (no files), then its stored test suite "
            "(all test_* functions), then your optional test_code asserts. Returns stdout + errors."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "branch": {"type": "string", "default": "main"},
                "test_code": {"type": "string"},
            },
        },
    },
    {
        "name": "mark_ready",
        "description": (
            "Submit your pending worktree for integration. Auto-merges into main when its ops "
            "touch different AST nodes than what landed since the fork AND the merged state "
            "passes the test suite. If it returns 'conflict' or 'verification_failed', the "
            "worktree stays open and YOU (the author) finish the job: resolve_merge for "
            "conflicts, or commit a fix and call mark_ready again."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"worktree": {"type": "string"}},
            "required": ["worktree"],
        },
    },
    {
        "name": "merge_worktree",
        "description": (
            "Integrate a pending worktree into main (mark_ready calls this automatically — use "
            "directly when adopting someone else's leftover worktree). Clean if its ops touch "
            "different AST nodes than what landed since the fork; otherwise returns a node-level "
            "conflict report (base/main/worktree code per node) for resolve_merge."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"worktree": {"type": "string"}},
            "required": ["worktree"],
        },
    },
    {
        "name": "resolve_merge",
        "description": (
            "Complete a conflicted worktree integration. resolutions = final code for each conflicted "
            "node, combining both sides' intent ({target, code}, code rules same as the 'replace' "
            "action). extra_ops = optional additional ops applied on top (same schema as commit ops)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "worktree": {"type": "string"},
                "resolutions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "target": {"type": "integer"},
                            "code": {"type": "string"},
                        },
                        "required": ["target", "code"],
                    },
                },
                "extra_ops": OPS_SCHEMA,
            },
            "required": ["worktree", "resolutions"],
        },
    },
    {
        "name": "history",
        "description": "Show the revision DAG: every committed transaction with author, message, ops and branch heads.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# ------------------------------------------------------------------ handlers


def vcs() -> VCS:
    return VCS(DB_PATH)


def t_list_worktrees(args):
    return json.dumps(vcs().worktrees(), indent=2)


def t_create_worktree(args):
    head = vcs().create_worktree(args["name"], args.get("task", ""))
    return f"worktree {args['name']!r} created from main @ rev {head}"


def t_read_source(args):
    v = vcs()
    branch = args.get("branch", "main")
    src = v.tree(branch).render() or "<empty module>"
    return f"[{branch} @ rev {v._head(branch)}]\n{src}"


def t_list_nodes(args):
    v = vcs()
    branch = args.get("branch", "main")
    return f"[{branch} @ rev {v._head(branch)}]\n{v.tree(branch).outline()}"


def t_commit_to_main(args):
    result = vcs().commit_to_main(
        args["ops"],
        AGENT,
        args["message"],
        test_code=args.get("test_code"),
        base_rev=args.get("base_rev"),
        dry_run=bool(args.get("dry_run")),
    )
    return json.dumps(result, indent=2)


def t_queue_status(args):
    return json.dumps(vcs().queue_status(), indent=2)


def t_commit(args):
    v = vcs()
    rev = v.commit(args["worktree"], args["ops"], AGENT, args["message"])
    return (
        f"committed rev {rev} to {args['worktree']}\n\n"
        f"--- {args['worktree']} now renders as ---\n{v.tree(args['worktree']).render()}"
    )


def t_run_program(args):
    v = vcs()
    source = v.tree(args.get("branch", "main")).render()
    # advisory run through the cached verifier: passes recorded here make the
    # commit-time gate cheaper for everyone
    ok, output = v.verify(source, args.get("test_code"))
    return ("OK\n" if ok else "FAILED\n") + output


def t_mark_ready(args):
    v = vcs()
    name = args["worktree"]
    v.set_status(name, "ready")
    result = v.merge(name, author=AGENT)
    if result["status"] != "merged":
        v.set_status(name, "open")  # still the author's problem
        result["hint"] = (
            "auto-integration did not land; the worktree is still open and yours. "
            "On 'conflict': call resolve_merge with resolution code combining both "
            "sides' intent. On 'verification_failed': commit a fix to this worktree "
            "and call mark_ready again."
        )
    return json.dumps(result, indent=2)


def t_merge_worktree(args):
    result = vcs().merge(args["worktree"], author=AGENT)
    return json.dumps(result, indent=2)


def t_resolve_merge(args):
    result = vcs().resolve_merge(
        args["worktree"],
        args["resolutions"],
        author=AGENT,
        extra_ops=args.get("extra_ops"),
    )
    return json.dumps(result, indent=2)


def t_history(args):
    lines = []
    for e in vcs().log():
        mp = f"  (merges rev {e['merge_parent']})" if e["merge_parent"] else ""
        head = f"  <- head of {e['head_of']}" if e["head_of"] else ""
        lines.append(
            f"rev {e['rev']}  parent={e['parent']}  {e['author']}: {e['message']}{mp}{head}"
        )
        for op in e["ops"]:
            code = op.get("code", "")
            first = code.splitlines()[0] if code else ""
            more = " ..." if len(code.splitlines()) > 1 else ""
            lines.append(f"    {op['action']} #{op.get('target')} {first}{more}")
    return "\n".join(lines)


HANDLERS = {t["name"]: globals()[f"t_{t['name']}"] for t in TOOLS}


# ------------------------------------------------------------------ protocol


def send(msg: dict):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, msg_id = req.get("method"), req.get("id")
        if method == "initialize":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": req["params"].get(
                            "protocolVersion", "2025-06-18"
                        ),
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "astdb", "version": "0.1.0"},
                    },
                }
            )
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = req["params"]["name"]
            args = req["params"].get("arguments") or {}
            try:
                text, is_error = HANDLERS[name](args), False
            except (VcsError, KeyError, TypeError, ValueError) as e:
                text, is_error = f"error: {e}", True
            except Exception:
                text, is_error = (
                    f"internal error:\n{traceback.format_exc(limit=5)}",
                    True,
                )
            send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": text}],
                        "isError": is_error,
                    },
                }
            )
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        elif msg_id is not None:  # unknown request (notifications are ignored)
            send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"method not found: {method}"},
                }
            )


if __name__ == "__main__":
    main()
