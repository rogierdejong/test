#!/usr/bin/env python3
"""Parse Claude Code stream-json and print human-readable output."""

import json
import sys
from datetime import datetime

BLUE = "\033[34m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def shorten(text, max_len=200):
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    return text[:max_len] + "..." if len(text) > max_len else text


def format_tool_args(tool_name, tool_input):
    """Extract the most useful info from tool input."""
    if not tool_input:
        return ""
    # acquire_cookies
    if "acquire_cookies" in tool_name:
        model = tool_input.get("model", "my")
        cond = tool_input.get("condition", "used")
        return f"{model} {cond}"
    # search_top_n / search_inventory
    if "search" in tool_name:
        parts = []
        model = tool_input.get("model", "my")
        cond = tool_input.get("condition", "used")
        parts.append(f"{model} {cond}")
        if tool_input.get("year_min"):
            parts.append(f"year>={tool_input['year_min']}")
        if tool_input.get("odometer_max"):
            parts.append(f"odo<={tool_input['odometer_max']}")
        if tool_input.get("top_n"):
            parts.append(f"top {tool_input['top_n']}")
        return " | ".join(parts)
    # merge_results
    if "merge" in tool_name:
        n = len(tool_input.get("raw_files", []))
        fname = tool_input.get("filename", "")
        return f"{n} files -> {fname}"
    # save_to_postgres
    if "postgres" in tool_name:
        n = len(tool_input.get("raw_files", []))
        cond = tool_input.get("condition", "used")
        return f"{n} files -> tesla_{cond}"
    # Bash
    if "command" in tool_input:
        return shorten(tool_input["command"], 100)
    return shorten(json.dumps(tool_input, ensure_ascii=False), 150)


def timestamp():
    return datetime.now().strftime("%H:%M:%S")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        etype = event.get("type", "")

        # System init
        if etype == "system" and event.get("subtype") == "init":
            model = event.get("model", "?")
            print(f"\n{BOLD}{'='*60}")
            print(f"  Tesla Inventory Scraper Started")
            print(f"  Model: {model}  |  {timestamp()}")
            print(f"{'='*60}{RESET}\n")

        # Assistant text
        elif etype == "assistant":
            msg = event.get("message", {})
            contents = msg.get("content", [])
            for block in contents:
                if block.get("type") == "text" and block.get("text", "").strip():
                    text = block["text"].strip()
                    print(f"{DIM}[{timestamp()}]{RESET} {BLUE}{text}{RESET}")
                elif block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    tool_input = block.get("input", {})
                    short_name = name.replace("mcp__tesla-inventory__", "")
                    args_str = format_tool_args(name, tool_input)
                    print(f"{DIM}[{timestamp()}]{RESET} {YELLOW}>> {short_name}{RESET}", end="")
                    if args_str:
                        print(f"  {DIM}{args_str}{RESET}")
                    else:
                        print()

        # Tool result
        elif etype == "tool_result":
            content = event.get("content", "")
            tool_name = event.get("tool_name", "")
            is_error = event.get("is_error", False)

            if is_error:
                print(f"{DIM}[{timestamp()}]{RESET} {RED}!! Error: {shorten(str(content), 150)}{RESET}")
            elif tool_name and "acquire_cookies" in tool_name:
                print(f"{DIM}[{timestamp()}]{RESET} {GREEN}<< {shorten(str(content), 120)}{RESET}")
            elif tool_name and "search" in tool_name:
                try:
                    data = json.loads(content) if isinstance(content, str) else content
                    total = data.get("total", "?")
                    returned = data.get("returned", data.get("count", "?"))
                    print(f"{DIM}[{timestamp()}]{RESET} {GREEN}<< {returned} vehicles (total: {total}){RESET}")
                except (json.JSONDecodeError, TypeError):
                    print(f"{DIM}[{timestamp()}]{RESET} {GREEN}<< {shorten(str(content), 120)}{RESET}")
            elif tool_name and ("merge" in tool_name or "postgres" in tool_name):
                print(f"{DIM}[{timestamp()}]{RESET} {GREEN}<< {shorten(str(content), 120)}{RESET}")
            elif tool_name and "Bash" in tool_name:
                print(f"{DIM}[{timestamp()}]{RESET} {GREEN}<< done{RESET}")

        # Final result
        elif etype == "result":
            text = event.get("result", "")
            cost = event.get("cost_usd", 0)
            duration = event.get("duration_ms", 0)
            session_id = event.get("session_id", "")
            print(f"\n{BOLD}{'='*60}")
            print(f"  Run Complete")
            if cost:
                print(f"  Cost: ${cost:.4f}  |  Duration: {duration/1000:.1f}s")
            if session_id:
                print(f"  Session: {session_id}")
            print(f"{'='*60}{RESET}")
            if text:
                print(f"\n{text}\n")

    sys.stdout.flush()


if __name__ == "__main__":
    main()
