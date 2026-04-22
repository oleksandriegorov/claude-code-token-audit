# Token Audit — Lessons Learned

Bugs encountered while building `~/.claude/hooks/token_audit.py`, in the order they appeared. Each entry includes the user's question that exposed it, the root cause, and the fix applied.

---

## Bug 1 — Wrong usage key path: `entry.usage` vs `entry.message.usage`

**User question that exposed it:**

> "How did you produce an entry into the file... The agent is still running?! more so it shown 21K token when I looked at it"

The token count shown (21K) was implausibly low for a turn with multiple tool calls.

**Root cause:**

The JSONL transcript stores each assistant API response as a Claude Code internal wrapper object, not as a raw API response. The Anthropic usage object is nested one level deeper than expected:

```jsonc
// Expected (wrong assumption):
{ "type": "assistant", "usage": { "input_tokens": 1234, ... } }

// Actual transcript format:
{ "type": "assistant", "message": { "usage": { "input_tokens": 1234, ... }, ... } }
```

The original code used `entry.get("usage", {})` which always returned an empty dict, causing all token counts to come back as zero (or from a single accidentally-correct entry).

**Fix:**

```python
u = entry.get("message", {}).get("usage", {}) or entry.get("usage", {})
```

Fallback to the flat key is kept in case the transcript format changes in a future Claude Code version.

---

## Bug 2 — `claude -p` agentic mode: prompt executed instead of summarized

**User question that exposed it:**

> "Reviewed token_audit.log — it seems to me that summary of an input prompt is not a summary per se... Doublecheck that you do summary"

Log entries showed the first 8 words of the prompt verbatim rather than a proper summary, indicating the Haiku subprocess was timing out silently.

**Root cause:**

`claude -p` launches Claude Code in **full agentic mode** by default. When the prompt text passed to the summarizer looked like an actionable task (e.g., "make my status line reflect token usage"), Claude Code tried to **execute** it rather than returning a text summary. This caused:

1. Permission prompts appearing inside the Stop hook subprocess
2. The subprocess stalling at the prompt, waiting for user interaction that would never come
3. A 20 s timeout → non-zero exit code → silent fallback to first-8-words truncation

The symptom in the log was summaries reading `make my status line reflect token...` — the literal start of the prompt.

**Fix:**

Two flags added to the `claude` invocation:

- `--tools ""` — passes an empty tools list, disabling all tool access and forcing pure LLM text completion mode
- `--no-session-persistence` — prevents a throwaway session file from being created in `~/.claude/sessions/`

```python
result = subprocess.run(
    ["claude", "--model", "haiku", "--tools", "", "--no-session-persistence",
     "-p", f"Your only output is a 5-8 word summary..."],
    ...
)
```

Without **both** flags, the agentic-mode stall can still occur on task-like prompts.

---

## Bug 3 — `last-prompt` staleness: wrong turn's prompt summarized

**User question that exposed it:**

> "I do not think those work currently enough: [log entries shown]... I did not ask for Lviv and Krakow weather alongside current president"

A log entry for "who is the current president of Ukraine" showed the summary from the previous turn ("Compare Lviv and Krakow weather").

**Root cause:**

The original implementation relied on the transcript's `last-prompt` entry type to retrieve the human's actual typed text:

```jsonc
{ "type": "last-prompt", "lastPrompt": "who is the president of Ukraine" }
```

This entry is written **asynchronously** by Claude Code and lags one turn behind. By the time the hook reads the transcript, `last-prompt` holds the text from the turn *before* the current one.

**Fix:**

Removed all reliance on `last-prompt`. The hook now parses the actual `user` message entries in the transcript directly, walking backward from the last assistant entry to find the human prompt text.

---

## Bug 4 — Skill injection contamination: skill markdown summarized instead of user prompt

**Discovery:**

Emerged as part of the `last-prompt` investigation. When a skill is invoked (e.g., `/token-audit-setup`), Claude Code appends the skill's full markdown content as a second `user` message immediately after the human's typed prompt. This second message:
- Starts with `# ` (a markdown heading)
- Is several hundred to several thousand characters long

A "find last user message" approach finds the skill injection first and summarizes it instead of the actual question.

**Fix:**

```python
def _is_skill_injection(text: str) -> bool:
    return text.startswith("# ") and len(text) > 500
```

The backward walk in `find_last_turn` skips any user message matching this heuristic.

---

## Bug 5 — Async race condition: turns not logged when user types quickly

**User question that exposed it:**

> "Now, this question and query did not get logged into token_audit file :-) Is there a bug, which causes to ignore single questions if no agents/subagents are involved?"

Simple direct questions (no tool calls, no subagents) were missing from the log.

**Root cause:**

The Stop hook is async — `uv run python3` has ~1–2 s startup latency. For short responses (a single factual answer), the user can submit the next message before the hook reads the transcript. The transcript then looks like:

```
... previous turns ...
[user]      "who is the president of Ukraine?"   ← completed turn's human prompt
[assistant] "Volodymyr Zelensky."                ← completed turn's answer
[user]      "who is the president of Poland?"    ← NEW message, no assistant entry yet
```

The original `find_last_turn` logic: "find the last user message, then collect assistant entries after it." With the new message present, it found "who is the president of Poland?" and saw no assistant entry after it → returned empty → no log written.

**Fix:**

Rewrite `find_last_turn` to anchor on the **last assistant entry** (which always belongs to the completed turn and is immune to newer user messages), then walk **backward** to find the human prompt:

```python
# Anchor: find the last assistant entry = end of the completed turn
last_assistant_idx = -1
for i in range(len(entries) - 1, -1, -1):
    if entries[i].get("type") == "assistant":
        last_assistant_idx = i
        break

# Walk backward to find the human prompt
for i in range(last_assistant_idx - 1, -1, -1):
    entry = entries[i]
    ...
```

This makes the function immune to any number of subsequent user messages in the transcript.

---

## Bug 6 — Async hook: log entry appears only after the next message is sent

**User question that exposed it:**

> "I do not get it — why can the script not wait until conversation turn ends and fire? This is inconvenient to make sure I send the next message to see the previous one"

Even after the race-condition fix, log entries for a given turn only appeared in the log after the next user message was submitted, because the async hook startup delay meant the hook hadn't run yet.

**Root cause:**

The Stop hook had `"async": true` in `settings.json`. This flag causes Claude Code to fire the hook in the background without waiting for it to finish. The UI becomes available for the next message immediately, while the hook (and its Haiku summarization call) runs in parallel. The log entry appears seconds later — but only visible to the user once they look at the file, which usually coincides with the next turn.

**Fix:**

Remove `"async": true` from the Stop hook. Claude Code now waits for the hook to finish (including the Haiku summarization call, ~2–5 s) before accepting the next input. The log entry is guaranteed to exist by the time the prompt is available again.

SubagentStop keeps `"async": true` — sub-agent log attribution does not need to block the UI.

```json
"Stop": [{
  "hooks": [{
    "type": "command",
    "command": "uv run --quiet python3 ~/.claude/hooks/token_audit.py",
    "timeout": 30,
    "statusMessage": "Logging token usage..."
  }]
}]
```

---

## Key Lessons

1. **Claude Code transcript internals are non-obvious.** Don't assume the transcript format matches the Anthropic API schema directly — there is a Claude Code wrapper layer.

2. **`claude -p` is agentic by default.** Any use of `claude -p` for pure text generation must include `--tools ""` or it may try to execute the prompt as a task.

3. **Async transcript entries lag.** `last-prompt` and similar async-written entries should not be relied upon for the current turn — they reflect the previous turn.

4. **Async hooks are not "fire after turn ends" — they fire and return immediately.** The hook subprocess runs concurrently with the next user interaction. For correctness guarantees, use synchronous hooks (no `async: true`).

5. **Anchor on output, not input, for race-condition safety.** When finding "what just happened" in a live transcript, anchor on the last output (assistant entry) rather than the last input (user entry). Output entries are immutable once written; new input entries can appear at any time.

---

## Enhancement — Split intermediate into `inter_in` / `inter_out`

**User question that prompted it:**

> "Is there a way to get intermediate_input and intermediate_output separately?"

**Motivation:**

The original `intermediate` field combined input and output tokens from all middle API calls into one number. This obscured the nature of the cost: `inter_in` (context reloads) and `inter_out` (tool call generation) have very different magnitudes and meanings.

**Change:**

`calc_tokens` now returns four values instead of three:

```python
intermediate_in  = sum(inp for inp, out in usages[1:])   # all inputs except prompt
intermediate_out = sum(out for inp, out in usages[:-1])  # all outputs except response
```

Log line format changed from:
```
prompt: N, response: N, intermediate: N.
```
to:
```
prompt: N, response: N, inter_in: N, inter_out: N.
```

**Interpretation:**
- `inter_in` is the expensive part — each tool call reloads the full conversation context. N tool calls → N× context size in inter_in.
- `inter_out` is cheap — just the tokens spent generating tool call syntax and intermediate reasoning steps.
