#!/bin/bash
# Launch one agent TUI (Claude Code) wired to the AST codebase via MCP only.
# Usage:
#   ./launch-agent.sh feature-1 "Add quantity support ..."
#   ./launch-agent.sh resolver     # only for adopting orphaned worktrees
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
ROLE="${1:?usage: launch-agent.sh <role> [task...]}"
shift || true
TASK="${*:-}"
DB="${ASTDB_PATH:-$DIR/codebase.db}"
CFG="$DIR/.mcp-astdb-$ROLE.json"

cat > "$CFG" <<EOF
{
  "mcpServers": {
    "astdb": {
      "command": "python3",
      "args": ["$DIR/mcp_server.py"],
      "env": { "ASTDB_PATH": "$DB", "ASTDB_AGENT": "$ROLE" }
    }
  }
}
EOF

COMMON_SYS="You are agent '$ROLE' in a multi-agent experiment where the codebase is NOT stored
in files. It lives as an AST in a shared SQLite database, and you access it ONLY through the
astdb MCP tools. There is no filesystem for you: do not try to read or write files or run
shell commands — everything (reading code, editing, committing, merging, even running the
program) happens through astdb tools. Source text you see from read_source is a projection
rendered from the database. Edits are structural operations targeting node ids from
list_nodes. Other agents are working concurrently against the same database."

if [[ "$ROLE" == "resolver" ]]; then
  SYS="$COMMON_SYS

You are an ON-DEMAND RESOLVER, spawned because a pending worktree was left behind (its
author is gone). There is no standing merge role in this system: integration is automatic
and conflicts normally bounce back to their author. You adopt the orphans:
 1. list_worktrees; for each worktree with status 'open' or 'ready', inspect it
    (read_source, history) to understand the author's intent.
 2. Call merge_worktree on it.
    - 'merged': done — the suite already passed on the merged state.
    - 'conflict': study base_code/main_code/worktree_code per node plus both sources, write
      resolution code that PRESERVES BOTH SIDES' INTENT, call resolve_merge (extra_ops for
      any new helper code). Never discard one side's feature.
    - 'verification_failed': commit a fix to that worktree, then merge_worktree again.
 3. When no worktrees remain open or ready, show history and give a short report."
  PROMPT="Adopt and integrate any leftover open/ready worktrees now."
else
  SYS="$COMMON_SYS

You are a FEATURE AGENT. The default path is committing straight to main — no branch, no
merge:
 1. read_source and list_nodes on main; note the rev number shown (your base_rev).
 2. Build ONE transaction of structural ops for your task (module root is node #1),
    INCLUDING ops that add or extend a test_* function covering your change — the stored
    suite is the codebase's QA and must grow with it.
 3. Call commit_to_main with message, ops, test_code (extra asserts), and base_rev.
    - 'committed': done — the whole suite passed on the exact state that landed.
    - 'conflict': another agent changed nodes you touched; re-read main (new rev), adapt
      your ops to the current code, retry. Do not drop their change.
    - 'verification_failed': your candidate broke a test; read the output, fix your ops,
      retry. Nothing landed.
 4. Only if your task genuinely needs persistent intermediate unverified states, use
    create_worktree / commit / run_program, then mark_ready — which auto-merges. If
    mark_ready returns 'conflict' or 'verification_failed', YOU finish the integration
    (resolve_merge, or fix and mark_ready again); there is no separate merge agent.
Then give a short report: node-level changes, tests added, and how many retries you needed."
  PROMPT="Your task: $TASK"
fi

# commit_to_main may block while queued transactions ahead verify/land; give
# MCP tool calls a generous client-side timeout.
export MCP_TOOL_TIMEOUT=600000

# --tools "" removes ALL built-in tools (Bash/Read/Write/Edit/Glob/Grep/...)
# from the session — they don't exist, rather than being deny-ruled. The only
# tools the agent has are the astdb MCP tools. --disallowedTools stays as a
# second layer in case a future CLI changes --tools semantics.
exec claude \
  --mcp-config "$CFG" \
  --strict-mcp-config \
  --tools "" \
  --allowedTools "mcp__astdb" \
  --disallowedTools "Bash Read Write Edit MultiEdit NotebookEdit Glob Grep WebFetch WebSearch Task TodoWrite" \
  --append-system-prompt "$SYS" \
  "$PROMPT"
