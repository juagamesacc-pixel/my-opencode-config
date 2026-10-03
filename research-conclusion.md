# Research Conclusion: Adding Subagents and Agents to OpenCode Configs

## Overview

This research explored how to add subagents and agents to OpenCode configurations, both through JSON configuration and markdown files, and how to customize built-in agent prompts.

## Key Findings

### 1. Agent Configuration Methods

**JSON Configuration (`opencode.json`):**

- **Built-in agents** (`build`, `plan`) are configured under the top-level `agent` key:
  ```json
  {
    "agent": {
      "build": {
        "mode": "primary",
        "model": "anthropic/claude-sonnet-4-20250514",
        "prompt": "{file:./prompts/build.txt}",
        "permission": { "edit": "allow", "bash": "allow" }
      },
      "plan": {
        "mode": "primary",
        "model": "anthropic/claude-haiku-4-20250514",
        "permission": { "edit": "deny", "bash": "deny" }
      }
    }
  }
  ```

- **Custom agents** are defined under the top-level `agents` key:
  ```json
  {
    "agents": {
      "reviewer": {
        "description": "Reviews code for quality",
        "mode": "subagent",
        "model": "anthropic/claude-sonnet-4-20250514",
        "permissions": [{ "action": "edit", "resource": "*", "effect": "deny" }]
      }
    }
  }
  ```

**Markdown Agent Files:**

- **Global**: `~/.config/opencode/agents/<name>.md`
- **Project**: `.opencode/agents/<name>.md`

Markdown format with YAML frontmatter:
```markdown
---
description: Reviews code for quality
mode: subagent
model: anthropic/claude-sonnet-4-20250514
---
You are a code reviewer...
```

### 2. Customizing Built-in Agent Prompts

- **`prompt` config field**: Sets a custom prompt file for any agent:
  ```json
  "build": { "prompt": "{file:./prompts/my-build.txt}" }
  "plan": { "prompt": "{file:./prompts/my-plan.txt}" }
  ```

- **V2 override**: Use `agents.plan.system` to replace the planner's system prompt:
  ```json
  {
    "agents": {
      "plan": {
        "system": "You are a planning expert. Focus on..."
      }
    }
  }
  ```

- **Per-agent prompt merging**: Config values merge in order - later `prompt` values replace earlier ones. The `prompt` field supports `{file:path}` syntax for external files.

### 3. Agent Modes

- **`primary`**: Runs as the main agent for a session (Tab cycle). Default for custom agents.
- **`subagent`**: Runs only in child sessions via the `task` tool or `@` mentions.
- **`all`**: Runs as either primary or subagent.

### 4. Permission System

- Per-agent permissions control tool access using `allow`/`ask`/`deny`
- Rules are ordered; last matching rule wins
- Common actions: `edit`, `bash`, `shell`, `subagent`, `read`, `glob`, `grep`, `webfetch`, `websearch`, `skill`
- Resources can use glob patterns for flexible matching

### 5. Built-in Agents

OpenCode includes:
- **Build** (`primary`): Default coding agent with all tools enabled
- **Plan** (`primary`): Restricted agent for planning/analysis
- **General** (`subagent`): Research and multi-step work
- **Explore** (`subagent`): Codebase exploration without editing files

## Implementation Summary

To add subagents and agents to OpenCode configs:

1. **For JSON config**: Add entries under `agent` (for built-ins) or `agents` (for custom) in `opencode.json`

2. **For markdown agents**: Create `.md` files in `~/.config/opencode/agents/` (global) or `.opencode/agents/` (project)

3. **To customize built-in prompts**: Use the `prompt` field for file-based prompts or `system` field (V2) for system prompt replacement

4. **Configure permissions**: Use the `permission` field to control which tools each agent can access

5. **Set agent mode**: Use `mode: primary`, `mode: subagent`, or `mode: all` based on intended usage

## Files Created

- `/content/research/research-conclusion.md` - This research conclusion document