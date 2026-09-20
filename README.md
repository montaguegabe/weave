# no-merge-conflicts

**Code stored as an AST in SQLite instead of files** — with worktrees, commits,
and merges as native operations on the syntax tree, so concurrent coding agents
almost never conflict, and when they truly do, the conflict is node-level and
small enough to hand to another LLM.

Inspired by [this ChatGPT conversation](https://chatgpt.com/share/6ab04c8f-3fb4-83ea-8ce8-1571c30f2ea6)
on AST-database research (Unison, Intentional Programming, JetBrains MPS).

## The multi-agent demo

```
./demo.sh
```

This seeds `codebase.db` and opens **two Claude Code TUIs** in Terminal
windows, each restricted to the `astdb` MCP server (filesystem and shell tools
disabled — the code never exists as a file):

- **feature-1** — adds quantity support to the inventory module
- **feature-2** — adds discount support

There is **no merge agent**: both agents commit straight to main through the
speculative merge queue (`commit_to_main`). The two tasks deliberately overlap
(both touch `total_price`), so expect disjoint ops to land automatically while
the racing agent that loses gets a node-level `conflict` or a red-suite
`verification_failed` bounced back — and adapts on its own. Integration is the
silent default; conflict is a structured event handled by whoever caused it.

Watch from another terminal:

```
python3 astctl.py status | render | outline | log | queue
```

Or launch agents individually: `./launch-agent.sh feature-1 "task..."`. If a
pending worktree is ever orphaned (its author died mid-flight), spawn
`./launch-agent.sh resolver` to adopt and integrate it.

## Architecture

```
Claude Code TUI (feature-1) ─┐
Claude Code TUI (feature-2) ─┼─ MCP (stdio) ─ mcp_server.py ─ astvcs.py ─ codebase.db (SQLite, WAL)
Claude Code TUI (resolver)  ─┘   (resolver only if a worktree is orphaned)
```

- **`astvcs.py`** — the store. Everything is an operation log: a revision is a
  transaction of structural ops (`replace`, `replace_subtree`, `insert_after`,
  `append_child`, `delete`) targeting stable node ids; any state is
  materialized by replaying the op chain. The default write path is
  `commit_to_main` (speculative queue, below). Worktrees survive as *pending
  transactions* for work that needs persistent unverified intermediate states;
  `mark_ready` auto-merges them, bouncing conflicts back to the author.
- **`mcp_server.py`** — dependency-free MCP stdio server exposing 12 tools:
  `commit_to_main` (+ `dry_run`), `queue_status`, `list_worktrees`,
  `create_worktree`, `read_source`, `list_nodes`, `commit`, `run_program`
  (executes the rendered projection in-memory — even "running the code" never
  touches disk, and warms the shared test cache), `mark_ready`,
  `merge_worktree`, `resolve_merge`, `history`.
- **`launch-agent.sh`** — wraps `claude` with `--strict-mcp-config`, an
  allowlist of only the astdb server, all built-in tools removed (`--tools ""`),
  and a role system prompt (feature agent vs on-demand resolver).

## Speculative parallel QA + dependency-hashed test caching

`commit_to_main` is a **speculative merge queue** (bors/GitHub-merge-queue
style), not a lock-the-world gate:

- Enqueue takes a millisecond write lock (OCC conflict checks against landed
  history *and* queued transactions, node-id assignment). Verification runs
  **outside the lock**, concurrently with other committers, against the
  *predicted landing state*: landed head + queued-ahead transactions + your
  ops.
- Landing is a hash check: if the state that passed verification is
  byte-identical to the real landing state, it lands instantly with no re-run.
  An eviction ahead invalidates the hash and triggers re-verification. Main
  only ever advances through verified states, in ticket order — every main
  revision is green by construction.
- **Test caching**: each `test_*` function's pass is cached against the
  content hash of its dependency closure (the top-level defs it transitively
  references, plus module globals). A test re-runs only when code it can
  actually observe changed. Advisory runs (`run_program`) and `dry_run`
  commits warm the same cache, so the landing gate is often nearly free.

Measured in `test_speculative.py` with a 0.6s suite: 3 concurrent disjoint
commits land in ~0.6s wall (serial gating: ≥1.8s), a racing buggy commit is
evicted while the good one lands, and a later commit that doesn't touch the
slow test verifies in 0.01s via the cache.

## Tests

- `python3 test_vcs.py` — full VCS flow: two worktrees merging cleanly, a real
  two-node conflict, LLM-style resolution, executing the merged program.
- The MCP layer was verified with a raw JSON-RPC smoke test and a headless
  `claude -p` session that created a worktree, committed, verified, and marked
  ready — end to end through MCP.

## v1: the original single-file POC

`astdb.py` + `demo.py` — the first iteration: a linear store demonstrating
git-conflicts-vs-structural-merge head to head (`python3 demo.py`).

## Honest limitations

- Statement-level granularity: edits inside the same statement still conflict.
- No behavioral-conflict detection (A deletes a function B now calls); the
  commit-time suite run is the safety net (grow the suite with every change).
- Comments and blank lines inside bodies aren't preserved as nodes.
- Python-only; a real system would use tree-sitter and keep trivia.

## Prior art

Unison (AST-in-SQLite, content-addressed, own VCS in UCM + Unison Share),
JetBrains MPS, Intentional Programming, Pijul/Darcs (patch commutation), and
mergiraf (tree-sitter structural merges as a git merge driver).
