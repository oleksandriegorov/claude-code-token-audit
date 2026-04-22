#!/usr/bin/env python3
"""Count token usage from a token_audit.log file.

Usage:
  count_tokens.py                                    # most recent log, all entries
  count_tokens.py today                              # most recent log, today's entries
  count_tokens.py yesterday                          # most recent log, yesterday's entries
  count_tokens.py 2026-04-22                         # most recent log, specific date
  count_tokens.py <log_file_or_session_id>           # specific log, all entries
  count_tokens.py <log_file_or_session_id> today     # specific log, filtered by date
  count_tokens.py <log_file_or_session_id> 2026-04-22
"""

import re
import sys
from datetime import date, timedelta
from pathlib import Path


def find_log(hint=None):
    logs_dir = Path("~/.claude/logs").expanduser()
    if hint:
        p = Path(hint).expanduser()
        if p.exists():
            return p
        p = logs_dir / hint / "token_audit.log"
        if p.exists():
            return p
    candidates = sorted(
        logs_dir.glob("*/token_audit.log"),
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def resolve_date(s):
    """Parse 'today', 'yesterday', or 'YYYY-MM-DD' into a date string prefix."""
    s = s.strip().lower()
    if s == "today":
        return str(date.today())
    if s == "yesterday":
        return str(date.today() - timedelta(days=1))
    if re.match(r'\d{4}-\d{2}-\d{2}', s):
        return s[:10]
    return None


def parse_log(path, date_filter=None):
    entries = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        if date_filter and date_filter not in line:
            continue
        p = re.search(r'prompt: (\d+)', line)
        if not p:
            continue
        r  = re.search(r'response: (\d+)', line)
        ii = re.search(r'intermediate_prompt: (\d+)|inter_in: (\d+)', line)
        io = re.search(r'intermediate_response: (\d+)|inter_out: (\d+)', line)
        im = re.search(r'\bintermediate: (\d+)', line)
        entry = {
            'line': line.strip(),
            'prompt': int(p.group(1)),
            'response': int(r.group(1)) if r else 0,
        }
        if ii and io:
            entry['inter_in'] = int(ii.group(1) or ii.group(2))
            entry['inter_out'] = int(io.group(1) or io.group(2))
            entry['format'] = 'new'
        elif im:
            entry['intermediate'] = int(im.group(1))
            entry['format'] = 'old'
        else:
            entry['inter_in'] = 0
            entry['inter_out'] = 0
            entry['format'] = 'new'
        entries.append(entry)
    return entries


def print_report(log_path, entries, date_filter=None):
    if not entries:
        label = f" on {date_filter}" if date_filter else ""
        print(f"No entries found{label}.")
        return

    prompt_total       = sum(e['prompt']             for e in entries)
    response_total     = sum(e['response']            for e in entries)
    inter_in_total     = sum(e.get('inter_in', 0)     for e in entries)
    inter_out_total    = sum(e.get('inter_out', 0)    for e in entries)
    intermediate_total = sum(e.get('intermediate', 0) for e in entries)
    old_count  = sum(1 for e in entries if e['format'] == 'old')
    new_count  = sum(1 for e in entries if e['format'] == 'new')
    grand_total = prompt_total + response_total + inter_in_total + inter_out_total + intermediate_total

    date_label = f"  [{date_filter}]" if date_filter else ""
    print(f"Log:   {log_path}{date_label}")
    print(f"Turns: {len(entries)}  ({old_count} old format, {new_count} new format)")
    print()
    print(f"{'Field':<22} {'Tokens':>12}")
    print("-" * 36)
    print(f"{'prompt (input)':<22} {prompt_total:>12,}")
    print(f"{'response (output)':<22} {response_total:>12,}")
    print(f"{'intermediate_prompt':<22} {inter_in_total:>12,}")
    print(f"{'intermediate_response':<22} {inter_out_total:>12,}")
    if intermediate_total:
        print(f"{'intermediate (mixed)':<22} {intermediate_total:>12,}  ← old format, mostly input")
    print("-" * 36)
    print(f"{'TOTAL':<22} {grand_total:>12,}")
    print()
    known_input  = prompt_total + inter_in_total
    known_output = response_total + inter_out_total
    print(f"Known input:   {known_input:>12,}")
    print(f"Known output:  {known_output:>12,}")
    if intermediate_total:
        print(f"Unsplit mixed: {intermediate_total:>12,}  (old-format entries — dominated by input)")


def main():
    args = sys.argv[1:]
    date_filter = None
    log_hint = None

    for arg in args:
        d = resolve_date(arg)
        if d:
            date_filter = d
        else:
            log_hint = arg

    log_path = find_log(log_hint)
    if not log_path or not log_path.exists():
        print("No token_audit.log found.")
        sys.exit(1)

    entries = parse_log(log_path, date_filter)
    print_report(log_path, entries, date_filter)


if __name__ == "__main__":
    main()
