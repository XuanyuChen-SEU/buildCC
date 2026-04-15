#!/usr/bin/env python3
# 框架：持久化任务——能跨越单次对话生命周期的目标。
"""
s07_task_system.py - 任务（Tasks）

任务以 JSON 文件形式持久化在 `.tasks/` 中，因此可以在上下文压缩后继续保留。
每个任务都有依赖关系图（`blockedBy`）。

    .tasks/
      task_1.json  {"id":1, "subject":"...", "status":"completed", ...}
      task_2.json  {"id":2, "blockedBy":[1], "status":"pending", ...}
      task_3.json  {"id":3, "blockedBy":[2], ...}

    依赖关系解析：
    +----------+     +----------+     +----------+
    | task 1   | --> | task 2   | --> | task 3   |
    | 已完成    |     | 被阻塞    |     | 被阻塞    |
    +----------+     +----------+     +----------+
         |                ^
         +--- 完成 task 1 后，会把它从 task 2 的 blockedBy 中移除

关键洞察：“状态之所以能在压缩后保留，是因为它存放在对话之外。”
"""


import json
import os
import re
import subprocess
import time
import yaml
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import readline

    # #143 UTF-8 backspace fix for macOS libedit
    readline.parse_and_bind("set bind-tty-special-chars off")
    readline.parse_and_bind("set input-meta on")
    readline.parse_and_bind("set output-meta on")
    readline.parse_and_bind("set convert-meta off")
    readline.parse_and_bind("set enable-meta-keybindings on")
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = f"You are a coding agent at {WORKDIR}.  Use tools to solve tasks"

THRESHOLD = 50000
KEEP_RECENT = 3
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
PRESERVE_RESULT_TOOLS = {"read_file"}


def estimate_tokens(messages: list) -> int:
    """粗略 token 估算：约每 4 个字符算 1 个 token。"""
    return len(str(messages)) // 4


def micro_compact(messages: list) -> list:
    """将除 read_file 之外、且早于最近 3 条的 tool_result 内容，
    替换为 "[Previous: used {tool_name}]"
    """
    tool_results = []
    for msg_idx, msg in enumerate(messages):
        if msg["role"] == "user" and isinstance(msg["content"], list):
            for part_idx, part in enumerate(msg["content"]):
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    tool_results.append((msg_idx, part_idx, part))
    if len(tool_results) <= KEEP_RECENT:
        return messages
    tool_name_map = {}
    for msg in messages:
        if msg["role"] == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if hasattr(block, "type") and block.type == "tool_use":
                        tool_name_map[block.id] = block.name
    to_clean = tool_results[:-KEEP_RECENT]
    for _, _, result in to_clean:
        if (
            not isinstance(result.get("content"), str)
            or len(result.get("content")) < 100
        ):
            continue
        tool_use_id = result.get("tool_use_id", "")
        tool_name = tool_name_map.get(tool_use_id, "")
        if tool_name in PRESERVE_RESULT_TOOLS:
            continue
        result["content"] = f"[Previous: used {tool_name}]"
    return messages


def auto_compact(messages: list) -> list:
    """将完整对话保存到 .transcripts/
    让 LLM 对会话做总结。
    将全部 messages 替换为 [summary]。
    """
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    transcript_path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(transcript_path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    print(f"Saved transcript to {transcript_path}")
    conversation_text = json.dumps(messages, default=str)
    response = client.messages.create(
        model=MODEL,
        messages=[
            {
                "role": "user",
                "content": "Summarize this conversation for continuity. Include: "
                "1) What was accomplished, 2) Current state, 3) Key decisions made. "
                "Be concise but preserve critical details.\n\n" + conversation_text,
            }
        ],
        max_tokens=2000,
    )
    summary = next(
        (block.text for block in response.content if hasattr(block, "text")), ""
    )  # 只拿第一个有 text 的 block
    if not summary:
        summary = "No summary generated."
    return [
        {
            "role": "user",
            "content": f"[Conversation compressed. Transcript: {transcript_path}]\n\n{summary}",
        },
    ]


# -- Tool implementations --
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


TOOL_HANDLERS = {
    "bash": lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "compact": lambda **kw: "Manual compression requested",
}

TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read file contents.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to file.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace exact text in file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "compact",
        "description": "Trigger manual conversation compression.",
        "input_schema": {
            "type": "object",
            "properties": {
                "focus": {
                    "type": "string",
                    "description": "What to preserve in the summary",
                }
            },
        },
    },
]


# -- Agent loop with nag reminder injection --
def agent_loop(messages: list):
    rounds_since_todo = 0
    while True:
        micro_compact(messages)
        if estimate_tokens(messages) > THRESHOLD:
            print("[auto compact triggered]")
            messages[:] = auto_compact(messages)
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "compact":
                    manual_compact = True
                    output = "Compressing..."
                else:
                    handler = TOOL_HANDLERS.get(block.name)
                    try:
                        output = (
                            handler(**block.input)
                            if handler
                            else f"Unknown tool: {block.name}"
                        )
                    except Exception as e:
                        output = f"Error: {e}"
                print(f"> {block.name}:")
                print(str(output)[:200])
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(output),
                    }
                )
        messages.append({"role": "user", "content": results})
        if manual_compact:
            print("[manual compact requested]")
            messages[:] = auto_compact(messages)
            return


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms02 >> \033[0m ")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ["exit", "q", "quit", "bye", ""]:
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print("--------------------------------")
