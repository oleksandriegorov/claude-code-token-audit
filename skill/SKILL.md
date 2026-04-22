---
name: token-audit-setup
description: |
  Configure the per-turn token audit feature for Claude Code. Installs a Stop hook
  and SubagentStop hook that log prompt/response/intermediate token counts with a
  Haiku-generated summary to ~/.claude/logs/<session_id>/token_audit.log after every
  turn. Sub-agent usage is attributed to the parent session. Log path shown in status bar.
  Use when setting up a new Claude Code installation or re-installing after a reset.
  Also use when the user asks about token usage, how many tokens were used, current
  session token count, or wants a token usage report for the current or any session.
---

# Skill: token-audit-setup

Configure the token audit feature in this Claude Code installation. This feature fires a Stop hook after every conversation turn to log per-turn token usage (prompt / response / intermediate) and a short summary to a per-session log file. Sub-agent token usage is attributed to the parent session via a SubagentStop hook. The log path is shown in the status bar.

**This skill handles two distinct tasks — read the user's request and follow the matching section:**

- User asks to **set up / install / configure** token audit → follow the Setup steps below
- User asks about **token usage / how many tokens / session stats** → follow the Reporting section at the bottom

---

## Prerequisites Check

Before writing any files, verify:

1. Run `which uv` — confirm `uv` is available. If not, tell the user to install it (`brew install uv` on macOS or `curl -LsSf https://astral.sh/uv/install.sh | sh`) and stop.
2. Run `claude --version` — confirm the `claude` CLI is in PATH (needed by the hook for Haiku summarization). If not, warn the user; the hook will fall back to truncation-based summaries.
3. Read `~/.claude/settings.json` — load the current settings so the JSON merge is safe.

---

## Step 1 — Create required directories

```bash
mkdir -p ~/.claude/hooks
mkdir -p ~/.claude/logs
```

Report: "Directories confirmed."

---

## Step 2 — Write the hook script

Copy `scripts/token_audit.py` from this skill directory to `~/.claude/hooks/token_audit.py`.

If `~/.claude/hooks/token_audit.py` already exists, read it first and check whether it contains:
- `--tools ""` and `--no-session-persistence` flags in `summarize()` (agentic-mode fix)
- `last_assistant_idx` in `find_last_turn()` (race-condition fix)
- `handle_subagent_stop` function (SubagentStop support)

If all three are present, report "Hook script already present (current version)." and skip.
Otherwise, overwrite with the script from `scripts/token_audit.py` and report "Hook script updated."

```bash
cp ~/.claude/skills/token-audit-setup/scripts/token_audit.py ~/.claude/hooks/token_audit.py
```

---

## Step 3 — Merge Stop and SubagentStop hooks into settings.json

Read `~/.claude/settings.json`. Apply a **safe merge** — do not overwrite existing hooks or settings.

For **Stop**: if `"hooks"."Stop"` already exists and any entry's `"command"` contains `"token_audit.py"`, report "Stop hook already registered." and skip. Otherwise add:

```json
{
  "hooks": [
    {
      "type": "command",
      "command": "uv run --quiet python3 ~/.claude/hooks/token_audit.py",
      "timeout": 30,
      "statusMessage": "Logging token usage..."
    }
  ]
}
```

**No `"async": true` on Stop** — the hook must run synchronously so the log entry is written before the next user input is accepted.

For **SubagentStop**: same check, same pattern, but with `"async": true`:

```json
{
  "hooks": [
    {
      "type": "command",
      "command": "uv run --quiet python3 ~/.claude/hooks/token_audit.py",
      "timeout": 30,
      "statusMessage": "Logging agent token usage...",
      "async": true
    }
  ]
}
```

Write the updated JSON back to `~/.claude/settings.json` with 2-space indentation. Preserve all existing keys and values exactly.

Report: "Stop and SubagentStop hooks registered in settings.json."

---

## Step 4 — Write the statusline script

Write the following to `~/.claude/statusline-command.sh`. If the file already exists, read it first.

- If it already contains `token_audit.log` — report "Statusline already includes token audit path." and skip.
- If it exists but lacks `token_audit.log` — merge carefully: add the `log_path` block before the final `printf` and append `$log_path` to it. Show the diff before writing.
- If the file does not exist — write it in full.

```sh
#!/bin/sh
input=$(cat)

model=$(echo "$input" | jq -r '.model.display_name // "unknown"')
current_dir=$(echo "$input" | jq -r '.workspace.current_dir // .cwd // ""')
dir_name=$(basename "$current_dir")

used_pct=$(echo "$input" | jq -r '.context_window.used_percentage // empty')
if [ -n "$used_pct" ]; then
  ctx=$(printf "ctx:%.0f%%" "$used_pct")
else
  ctx="ctx:--"
fi

five_pct=$(echo "$input" | jq -r '.rate_limits.five_hour.used_percentage // empty')
if [ -n "$five_pct" ]; then
  session=$(printf "session:%.0f%%" "$five_pct")
else
  session="session:--"
fi

# Determine token_audit.log path for the current session
session_id=$(echo "$input" | jq -r '.session_id // empty')
if [ -n "$session_id" ]; then
  log_path="$HOME/.claude/logs/${session_id}/token_audit.log"
else
  # Fallback: find the most recently modified token_audit.log
  log_path=$(find "$HOME/.claude/logs" -name "token_audit.log" -maxdepth 2 2>/dev/null \
    | xargs ls -t 2>/dev/null | head -1)
  [ -z "$log_path" ] && log_path="~/.claude/logs/<session>/token_audit.log"
fi

printf "%s | %s | %s | %s | %s" "$model" "$dir_name" "$ctx" "$session" "$log_path"
```

After writing, make the file executable:
```bash
chmod +x ~/.claude/statusline-command.sh
```

---

## Step 5 — Register the statusline command in settings.json

Read the (now-updated) `~/.claude/settings.json`.

- If `"statusLine"` already has `"command": "bash ~/.claude/statusline-command.sh"` — report "Statusline already configured." and skip.
- If `"statusLine"` exists with a different command — warn the user and ask whether to replace it.
- Otherwise add:
  ```json
  "statusLine": {
    "type": "command",
    "command": "bash ~/.claude/statusline-command.sh"
  }
  ```

Write the updated JSON back with 2-space indentation.

Report: "Statusline command registered in settings.json."

---

## Step 6 — Verification

Run all checks and report the output of each:

1. `ls -lh ~/.claude/hooks/token_audit.py` — confirm script exists
2. `grep -c "token_audit" ~/.claude/settings.json` — expect ≥ 2 (Stop + SubagentStop)
3. `ls -lh ~/.claude/statusline-command.sh` — confirm script exists and is executable
4. `grep -c "last_assistant_idx" ~/.claude/hooks/token_audit.py` — expect ≥ 1 (race-condition fix)
5. `grep -c "no-session-persistence" ~/.claude/hooks/token_audit.py` — expect ≥ 1 (agentic-mode fix)
6. `uv run --quiet python3 -c "import ast, pathlib; ast.parse(pathlib.Path('~/.claude/hooks/token_audit.py').expanduser().read_text()); print('syntax OK')"` — confirm valid Python

---

## Step 7 — Final instructions to user

After all steps succeed, tell the user:

> Setup complete. To see the token audit in action:
>
> 1. Start a new Claude Code session (the hook reads from the current session's transcript).
> 2. Send any message and wait for the response to finish.
> 3. The status bar shows "Logging token usage..." briefly, then the path to the log file.
> 4. Run: `cat ~/.claude/logs/<session_id>/token_audit.log`
>
> Each line shows:
> - Main turns: `[timestamp] [ Main ] <summary> - prompt: N, response: N, inter_in: N, inter_out: N.`
> - Sub-agent turns: `[timestamp] [ Agent - <type>: <description> ] <summary> - prompt: N, response: N, inter_in: N, inter_out: N.`
>
> The Stop hook runs synchronously — the log entry is guaranteed to be written before you can type the next message.
>
> Reference: `~/.claude/docs/token-audit.md` for full documentation and troubleshooting.
> Reference: `~/.claude/docs/token-audit-lessons-learned.md` for bugs encountered during development.

---

## Reporting — Count session token usage

Use this section when the user asks how many tokens were used, wants a usage summary, or asks about token counts for the current or any session.

**Step 1 — Find the log file.**

The current session log path is shown in the status bar. If the user doesn't provide a path, run:

```bash
find ~/.claude/logs -name "token_audit.log" | xargs ls -t | head -5
```

Use the most recent one, or the one matching the session ID the user mentions.

**Step 2 — Run the counting script:**

```bash
# All entries in most recent log
uv run --quiet python3 ~/.claude/hooks/count_tokens.py

# Filter by date — accepts 'today', 'yesterday', or 'YYYY-MM-DD'
uv run --quiet python3 ~/.claude/hooks/count_tokens.py today
uv run --quiet python3 ~/.claude/hooks/count_tokens.py yesterday
uv run --quiet python3 ~/.claude/hooks/count_tokens.py 2026-04-22

# Specific log file or session ID, optionally filtered
uv run --quiet python3 ~/.claude/hooks/count_tokens.py <session_id_or_path>
uv run --quiet python3 ~/.claude/hooks/count_tokens.py <session_id_or_path> today
```

Arguments can be given in any order — the script distinguishes dates from log paths automatically.

**Step 3 — Present the results** in a clean table. Explain the fields:

- `prompt` — input tokens at turn start (context window size)
- `response` — output tokens for the final answer
- `inter_in` — input tokens from context reloads per tool call (the expensive part)
- `inter_out` — output tokens from tool call generation (usually small)
- `intermediate` — old-format combined value, appears in logs before the inter_in/inter_out split was introduced; dominated by input

Note: if the log contains old-format entries (with `intermediate` instead of `inter_in`/`inter_out`), the input/output split is only partial — mention this to the user.
