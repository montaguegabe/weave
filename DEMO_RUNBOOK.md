# Live Demo Runbook — `no-merge-conflicts`

**The pitch:** the codebase is not files. It's an AST in SQLite. Coding agents
commit structural transactions against node IDs through a speculative merge
queue, so concurrent work integrates automatically, conflicts are rare
node-level events handled by whoever caused them, and every revision of main
is verified-green by construction.

**Total time:** ~6–8 minutes. **Cast:** two Claude Code agents, you, and a
watcher terminal. No merge agent — that's part of the point.

---

## Setup (before the audience, 30 seconds)

```bash
cd ~/Projects/hacks/no-merge-conflicts
python3 init_db.py --fresh
```

Open a **watcher terminal**:

```bash
watch -n1 "python3 astctl.py queue; echo; python3 astctl.py log"
```

Have two more terminals ready for the agents.

---

## Act 1 — There is no file (~45 seconds)

```bash
python3 astctl.py render     # human-readable Python… but it's a projection
python3 astctl.py outline    # the actual codebase: nodes with IDs in SQLite
ls                           # no inventory.py anywhere on disk
```

> "Here's the code you'd expect — except I didn't `cat` a file, I asked the
> database to render it. The outline is what's actually stored; the text is
> disposable output, like a query result. When agents 'edit code' they never
> touch this text — they commit operations against those node IDs."

Optional flourish for skeptics ("isn't this files with extra steps?"):

```bash
sqlite3 codebase.db "select rev, author, message from revisions"
python3 astctl.py render     # render again — same text, no identity, pure view
```

Text is **view**; the tree is **truth**.

---

## Act 2 — Launch the race (~30 seconds to start)

Two terminals (or `./demo.sh` to spawn both):

```bash
./launch-agent.sh feature-1 "Add quantity support: Item takes a quantity argument (default 1); total_price charges price * quantity per item."
```

```bash
./launch-agent.sh feature-2 "Add discount support: apply_discount(subtotal, code) where SAVE10 gives 10% off, else no discount; total_price takes an optional discount_code applied to the subtotal before tax."
```

Both tasks touch `total_price` **on purpose**.

> "Each agent is a stock Claude Code session with every built-in tool removed —
> no filesystem, no shell. Its entire world is eleven MCP tools over the
> database. And notice what's missing: there is no merge agent, no coordinator.
> Integration is the silent default."

---

## Act 3 — What to point at while they run (~2–3 minutes)

**In the watcher:**
- Tickets appearing in the queue; `landed` vs `evicted` states.
- The revision log growing — every entry landed only after the full suite
  passed on the exact state that landed.

**In the agents' TUIs:**
- The **losing** agent's `conflict` or `verification_failed` result: it names
  the exact node IDs and both versions of the code. Watch it re-read main and
  rebuild its ops **preserving the other agent's change** — that's the thesis
  on screen.
- Commit outputs showing `suite passed — ran: … cache-skipped (dependencies
  unchanged): …` — the dependency-hash test cache at work.

**Talking points while waiting:**
- "Verification runs concurrently against *predicted landing states* — a merge
  queue, like bors — landing is just a hash match."
- "Textual merge conflicts can't exist here; only genuine same-node collisions
  conflict, and they return as structured data small enough to hand to an LLM."
- "Every main revision is green by construction — there is no 'main is broken,
  who did it' state."

---

## Act 4 — Finale (~1 minute)

```bash
python3 astctl.py render     # both features interleaved in one function
python3 astctl.py log        # the whole history; every revision was gated
python3 astctl.py queue      # the race's fossil record: landed + evicted tickets
```

> "Two agents edited the same function at the same time. Nobody merged
> anything. The conflict happened, was described precisely, and was resolved
> by the agent that caused it — in seconds."

---

## Optional encores

- **Dry run:** ask an agent to `commit_to_main` with `dry_run: true` — full
  verification, nothing lands, and it warms the shared test cache.
- **Orphan rescue:** ask an agent to build something in a worktree
  (`create_worktree` + `commit`), kill its terminal mid-flight, then:
  `./launch-agent.sh resolver` — an on-demand agent adopts and integrates it.
- **Time travel:** `python3 -c "from astvcs import VCS; v=VCS('codebase.db'); print(v.tree_at(2).render())"`
  — render main as it existed at any revision.

---

## Contingencies

| Symptom | Fix |
|---|---|
| An agent idles mid-task | Type `continue` in its TUI |
| Timing serializes; no conflict occurs | Coin flip — `python3 init_db.py --fresh` and relaunch, or show the recorded race logs (`race1.log` / `race2.log` in the session scratchpad) |
| Agent tries something odd with ops | It gets a precise `VcsError` back and self-corrects; let it |
| Store gets weird | `python3 init_db.py --fresh` — total reset in one command |

## One-liner if asked "so what?"

> Files and git are a shared-memory format designed for humans. This is what
> version control looks like when the program itself — not its text — is the
> thing under management: transactions instead of branches, verification
> instead of review queues, and conflicts as rare structured events instead of
> a daily tax.
