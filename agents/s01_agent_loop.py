#!/usr/bin/env python3
# 框架：循环 —— 持续将真实工具结果回传给模型。
"""
s01_agent_loop.py - 智能体循环

这个文件演示了最小可用的编码智能体模式：

    用户消息
      -> 模型回复
      -> 如果发生 tool_use：执行工具
      -> 将 tool_result 写回 messages
      -> 继续循环

它有意保持循环结构精简，但仍然显式展示循环状态，
这样后续章节就可以在同一结构上逐步扩展。
"""

import os
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

try:
  import readline
  # #143 UTF-8 backspace fix for macOS libedit
  readline.parse_and_bind('set bind-tty-special-chars off')
  readline.parse_and_bind('set input-meta on')
  readline.parse_and_bind('set output-meta on')
  readline.parse_and_bind('set convert-meta off')
  readline.parse_and_bind('set enable-meta-keybindings on')
except ImportError:
  pass

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
  os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = (
  f"You are a coding agent at {os.getcwd()}. "
  "Use bash to inspect and change the workspace. Act first, then report clearly."
)

TOOLS = [{
  "name": "bash",
  "description": "Run a shell command in the current workspace.",
  "input_schema": {
    "type": "object",
    "properties": {"command": {"type": "string"}},
    "required": ["command"],
  },
}]


@dataclass
class LoopState:
  messages: List
  turn_count: int = 1
  transition_reason: str | None = None


def run_bash(command: str) -> str:
  dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
  if any(item in command for item in dangerous):
    return "Error: Dangerous command blocked"
  try:
    result = subprocess.run(
      command,
      shell=True,
      cwd=os.getcwd(),
      capture_output=True,
      text=True,
      timeout=120,
    )
  except subprocess.TimeoutExpired:
    return "Error: Timeout (120s)"
  except (FileNotFoundError, OSError) as e:
    return f"Error: {e}"

  output = (result.stdout + result.stderr).strip()
  return output[:50000] if output else "(no output)"

def extract_text(content: List[dict]) -> str:
  if not isinstance(content, list):
    return ""
  texts = []
  for block in content:
    text = getattr(block, "text", None)
    if text:
      texts.append(text)
  return "\n".join(texts).strip()


def execute_tool_calls(response_content: List[dict]) -> List[dict]:
  results = []
  for block in response_content:
    if block.type != "tool_use":
      continue
    command = block.input["command"]
    output = run_bash(command)
    results.append({"role": "tool", "content": output})
  return results


def run_one_turn(state: LoopState) -> bool:
  messages = state.messages
  response = client.messages.create(
    model=MODEL, system=SYSTEM, messages=messages,
    tools=TOOLS, max_tokens=8000,
  )
  state.messages.append({"role": "assistant", "content": response.content})

  if response.stop_reason != "tool_use":
    state.transition_reason = None
    return False
  
  results = execute_tool_calls(response.content)
  if not results:
    state.transition_reason = "No tool calls found"
    return False

  state.messages.append({"role": "tool", "content": results})
  state.turn_count += 1
  state.transition_reason = "tool_result"
  return True


def agent_loop(state: LoopState) -> None:
  while True:
    if not run_one_turn(state):
      pass


if __name__ == "__main__":
  history = []
  while True:
    try:
      query = input("\033[36ms01:\033[0m ")
    
    except (EOFError, KeyboardInterrupt):
      break
    if query.strip().lower() in ["exit", "quit", "bye"]:
      break
    history.append({"role": "user", "content": query})
    state = LoopState(messages=history)
    agent_loop(state)
    
    final_text = extract_text(history[-1]["content"])
    if final_text:
      print(final_text)
    print("\n")