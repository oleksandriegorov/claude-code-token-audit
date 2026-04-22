#!/usr/bin/env python3
"""Token audit hook — logs per-turn token usage to ~/.claude/logs/<session_id>/token_audit.log

Handles two hook events:
  Stop         — fires at end of every session turn; logs as [ Main ]
  SubagentStop — fires in parent session when a sub-agent finishes; logs as [ Agent - <type>: <desc> ]
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def read_transcript(transcript_path: str) -> list[dict]:
    entries = []
    path = Path(transcript_path).expanduser()
    if not path.exists():
        return entries
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
    return ""


def _is_skill_injection(text: str) -> bool:
    """Skill content is injected as a user message starting with a markdown heading."""
    return text.startswith("# ") and len(text) > 500


def find_last_turn(entries: list[dict]) -> tuple[str, list[dict]]:
    """Return (human_prompt_text, [assistant_entries]) for the most recent completed turn.

    Anchors on the LAST assistant entry in the transcript, then walks backward
    to find the human message that started that turn. This is race-condition-safe:
    if the user submits a new message before the async hook reads the transcript,
    that new message has no assistant entry yet and is invisible to this logic.

    Skill injections (long messages starting with '# ') are skipped — when a
    skill is invoked, its full markdown content is appended as a second user
    message after the real human prompt.
    """
    # Anchor: find the last assistant entry = end of the completed turn
    last_assistant_idx = -1
    for i in range(len(entries) - 1, -1, -1):
        if entries[i].get("type") == "assistant":
            last_assistant_idx = i
            break

    if last_assistant_idx == -1:
        return "", []

    # Walk backward before that assistant entry to find the human prompt
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
        # Found the human message — collect all assistant entries in this turn
        turn_entries = entries[i:last_assistant_idx + 1]
        assistant_entries = [e for e in turn_entries if e.get("type") == "assistant"]
        return text, assistant_entries

    return "", []


def calc_tokens(assistant_entries: list[dict]) -> tuple[int, int, int, int]:
    """Return (prompt_tokens, response_tokens, intermediate_in_tokens, intermediate_out_tokens).

    prompt           = input_tokens of the first API call  (initial context cost)
    response         = output_tokens of the last API call  (final visible answer)
    intermediate_in  = all input tokens except the first   (context reloads per tool call)
    intermediate_out = all output tokens except the last   (tool call generation cost)
    """
    usages: list[tuple[int, int]] = []
    for entry in assistant_entries:
        u = entry.get("message", {}).get("usage", {}) or entry.get("usage", {})
        if not u:
            continue
        inp = (u.get("input_tokens", 0)
               + u.get("cache_read_input_tokens", 0)
               + u.get("cache_creation_input_tokens", 0))
        out = u.get("output_tokens", 0)
        usages.append((inp, out))

    if not usages:
        return 0, 0, 0, 0
    if len(usages) == 1:
        return usages[0][0], usages[0][1], 0, 0

    prompt_tokens = usages[0][0]
    response_tokens = usages[-1][1]
    intermediate_in = sum(inp for inp in [u[0] for u in usages[1:]])
    intermediate_out = sum(out for out in [u[1] for u in usages[:-1]])
    return prompt_tokens, response_tokens, intermediate_in, intermediate_out


def summarize(text: str) -> str:
    """Summarize via `claude -p` routed through Max subscription.

    Uses --tools "" to force pure LLM completion — without this flag, claude -p
    runs in agentic mode and may try to *execute* the prompt text as a task
    (e.g. "configure my status line") instead of summarizing it, causing a
    permission-prompt stall → 20 s timeout → silent truncation fallback.
    --no-session-persistence avoids creating a session file for this throwaway call.
    """
    if not text:
        return "(empty prompt)"
    try:
        result = subprocess.run(
            [
                "claude", "--model", "haiku",
                "--tools", "",
                "--no-session-persistence",
                "-p", f"Your only output is a 5-8 word summary of the following user prompt, no trailing punctuation:\n\n{text[:800]}",
            ],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().rstrip(".")
    except Exception:
        pass
    words = text.split()
    return " ".join(words[:8]) + ("..." if len(words) > 8 else "")


def write_log(log_dir: Path, label: str, summary: str,
              prompt_tok: int, response_tok: int,
              intermediate_in_tok: int, intermediate_out_tok: int) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = (
        f"[{timestamp}] {label} {summary} - "
        f"prompt: {prompt_tok}, response: {response_tok}, "
        f"intermediate_prompt: {intermediate_in_tok}, intermediate_response: {intermediate_out_tok}.\n"
    )
    with open(log_dir / "token_audit.log", "a") as f:
        f.write(line)


def handle_stop(data: dict) -> None:
    """Main session turn ended."""
    session_id = data.get("session_id", "unknown")
    transcript_path = data.get("transcript_path", "")
    if not transcript_path:
        return

    entries = read_transcript(transcript_path)
    prompt_text, assistant_entries = find_last_turn(entries)
    if not assistant_entries:
        return

    prompt_tok, response_tok, inter_in_tok, inter_out_tok = calc_tokens(assistant_entries)
    summary = summarize(prompt_text)
    log_dir = Path(f"~/.claude/logs/{session_id}").expanduser()
    write_log(log_dir, "[ Main ]", summary, prompt_tok, response_tok, inter_in_tok, inter_out_tok)


def handle_subagent_stop(data: dict) -> None:
    """A sub-agent finished — log to the PARENT session's log file."""
    parent_session_id = data.get("session_id", "unknown")
    agent_transcript_path = data.get("agent_transcript_path", "")
    if not agent_transcript_path:
        return

    # Read agent description and type from the .meta.json file alongside the transcript
    meta_path = Path(agent_transcript_path.replace(".jsonl", ".meta.json"))
    agent_type = data.get("agent_type", "") or "agent"
    description = ""
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            agent_type = meta.get("agentType", agent_type) or agent_type
            description = meta.get("description", "")
        except Exception:
            pass

    label = f"[ Agent - {agent_type}" + (f": {description}" if description else "") + " ]"

    entries = read_transcript(agent_transcript_path)
    _, assistant_entries = find_last_turn(entries)

    # For sub-agents the whole transcript IS the "turn" — sum all assistant entries
    if not assistant_entries:
        # Fall back: use ALL assistant entries in the transcript
        assistant_entries = [e for e in entries if e.get("type") == "assistant"]
    if not assistant_entries:
        return

    prompt_tok, response_tok, inter_in_tok, inter_out_tok = calc_tokens(assistant_entries)

    # Summarize the sub-agent's task from the first user message in its transcript
    prompt_text = ""
    for entry in entries:
        if entry.get("type") != "user":
            continue
        content = entry.get("message", {}).get("content", [])
        if isinstance(content, str):
            prompt_text = content
            break
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    prompt_text = block.get("text", "")
                    break
            if prompt_text:
                break

    summary = description if description else summarize(prompt_text)

    log_dir = Path(f"~/.claude/logs/{parent_session_id}").expanduser()
    write_log(log_dir, label, summary, prompt_tok, response_tok, inter_in_tok, inter_out_tok)


def main() -> None:
    try:
        data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        data = {}

    event = data.get("hook_event_name", "Stop")
    if event == "SubagentStop":
        handle_subagent_stop(data)
    else:
        handle_stop(data)


if __name__ == "__main__":
    main()
