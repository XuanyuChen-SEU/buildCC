# `s12_worktree_task_isolation.py` 说明文档

本文档按源码结构说明 [`s12_worktree_task_isolation.py`](./s12_worktree_task_isolation.py) 中各模块、类与函数的职责，便于维护与二次开发。

---

## 1. 模块定位与设计思想

该脚本实现 **「任务（控制平面）+ Git Worktree（执行平面）」** 的目录级隔离：多个并行任务各自在独立 worktree 目录中执行，通过共享任务板（`.tasks/task_*.json`）与 worktree 索引（`.worktrees/index.json`）协同。

核心数据流简述：

- **任务**：记录在 `REPO_ROOT/.tasks/task_{id}.json`，字段含 `subject`、`status`、`worktree` 绑定等。
- **Worktree**：`git worktree add` 创建到 `REPO_ROOT/.worktrees/{name}`，分支名为 `wt/{name}`，索引写在 `.worktrees/index.json`。
- **事件**：生命周期写入 `.worktrees/events.jsonl`（JSON Lines），供 `worktree_events` 工具查询。

脚本同时内置 Anthropic 客户端与 **工具（Tools）定义 + 处理函数**，在 `agent_loop` 中与模型对话并执行工具调用。

---

## 2. 顶层配置与辅助函数

### 2.1 导入与 readline（约 34–56 行）

- 标准库：`json`、`os`、`re`、`subprocess`、`threading`、`time`、`uuid`、`pathlib`、`dataclasses`、`typing` 等（部分如 `threading`、`uuid`、`dataclass` 在当前文件中未再使用，可能为历史遗留）。
- 第三方：`yaml`（当前文件内未见使用）、`anthropic`、`dotenv`。
- **readline**：在可用时绑定若干选项，注释说明用于 macOS libedit 下 UTF-8 退格等输入问题（`#143`）。

### 2.2 环境与全局变量（约 58–69 行）

| 符号 | 作用 |
|------|------|
| `load_dotenv(override=True)` | 从 `.env` 加载环境变量，后者覆盖已有值。 |
| `ANTHROPIC_BASE_URL` | 若设置，则删除 `ANTHROPIC_AUTH_TOKEN`（与自定义网关/代理用法相关）。 |
| `WORKDIR` | `Path.cwd()`，作为「工作区根」用于 `safe_path`、bash 工具等。 |
| `client` | `Anthropic(base_url=...)`，用于请求 API。 |
| `MODEL` | 从环境变量 `MODEL_ID` 读取，作为 `agent_loop` 中的模型 ID。 |

### 2.3 `detect_repo_root(cwd: Path) -> Path | None`

- 在 `cwd` 下执行 `git rev-parse --show-toplevel`（超时 10 秒）。
- 成功则返回解析出的仓库根 `Path`；失败或路径不存在则返回 `None`。
- 用于确定 `REPO_ROOT`，以便 `.tasks` / `.worktrees` 落在真实 Git 仓库根上。

### 2.4 `REPO_ROOT`

- `detect_repo_root(WORKDIR) or WORKDIR`：若在 Git 仓库内则取仓库根，否则退回当前工作目录。

### 2.5 `SYSTEM`

- 发给模型的 **系统提示** 字符串：说明当前工作目录、要求使用 task/worktree 工具做多任务与并行变更，并提到 `worktree_events` 用于生命周期可见性。

### 2.6 全局管理器实例

| 实例 | 含义 |
|------|------|
| `TASKS` | `TaskManager(REPO_ROOT / ".tasks")` |
| `EVENTS` | `EventBus(REPO_ROOT / ".worktrees" / "events.jsonl")` |
| `WORKTREES` | `WorktreeManager(REPO_ROOT, TASKS, EVENTS)` |

---

## 3. 类 `EventBus`

**职责**：将 worktree/任务相关事件以 **JSON 行** 追加写入单一日志文件。

### 3.1 `__init__(self, event_log_path: Path)`

- 保存 `self.path`。
- 创建父目录，`write_text("")` **清空或创建** 日志文件（每次构造都会截断为空文件）。

### 3.2 `emit(self, event, task=None, worktree=None, error=None)`

- 组装 payload：`event`、`ts`（`time.time()`）、`task`、`worktree`（默认空字典）；若传入 `error` 则增加 `error` 字段。
- 以 UTF-8 追加一行 `json.dumps(payload)`。

### 3.3 `list_recent(self, limit=20) -> str`

- 将 `limit` 限制在 `[1, 200]`。
- 读取整个日志，按行分割，取最后 `limit` 行；每行尝试 `json.loads`，失败则放入 `{"event": "parse_error", "raw": line}`。
- 返回 **缩进后的 JSON 字符串**（供工具展示）。

---

## 4. 类 `TaskManager`

**职责**：在 `self.dir` 下以 `task_{id}.json` 持久化任务，提供 CRUD 与 worktree 绑定。

### 4.1 `__init__(self, tasks_dir: Path)`

- 创建目录；`self._next_id = self._max_id() + 1`，用于 `create` 分配新 ID。

### 4.2 `_max_id(self) -> int`

- 对 `task_*.json` 用 `stem.split("_")[1]` 解析数字 ID，取最大值；无文件则返回 `0`。

### 4.3 `_path(self, task_id: int) -> Path`

- 返回 `self.dir / f"task_{task_id}.json"`。

### 4.4 `_load(self, task_id: int) -> dict`

- 若文件不存在抛 `ValueError`；否则读入并 `json.loads`。

### 4.5 `_save(self, task: dict)`

- 按 `task["id"]` 写入 `_path`，`indent=2`，`ensure_ascii=False`。

### 4.6 `create(self, subject, description="") -> str`

- 新建任务：`pending`、`blockedBy=[]`、`owner`、`worktree` 空串、`created_at`/`updated_at`。
- 保存后递增 `_next_id`，返回该任务的 **JSON 字符串**。

### 4.7 `get(self, task_id: int) -> str`

- 返回指定任务 JSON 格式化字符串。

### 4.8 `exists(self, task_id: int) -> bool`

- 判断对应 `task_*.json` 是否存在。

### 4.9 `update(self, task_id, status=None, owner="") -> str`

- 加载任务；若提供 `status` 则更新，且必须是 `pending` / `in_progress` / `completed` 之一，否则 `ValueError`。
- 若 `owner is not None` 则更新 `owner`（注意默认参数为 `""`，调用方传 `None` 可清空语义需结合调用约定）。
- 保存并返回 JSON 字符串。

### 4.10 `bind_worktree(self, task_id, worktree, owner="") -> str`

- 设置 `task["worktree"]`；若 `owner` 非空则更新负责人。
- 若当前状态为 `pending` 则改为 `in_progress`；更新 `updated_at`。
- 保存并返回 JSON 字符串。

### 4.11 `unbind_worktree(self, task_id: int) -> str`

- 清空 `worktree` 字段，更新 `updated_at`，保存并返回 JSON。

### 4.12 `_clear_dependency(self, completed_id: int)`

- 遍历所有任务文件，从各任务的 `blockedBy` 列表中移除 `completed_id`（若存在则写回）。**当前文件中未被其他公有方法调用**（可能预留给后续「完成依赖清理」流程）。

### 4.13 `list_all(self) -> str`

- 按任务 ID 排序读取所有任务；若无任务返回 `"No tasks."`。
- 否则生成人类可读多行文本：`[ ]` / `[>]` / `[x]` 对应 `pending` / `in_progress` / `completed`，并附带 `blockedBy` 提示。

---

## 5. 类 `WorktreeManager`

**职责**：在 `repo_root` 下维护 `.worktrees` 目录、调用 `git worktree`，并与 `TaskManager`、`EventBus` 联动。

### 5.1 `__init__(self, repo_root, tasks, events)`

- 设置 `repo_root`、`tasks`、`events`；确保 `.worktrees` 存在。
- 若 `index.json` 不存在则初始化为 `{"worktrees": []}`。
- `self.git_available = self._is_git_repo()`。

### 5.2 `_is_git_repo(self) -> bool`

- 在 `repo_root` 执行 `git rev-parse --show-toplevel`，根据返回码判断是否 Git 仓库。

### 5.3 `_run_git(self, args: list[str]) -> str`

- 若 `git_available` 为假则抛 `RuntimeError`。
- `subprocess.run(["git", *args], cwd=repo_root, ...)`，超时 120 秒；失败则抛 `RuntimeError`（信息为 stdout+stderr）。
- 成功则返回合并后的输出文本（空则 `"(no output)"`）。

### 5.4 `_load_index(self) -> list`

- **实际返回**：`json.loads(self.index_path.read_text())` 的 **整个对象**（应为 `dict`，键 `worktrees`）。命名上略易误解为「仅 list」。

### 5.5 `_save_index(self, index: list)`

- 将 `index` 整体写回 `index.json`（参数名 `list` 但实际应为包含 `worktrees` 的 dict）。

### 5.6 `_find(self, name: str) -> dict | None`

- 在索引的 `worktrees` 列表中按 `name` 查找并返回条目，找不到返回 `None`。

### 5.7 `_validate_name(self, name: str)`

- 要求名称匹配 `^[A-Za-z0-9._-]{1,40}$`，否则 `ValueError`。

### 5.8 `create(self, name, task_id=None, base_ref="HEAD") -> str`

- 校验名称；索引中已存在同名则报错；若指定 `task_id` 则任务必须存在。
- `events.emit("worktree.create.before", ...)`。
- 执行 `git worktree add -B wt/{name} {path} {base_ref}`，其中 `path = self.dir / name`。
- 构造索引条目：`name`、`path`、`branch`、`task_id`、`status: active`、`created_at`；追加到索引并保存。
- 若绑定了 `task_id`，调用 `tasks.bind_worktree`。
- 成功则 `emit("worktree.create.after")` 并返回条目 JSON 字符串；异常则 `emit("worktree.create.error", error=...)` 后重新抛出。

### 5.9 `list_all(self) -> str`

- 列出索引中所有 worktree 一行一条：状态、名称、路径、分支、可选 `task_id`。

### 5.10 `status(self, name: str) -> str`

- 解析 worktree；路径不存在则返回错误字符串。
- 在 `repo_root` 下执行 `git status --short --porcelain {path}`，返回输出或 `"Clean worktree"`。

### 5.11 `run(self, name: str, command: str) -> str`

- 对命令字符串做简单危险子串检测（如 `rm -rf /`、`sudo` 等），命中则直接返回错误信息。
- 解析 worktree 路径；用 `subprocess.run(command.split(), shell=True, cwd=path, ...)` 执行（**注意**：`shell=True` 与 `command.split()` 组合非常规，复杂 shell 语法可能不符合预期）。
- 超时 300 秒；输出截断至 50000 字符。

### 5.12 `remove(self, name, force=False, complete_task=False) -> str`

- `emit("worktree.remove.before")`。
- `git worktree remove [--force] <path>`。
- 若 `complete_task` 且条目有 `task_id`：读取任务、`update(..., status="completed")`、**`tasks.unbind_worktree(task_id)`**（解除任务上的 worktree 字段）、`emit("task.complete")`。
- 更新索引中对应项：`status` 改为 `removed`，写入 `removed_at`。
- `emit("worktree.remove.after")`；成功返回 `"Removed worktree '{name}'"`；失败则 `emit("worktree.remove.failed")` 并重新抛出异常。

### 5.13 `keep(self, name: str) -> str`

`WorktreeManager` 的实例方法（与 `remove` 同级，定义于类体内）。

- 用 `_find(name)` 解析索引条目；不存在则返回 **`f"Error: Unknown worktree '{name}'"`**。
- 加载完整索引，在 `worktrees` 列表中找到同名项：将 `status` 设为 **`kept`**，写入 **`kept_at`**（当前时间戳），保存索引。
- `emit("worktree.keep", ...)`，附带任务 id（若有）与 worktree 元数据。
- 若上一步循环成功命中条目，返回该条目的 **JSON 字符串**；否则返回字面错误串 `"Error: Unknown worktree '{name}'"`（未使用 f-string，与首行 f-string 错误信息略有差异；正常数据一致时后者极少触发）。

---

## 6. 模块级工具函数（工作区文件与 Shell）

这些函数主要服务于 `TOOL_HANDLERS` 中的 `bash` / `read_file` / `write_file` / `edit_file`，操作范围限制在 `WORKDIR` 之下（通过 `safe_path`）。

### 6.1 `safe_path(p: str) -> Path`

- 解析为 `(WORKDIR / p).resolve()`；若结果不是相对于 `WORKDIR` 的子路径，则 `ValueError`（防止路径逃逸）。

### 6.2 `run_bash(command: str) -> str`

- 危险命令子串检测（与 worktree 内 `run` 类似，此处为大小写敏感子串匹配）。
- `shell=True`、`cwd=WORKDIR`、超时 120 秒；输出截断 50000 字符。

### 6.3 `run_read(path, limit=None) -> str`

- `safe_path(path)` 读全文按行分割；若 `limit` 小于总行数则截断并追加提示行。
- 总返回长度上限约 50000 字符。

### 6.4 `run_write(path, content) -> str`

- 确保父目录存在后写入；返回写入字节数说明或错误信息。

### 6.5 `run_edit(path, old_text, new_text) -> str`

- 读文件；若 `old_text` 未出现则报错；否则仅替换 **第一次** 出现；写回并返回成功信息。

---

## 7. `TOOL_HANDLERS` 与 `TOOLS`

### 7.1 `TOOL_HANDLERS`

字典：**工具名 → 可调用对象**（lambda 接收 `**kw`，从模型 `tool_use` 的 `input` 取参）。

| 键 | 底层行为 |
|----|-----------|
| `bash` | `run_bash(command)` |
| `read_file` | `run_read(path, limit)` |
| `write_file` | `run_write(path, content)` |
| `edit_file` | `run_edit(path, old_text, new_text)` |
| `task_create` | `TASKS.create(subject, description)` |
| `task_list` | `TASKS.list_all()` |
| `task_get` | `TASKS.get(task_id)` |
| `task_update` | `TASKS.update(task_id, status, owner)` |
| `task_bind_worktree` | `TASKS.bind_worktree(task_id, worktree, owner)` |
| `worktree_create` | `WORKTREES.create(name, task_id, base_ref)` |
| `worktree_list` | `WORKTREES.list_all()` |
| `worktree_status` | `WORKTREES.status(name)` |
| `worktree_run` | `WORKTREES.run(name, command)` |
| `worktree_remove` | `WORKTREES.remove(name, force, complete_task)` |
| `worktree_keep` | `WORKTREES.keep(name)` |
| `worktree_events` | `EVENTS.list_recent(limit)` |

### 7.2 `TOOLS`

- 供 Anthropic API 使用的 **工具 JSON Schema 列表**：每个元素含 `name`、`description`、`input_schema`（`type: object`、`properties`、`required`）。
- 描述中有个别拼写笔误（如 `optionallt`、`optinally`），不影响字段结构理解。

---

## 8. `agent_loop(messages: list)`

- 无限循环：调用 `client.messages.create(..., tools=TOOLS, max_tokens=8000)`。
- 将 `response.content` 追加到 `messages`。
- 若 `stop_reason != "tool_use"` 则 **return**（结束本轮循环，由调用方继续读历史）。
- 否则遍历 `content` 中 `type == "tool_use"` 的块：查 `TOOL_HANDLERS` 执行，捕获异常转为错误字符串；打印工具名与输出前 200 字符；组装 `tool_result` 列表。
- 将 `{"role": "user", "content": results}` 追加到 `messages`（标准 tool result 回合），然后循环继续。

---

## 9. `if __name__ == "__main__":` 交互入口

- 打印 `REPO_ROOT`；若 `WORKTREES.git_available` 为假则提示非 Git 仓库并 `exit(1)`。
- 维护 `history` 列表；REPL 循环读取 `s12 >>` 输入，`exit`/`q`/`quit`/`bye`/空行则退出。
- 每轮将用户消息加入 `history`，调用 `agent_loop(history)`。
- 尝试从最后一条 assistant 的 `content` 中打印带 `.text` 属性的块；打印分隔线。

---

## 10. 数据文件与事件名速查

| 路径 | 用途 |
|------|------|
| `.tasks/task_{id}.json` | 单任务持久化 |
| `.worktrees/index.json` | worktree 元数据列表 |
| `.worktrees/events.jsonl` | 追加式事件日志 |

常见 `emit` 事件名：`worktree.create.before/after/error`、`worktree.remove.before/after/failed`、`task.complete`、`worktree.keep`。

---

## 11. 小结

该文件将 **任务看板 + Git worktree 生命周期 + 事件日志 + 受限文件/bash 工具** 封装为一套 Agent 可调用的工具集，并在 `__main__` 下提供最小 REPL。阅读或修改时建议特别注意 **`worktree_run` 中 `shell=True` 与 `split()` 的交互**，避免安全与可移植性问题。

如需与仓库其他 agent 脚本对齐行为，可对比 [`s01_agent_loop.py`](./s01_agent_loop.py)、[`s_full.py`](./s_full.py) 等中的工具注册方式。
