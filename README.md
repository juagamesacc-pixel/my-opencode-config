Two separate answers:

## 1. Clone → MCP: yes, but with one required edit
The registration **is** shipped in the repo — `opencode.json` on GitHub already contains the `mcp` block, and the script itself is in the repo at `.config/opencode/tools/memory_mcp.py`. So you won't have to write any config from scratch.

What you *do* have to fix: the block hard-codes **this workspace's paths** (`/content/opencode-agent2/...`), which won't exist on your machine. After cloning, change these three spots in `~/.config/opencode/opencode.json`:

- **`command`** → point to where you cloned it, e.g. `/home/you/.config/opencode/tools/memory_mcp.py`
- **`--root`** → your project's memory folder, e.g. `/home/you/myproject/.memory`
- **`cwd`** → that project folder

Save, start opencode — done, the memory tools appear automatically (it needs `python3` on PATH, nothing else to install).

One more thing: the repo ships the *tool*, not the *memory*. The actual memory files (GOAL.md, LOG.md, …) live in your project's `.memory` folder. You don't need to create them — the tool builds them on first write. It just starts out empty instead of pre-seeded like this workspace.

## 2. Persistence: it's the opposite of per-session
The memory is **plain files on disk** — persistent, and shared by everything:

- **Every session, every conversation, every agent** in that setup reads and writes the *same* `.memory` folder. Close opencode, reboot, come back tomorrow — it's all still there.
- The only thing that resets per session is the model's *conversation* memory (the chat itself). That's the whole point of the design: chat is disposable, files are the durable layer the team rehydrates from at the start of every task.
- The files evolve as work happens rather than clearing: `GOAL` and `STATE` get replaced with the latest project's current goal/status, while `LOG`, `DECISIONS`, and `EVOLUTION` only ever grow (your team's history and precedents).

So in one line: **change the three paths once, and you get a permanent shared team memory that outlives any session.** If you later work on two different projects and want *separate* memories for each, point `--root` at each project's own `.memory` (one path edit per project) — right now the config gives you a single shared store for everything you run with it.
