# claude-code-token-audit

Per-turn token usage logging for [Claude Code](https://claude.ai/code). After every conversation turn, a Stop hook writes one line to a session log:

```
[2026-04-22 10:41:16] [ Main ] Why can't scripts wait until conversation turn ends - prompt: 35035, response: 196, inter_in: 107377, inter_out: 196.
[2026-04-22 10:22:54] [ Agent - general-purpose: Current president of Ukraine ] Current president of Ukraine - prompt: 7542, response: 26, inter_in: 0, inter_out: 0.
```

Sub-agent usage is attributed to the parent session's log so you see the full cost of a session in one place.

## What gets logged

| Field | Meaning |
|-------|---------|
| `prompt` | Input tokens of the first API call — the size of your context window at turn start |
| `response` | Output tokens of the last API call — the actual answer |
| `inter_in` | Input tokens across all middle API calls — context reloaded once per tool call; this is the expensive part |
| `inter_out` | Output tokens across all middle API calls — tool call generation cost; usually small |

A turn with no tool calls has `inter_in: 0, inter_out: 0`. A turn with N tool calls pays approximately N× the context size in `inter_in`.

## Files

```
skill/
  SKILL.md              Claude Code skill — invoke with /token-audit-setup
  scripts/
    token_audit.py      The hook script (also lives at ~/.claude/hooks/token_audit.py)
knowledge-base.md       Full documentation: architecture, transcript quirks, setup, troubleshooting
lessons-learned.md      Every bug found during development, with the user question that exposed it
```

## Quick setup

**Prerequisites:** Claude Code with a Max subscription, `uv` in PATH.

1. Copy the hook script:
   ```sh
   mkdir -p ~/.claude/hooks
   cp skill/scripts/token_audit.py ~/.claude/hooks/token_audit.py
   ```

2. Add Stop and SubagentStop hooks to `~/.claude/settings.json`:
   ```json
   "hooks": {
     "Stop": [{
       "hooks": [{
         "type": "command",
         "command": "uv run --quiet python3 ~/.claude/hooks/token_audit.py",
         "timeout": 30,
         "statusMessage": "Logging token usage..."
       }]
     }],
     "SubagentStop": [{
       "hooks": [{
         "type": "command",
         "command": "uv run --quiet python3 ~/.claude/hooks/token_audit.py",
         "timeout": 30,
         "statusMessage": "Logging agent token usage...",
         "async": true
       }]
     }]
   }
   ```
   Stop is **synchronous** (no `async: true`) — the log entry is guaranteed to exist before you can type the next message.

3. Optionally add the log path to your status bar — see `knowledge-base.md` for the statusline script.

4. Or use the skill: copy `skill/` to `~/.claude/skills/token-audit-setup/` and run `/token-audit-setup` in any Claude Code session.

## Log location

```
~/.claude/logs/<session_id>/token_audit.log
```

One directory per session. The statusline integration (see KB) shows the full path in the Claude Code status bar so you can always find it without knowing the session ID.

## Why inter_in dominates

Claude Code is stateless — the full conversation history is sent with every API call. Each tool call adds one more round-trip that reloads the entire context. In a long session with heavy tool use:

- A turn with 5 tool calls pays for 5× the context size in `inter_in`
- Starting a fresh session for unrelated work avoids paying the accumulated history tax

See `knowledge-base.md` for a detailed breakdown.

## Non-obvious implementation details

See `lessons-learned.md` for the full history. Short version:

- Usage is at `entry.message.usage`, not `entry.usage` — there's a Claude Code wrapper layer
- `claude -p` runs in agentic mode by default; `--tools ""` is required to force pure text completion
- The Stop hook anchors on the **last assistant entry** when scanning the transcript, not the last user message — otherwise a fast follow-up message causes the turn to go unlogged
- Skill invocations inject the full skill markdown as a user message; the hook skips these to find the real human prompt
