# Week 07 安全测试报告 — jwipc-dev-mcp-server

> 范围：MCP Server 的安全边界设计与验证。测试基线：`tests/test_mcp_security.py`（10 条）+ `tests/test_mcp_server_tools.py`（15 条）+ 端到端异常场景实测（`scripts/mcp_week7_review.py --demo-errors`）。
> 结论先行：**三道闸（路径白名单 / 命令白名单 / 确认门）全部按预期拦截，全量测试 350 passed + 1 skipped，异常场景协议返回与设计一致。**

---

## 1. 威胁模型与设计原则

MCP 工具本质是「让远端调用方（以及自动调用工具的 LLM）能读文件、跑命令」，如果不设防，等于开放了任意文件读取与命令执行。本周按**最小权限 + 纵深防御**设计，三道闸各管一层：

| 闸门 | 实现 | 拦截目标 |
| --- | --- | --- |
| 路径白名单 | `SandboxRoot`（`security.py`） | 路径穿越 / 绝对路径 / 盘符 / 空字节 / 符号链接逃逸——把爆炸半径锁死在沙箱目录内 |
| 命令白名单 | `GitCommandPolicy`（`security.py`） | git 子命令与参数注入——只允许只读子命令与白名单选项 |
| 确认门 | `ConfirmationGate`（`security.py`） | 超限大文件读取——未二次确认不执行 |

配套原则：

- **工具是「能做什么」，白名单是「只能在哪做」**：只加工具不放白名单就是安全漏洞，两者配套缺一不可。
- **不依赖调用方自觉**：工具由模型根据自然语言自动调用，无法保证每次都传合法参数，硬约束必须在服务端代码层。
- **错误平铺降级**：安全失败统一走 `tool_failure()` 返回 `ok=false + kind`，不向协议层抛异常，避免给调用方暴露堆栈细节。

## 2. 路径白名单 SandboxRoot

### 2.1 机制

`resolve(rel_path)` 的校验序列（`security.py:34-50`）：

1. 空路径 / 纯空白 → `bad_path`；
2. 含空字节 `\x00` → `bad_path`；
3. 绝对路径、Windows 盘符、UNC 根 → `bad_path`；
4. `..` 穿越 → `forbidden`；
5. 与根目录拼接后 `resolve()` 规范化，再 `is_relative_to(根)` 复核——**符号链接逃逸在这一步被拦下**（链接指向沙箱外时解析结果不再位于根内）。

通过复核后才返回候选路径；文件工具拿到的路径必然在沙箱内。

### 2.2 拦截类别

| kind | 触发样例 |
| --- | --- |
| `bad_path` | `""`、`"a\x00b"`、`"C:/Windows/win.ini"`、`"\\\\server\\share"` |
| `forbidden` | `"../.."`、`"sub/../../escape"`、符号链接指向沙箱外 |

### 2.3 验证结果

`tests/test_mcp_security.py` 覆盖：空路径、空字节、绝对路径、盘符路径、`..` 穿越、深层 `..`、符号链接逃逸、正常路径放行等。端到端实测（stdio 与 Streamable HTTP 双链路一致）：

```json
// list_files {"path": "../.."}
{
  "ok": false,
  "error": "不允许路径穿越(..): '../..'",
  "kind": "forbidden",
  "path": "../..",
  "entries": [], "total": 0, "truncated": false
}
// read_file {"path": "C:/Windows/win.ini"} 同样被拒（bad_path，绝对路径）
```

> 说明：符号链接逃逸用例在 Windows 普通用户下因无权限创建 symlink 被 `pytest.skip`（需开发者模式或管理员权限）。拦截逻辑本身由第 5 步 `is_relative_to` 复核承担，其余用例已覆盖同一段代码路径。

## 3. 命令白名单 GitCommandPolicy

### 3.1 机制

两层防线配合：

- **服务端固定拼装**：`git_log` 的 argv 由服务端模板拼出（`git -C <目标> log -n<N> --date=iso --pretty=format:...`），客户端入参只有 `path` 和 `limit`，**协议层根本无法注入 git 参数**。
- **argv 白名单校验**（纵深防御）：拼好的 argv 交给 `GitCommandPolicy.validate()` 再过一遍：
  - 第一个 token 必须是 `git`，否则拒绝；
  - 全局选项区（子命令前）：`-C`（精确匹配，自动跳过其后的路径值）、`--`（分隔符）只允许精确匹配，防 `--all` 之类选项被前缀规则误放行；`-n`、`--date`、`--pretty`、`--format`、`--oneline`、`--max-count`、`--no-pager`、`-1` 允许前缀匹配（兼容 `-n20`、`--date=iso` 合并写法）；
  - 子命令必须落在只读集合 `{log, rev-list, status, show, diff, ls-files}` 内，`push`/`reset`/`commit` 等一律拒绝；
  - 子命令之后的参数逐项过同样的白名单，非选项 token（路径、条数值）跳过。

校验失败抛 `CommandPolicyError(kind="argument_rejected")`，工具捕获后转为失败结果。

### 3.2 验证结果

单元级（`tests/test_mcp_security.py`）与本地策略演示（`--demo-errors`）实测：

```text
放行: ['git', '-C', 'data/mcp_sandbox', 'log', '-n5']
拦截: ['git', 'push', '--force', 'origin', 'main']
      kind=argument_rejected  不允许的 git 子命令: ['push', '--force', 'origin', 'main']
拦截: ['git', 'log', '--all', '--grep=password']
      kind=argument_rejected  不允许的 git 参数: '--all'
```

`--` 精确匹配规则的必要性：若按前缀放行 `--`，则 `--all`、`--grep` 等危险选项都会被误放行（周三开发中发现并修正）。

`subprocess` 加固：`stdin=DEVNULL`（Server 的 stdin 是协议管道，不能被子进程消费）、`timeout=15s` 超时转 `git_error`。

## 4. 确认门 ConfirmationGate

### 4.1 机制

对「超过 `max_read_bytes`（默认 50000 字节）」的文件读取，先查确认状态：

- 未确认 → 返回 `needs_confirmation`，不读文件；
- 已确认（`gate.confirm(key)`，key 形如 `read:<文件绝对路径>`）→ 放行，同一 key 本次进程内后续直接通过；
- `revoke(key)` 可撤销确认回到需确认状态。

状态存进程内存（`set[str]`），key 按 `种类:值` 生成，可预测、可复现——测试可以直接预置确认状态。

### 4.2 验证结果

端到端实测（116254 字节的 `large_log.txt`，上限 50000）：

```json
// read_file {"path": "large_log.txt"}（未确认）
{
  "ok": false,
  "error": "文件 116254 字节，超过上限 50000 字节，确认后才能读取",
  "kind": "needs_confirmation",
  "path": "large_log.txt",
  "content": "", "bytes": 0, "truncated": false
}
```

单元测试（`test_mcp_server_tools.py::test_read_file_large_after_confirm`）注入 `ConfirmationGate` 并 `confirm()` 后，同一文件成功放行读取——「拦截 + 放行」两个分支都覆盖。

## 5. 其他安全相关行为

| 项 | 行为 |
| --- | --- |
| 二进制文件读取 | 非 UTF-8 内容拒绝读取（`binary_or_undecodable`），不当文本硬解码 |
| 工具行为声明 | 5 个工具全部 `readOnlyHint=true` / `destructiveHint=false`，供客户端与模型参考（自我声明，非硬边界） |
| 输入模型约束 | 出参模型 `extra="forbid"`，多传未定义字段被 SDK 直接拒绝 |
| 错误信息暴露 | 失败只返回 `kind` 与一句可读原因，不带内部路径解析细节之外的堆栈信息 |
| 子进程环境 | git 子进程 `stdin=DEVNULL` + 15s 超时，防止挂起与管道污染 |
| 传输层 | Streamable HTTP 由 SDK 自带 DNS 重绑定保护（`127.0.0.1` 的 Host 必须带端口）；stdio 下 stdout 专用于协议，日志全部走 stderr |

## 6. 测试汇总

| 验证层 | 内容 | 结果 |
| --- | --- | --- |
| 单元测试 `test_mcp_security.py`（10 条） | SandboxRoot 各类越界 / GitCommandPolicy 注入拦截 / ConfirmationGate 状态 | 全部通过 |
| 单元测试 `test_mcp_server_tools.py`（15 条） | 4 工具常规 + 边界（`too_large`/`binary`/`not_found`/确认后放行） | 全部通过 |
| 全量回归 | `uv run pytest -q` | **350 passed, 1 skipped**（skip 为 Windows symlink 权限限制，见 §2.3） |
| 端到端异常场景 | `--demo-errors`：越界 → `forbidden`；大文件 → `needs_confirmation`；git 注入 → `argument_rejected` | 三场景返回与设计一致，stdio 与 Streamable HTTP 双链路各实测一轮 |
| 静态检查 | `uv run ruff check .` / `uv run ruff format --check .` | 全绿 |

## 7. 已知限制

1. **确认门状态不跨进程**：确认状态存单进程内存，Streamable HTTP 多 worker 部署时各 worker 独立；且当前没有通过协议暴露「确认」操作（客户端无法远程确认，测试通过注入 gate 预置状态）。生产化需要持久化存储 + 明确的确认协议入口。
2. **符号链接用例在 Windows 默认跳过**：拦截逻辑已被代码覆盖，但该子场景需要开发者模式才能端到端实测。
3. **白名单为静态配置**：git 只读子命令集合与参数前缀硬编码在 `security.py`，扩展需改代码；未提供运行时白名单管理（刻意保持简单，降低配错风险）。
