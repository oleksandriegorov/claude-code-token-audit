# Token Audit — Knowledge Base Article

## What It Does and Why

The token audit feature gives you per-turn token visibility after every Claude Code conversation turn. After each response, a Stop hook fires and writes one log line to a session-specific file:

```
[2026-04-22 10:38:16] [ Main ] User asking about the current president of Poland - prompt: 34009, response: 27, inter_in: 0, inter_out: 0.
[2026-04-22 10:22:54] [ Agent - general-purpose: Current president of Ukraine ] Current president of Ukraine - prompt: 7542, response: 26, inter_in: 0, inter_out: 0.
```

**Why it matters:** Claude Code sessions can silently accumulate enormous token counts through tool use, subagent spawning, and context reloading. A single "simple" turn may appear cheap on the surface while its intermediate rounds cost 10–50× what the final response cost. Without per-turn accounting you have no signal for when a session is becoming expensive or when to start a fresh context.


## Architecture

### Files Involved

| File | Role |
|------|------|
| `~/.claude/hooks/token_audit.py` | Stop + SubagentStop hook script — reads transcript, computes tokens, writes log |
| `~/.claude/settings.json` | Wires the Stop and SubagentStop hooks; also carries the statusline command |
| `~/.claude/statusline-command.sh` | Statusline script — shows log path in the Claude Code status bar |
| `~/.claude/logs/<session_id>/token_audit.log` | Per-session append-only log (created at runtime) |

### Data Flow

```
Claude Code ends a turn
        |
        v
Stop hook fires (synchronous, timeout=30s)
        |
        v
uv run python3 ~/.claude/hooks/token_audit.py
        |
        | stdin: {"session_id": "...", "transcript_path": "...", "hook_event_name": "Stop"}
        v
token_audit.py
  1. Reads JSONL transcript line by line
  2. Anchors on LAST assistant entry (immune to next user message in transcript)
  3. Walks backward to find the human prompt that started that turn
  4. Skips pure tool_result messages and skill injections (long "# …" blocks)
  5. Parses entry.message.usage for each assistant entry in the turn
  6. Computes prompt / response / intermediate token counts
  7. Calls: claude --model haiku --tools "" --no-session-persistence -p "Summarize..."
  8. Appends one line to ~/.claude/logs/<session_id>/token_audit.log

Claude Code ends a sub-agent turn
        |
        v
SubagentStop hook fires (async, timeout=30s)
        |
        v
uv run python3 ~/.claude/hooks/token_audit.py
        |
        | stdin: {"session_id": "<parent_id>", "agent_transcript_path": "...", "hook_event_name": "SubagentStop"}
        v
token_audit.py
  1. Reads agent_transcript_path + alongside .meta.json for agentType + description
  2. Sums ALL assistant entries in the agent transcript
  3. Uses description as summary label
  4. Appends to PARENT session's log file: ~/.claude/logs/<parent_session_id>/token_audit.log
```

### Hook Wiring in settings.json

```json
"hooks": {
  "Stop": [
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
  ],
  "SubagentStop": [
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
  ]
}
```

**Stop hook is synchronous** (no `async: true`) — Claude Code waits for the hook to finish before accepting the next user input, guaranteeing the log entry is written before the prompt is available again. The ~2–5 s Haiku summarization latency is acceptable here.

**SubagentStop hook is async** — sub-agent logs are attribution metadata and do not need to block the UI.


## Transcript Format Quirks

The JSONL transcript has a non-obvious structure that required several workarounds.

### 1. Usage is at `entry.message.usage`, not `entry.usage`

Each assistant entry in the transcript is a Claude Code internal record wrapping the raw API response. The Anthropic API usage object lives one level deeper:

```jsonc
// What you might expect:
{ "type": "assistant", "usage": { "input_tokens": 1234, ... } }

// What is actually in the transcript:
{ "type": "assistant", "message": { "usage": { "input_tokens": 1234, ... }, ... } }
```

The script handles this with a fallback chain:
```python
u = entry.get("message", {}).get("usage", {}) or entry.get("usage", {})
```

### 2. Race condition: new user message arrives before async hook reads transcript

The Stop hook fires asynchronously after a turn ends. For short responses, `uv run python3` startup takes ~1–2 s — enough time for the user to submit the next message. A naive "find the last user message, then collect assistant entries after it" approach would find the NEW message (which has no assistant entry yet) and bail without logging.

**Fix:** anchor on the **last assistant entry** (which always exists from the just-completed turn and is immune to newer user messages), then walk backward to find the human prompt that started that turn.

```python
def find_last_turn(entries):
    # Find the last assistant entry — always from the completed turn
    last_assistant_idx = -1
    for i in range(len(entries) - 1, -1, -1):
        if entries[i].get("type") == "assistant":
            last_assistant_idx = i
            break
    if last_assistant_idx == -1:
        return "", []

    # Walk backward to find the human prompt
    for i in range(last_assistant_idx - 1, -1, -1):
        entry = entries[i]
        if entry.get("type") != "user":
            continue
        content = entry.get("message", {}).get("content", [])
        # Skip pure tool_result messages (intermediate tool round-trips)
        if isinstance(content, list) and content and all(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        ):
            continue
        text = _extract_text(content)
        if _is_skill_injection(text):
            continue
        turn_entries = entries[i:last_assistant_idx + 1]
        assistant_entries = [e for e in turn_entries if e.get("type") == "assistant"]
        return text, assistant_entries
    return "", []
```

### 3. Skill injection contamination

When a Claude Code skill is invoked, the skill's full markdown content is appended as a second user message immediately after the real human prompt. This second message starts with `# ` and is several hundred to several thousand characters long.

A naive "find last user message" approach would find the skill injection and summarize the skill documentation rather than the user's actual question.

**Fix:** `_is_skill_injection(text)` returns True for messages that start with `# ` and are longer than 500 characters. The backward walk skips these.


## Sub-Agent Attribution

When a sub-agent finishes, the SubagentStop hook fires in the **parent session** with:

```json
{
  "hook_event_name": "SubagentStop",
  "session_id": "<parent_session_id>",
  "agent_transcript_path": "/path/to/agent-<id>.jsonl",
  "agent_type": "general-purpose"
}
```

A `.meta.json` file alongside the transcript contains:
```json
{ "agentType": "general-purpose", "description": "Current president of Ukraine" }
```

The log line uses the description as both the label and the summary:
```
[2026-04-22 10:22:54] [ Agent - general-purpose: Current president of Ukraine ] Current president of Ukraine - prompt: 7542, response: 26, intermediate: 0.
```

All sub-agent entries go to the **parent session's** log file, so the full picture of a session's token usage (main turns + all spawned agents) is in one place.


## Token Accounting Model

Each conversation turn triggers potentially many API calls (tool use rounds, subagent calls, context reloads). The script collects `(input_tokens, output_tokens)` pairs from every assistant entry in the turn, then partitions them:

```
usages = [(inp_0, out_0), (inp_1, out_1), ..., (inp_n, out_n)]

prompt        = inp_0                        # initial context load
response      = out_n                        # tokens in the final human-visible answer
inter_in      = inp_1 + inp_2 + ... + inp_n  # context reloads per tool call
inter_out     = out_0 + out_1 + ... + out_{n-1}  # tool call generation cost
```

**Interpretation:**
- `prompt` — baseline cost of starting this turn (context window size signal); grows slowly as conversation history accumulates
- `response` — actual answer generation cost (usually small)
- `inter_in` — the dominant hidden cost; each tool call reloads the full context: N tool calls → N× context size
- `inter_out` — cheap by comparison; just the tokens spent generating tool call syntax and intermediate reasoning

Cache tokens (`cache_read_input_tokens`, `cache_creation_input_tokens`) are included in `inp` counts to give a faithful view of total context processed regardless of caching.


## Summarization Approach

```python
result = subprocess.run(
    [
        "claude", "--model", "haiku",
        "--tools", "",                    # disables ALL tools → pure LLM completion
        "--no-session-persistence",       # no session file created for throwaway call
        "-p", f"Your only output is a 5-8 word summary of the following user prompt, no trailing punctuation:\n\n{text[:800]}",
    ],
    capture_output=True, text=True, timeout=20,
)
```

**Why `claude -p` instead of the Anthropic SDK:**

Using the SDK directly would require an explicit `ANTHROPIC_API_KEY` and would bill against a separate API account. Calling `claude --model haiku -p` routes through the Claude Code process, which uses the user's Max subscription. There is no additional cost or credential management.

### The agentic-mode bug (and why `--tools ""` and `--no-session-persistence` are required)

`claude -p` runs Claude Code in **full agentic mode** by default. When the prompt text passed for summarization looked like a task — for example, "make my status line reflect token usage" — Claude Code interpreted it as an instruction to execute rather than a string to summarize. This triggered:

1. Permission prompts appearing during the Stop hook run
2. A subprocess timeout (20 s) because the agentic execution stalled waiting for user interaction
3. Silent fallback to word truncation — the symptom visible in logs was summary lines reading `make my status line reflect token...`

Two flags together prevent this:

- `--tools ""` — passes an empty tools list, disabling ALL tool access. Claude receives the prompt with no ability to call any tool, forcing pure LLM text completion.
- `--no-session-persistence` — prevents Claude Code from creating a session file for this throwaway call, avoiding session state pollution in `~/.claude/sessions/`.

**Fallback:** If the `claude` subprocess fails (not in PATH, timeout, non-zero exit), the script falls back to the first 8 words of the prompt text with `...` appended. The log line is still written; only the summary quality degrades.

**Model choice:** `haiku` is used because summarization is a trivial task. Using `sonnet` or `opus` here would be wasteful.


## Statusline Integration

`~/.claude/statusline-command.sh` is called by Claude Code on every statusline refresh. It reads a JSON blob from stdin containing `session_id`, `model`, context window usage, and rate limit usage.

The token audit integration adds the log path to the status bar:

```sh
session_id=$(echo "$input" | jq -r '.session_id // empty')
if [ -n "$session_id" ]; then
  log_path="$HOME/.claude/logs/${session_id}/token_audit.log"
else
  log_path=$(find "$HOME/.claude/logs" -name "token_audit.log" -maxdepth 2 2>/dev/null \
    | xargs ls -t 2>/dev/null | head -1)
  [ -z "$log_path" ] && log_path="~/.claude/logs/<session>/token_audit.log"
fi

printf "%s | %s | %s | %s | %s" "$model" "$dir_name" "$ctx" "$session" "$log_path"
```

The status bar output looks like:
```
Claude Sonnet 4.6 | myproject | ctx:42% | session:18% | /Users/you/.claude/logs/abc123/token_audit.log
```

This lets you `tail -f` or `cat` the log file directly from the path shown in the status bar without hunting for the session ID.


## Manual Setup Steps

Follow these steps to set up the token audit feature in a fresh Claude Code installation.

**Prerequisites:** Claude Code installed and authenticated, `uv` available in PATH.

1. **Create the hooks and logs directories** (if they do not exist):
   ```sh
   mkdir -p ~/.claude/hooks
   mkdir -p ~/.claude/logs
   ```

2. **Write `~/.claude/hooks/token_audit.py`** with the contents from this repository.

3. **Merge the Stop and SubagentStop hooks into `~/.claude/settings.json`**. Add the following inside the `"hooks"` object. If a `"Stop"` key already exists, append to its array; do not replace it:
   ```json
   "Stop": [
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
   ],
   "SubagentStop": [
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
   ]
   ```

4. **Write `~/.claude/statusline-command.sh`** with the contents from this repository (or merge the `log_path` block into an existing statusline script).

5. **Register the statusline command** in `~/.claude/settings.json`:
   ```json
   "statusLine": {
     "type": "command",
     "command": "bash ~/.claude/statusline-command.sh"
   }
   ```

6. **Verify** by starting a new Claude Code session, sending any message, and waiting for "Logging token usage..." to appear in the status bar. Then:
   ```sh
   cat ~/.claude/logs/<session_id>/token_audit.log
   ```
   You should see one line per completed turn.


## Troubleshooting

### Log file is not written after a turn

1. Check that the hook is registered: open `~/.claude/settings.json` and confirm the `"Stop"` hook array contains the `token_audit.py` command.
2. Confirm `uv` is in the PATH that Claude Code uses: run `which uv` in a terminal. If it is only in a shell-specific PATH extension (e.g., `.zshrc`) but not in `/usr/local/bin` or similar, Claude Code's hook subprocess may not find it.
3. Check that the transcript path is being passed: temporarily add `import sys; open("/tmp/ta_debug.json","w").write(sys.stdin.read())` at the top of `main()`, trigger a turn, and inspect `/tmp/ta_debug.json`.
4. Look for Python errors: add `2>/tmp/ta_error.log` to the hook command to capture stderr.

### Summary is truncated (shows first 8 words + `...`)

This is the symptom of the agentic-mode bug. Verify that `summarize()` in `~/.claude/hooks/token_audit.py` includes both `--tools ""` and `--no-session-persistence` flags. If they are present but truncation still occurs, test the CLI call directly:

```sh
claude --model haiku --tools "" --no-session-persistence -p "Your only output is a 5-8 word summary of the following user prompt, no trailing punctuation: hello world"
```

### All token counts show 0

The script found assistant entries but could not read usage from them. Check whether `entry.message.usage` still exists in the current Claude Code version:

```sh
cat ~/.claude/sessions/<session_id>.jsonl | python3 -c "
import sys, json
for line in sys.stdin:
    e = json.loads(line.strip())
    if e.get('type') == 'assistant':
        print(json.dumps(e.get('message',{}).get('usage'), indent=2))
        break
"
```

### Statusline shows wrong or missing log path

If the status bar shows `~/.claude/logs/<session>/token_audit.log` literally, `session_id` was empty in the statusline input JSON. Inspect the raw input by temporarily changing the statusline command to `cat > /tmp/sl_debug.json`.
