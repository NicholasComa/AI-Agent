# Week 07 工具说明 — jwipc-dev-mcp-server

> 对应实现：`src/jwipc_dev_mcp_server/tools.py`（工具逻辑）、`schemas.py`（出参模型）、`config.py`（上限配置）。
> 服务标识：`jwipc-dev-mcp-server`；协议端点：stdio（默认）或 Streamable HTTP `http://127.0.0.1:8765/mcp`。

---

## 1. 工具总览

| 工具 | 用途 | 只读 | 幂等 |
| --- | --- | --- | --- |
| `ping` | 连通性检查，确认会话完成 initialize 握手 | 是 | 是 |
| `list_files` | 列出沙箱内某目录的文件与子目录（不递归，跳过 `.git`） | 是 | 是 |
| `read_file` | 读取沙箱内一个 UTF-8 文本文件（超限需确认，二进制拒绝） | 是 | 是 |
| `git_log` | 查看沙箱内 git 仓库最近提交，并逐条校验提交信息格式 | 是 | 是 |
| `check_commit_message` | 校验一条提交信息是否符合 `type: scope: subject` 规范 | 是 | 是 |

所有工具在 `ToolAnnotations` 上自我声明为只读（`readOnlyHint=true`、`destructiveHint=false`、`idempotentHint=true`、`openWorldHint=false`）。这是给客户端/模型看的行为提示，**真正的边界由服务端三道闸守卫**（路径白名单 / 命令白名单 / 确认门，详见 `week07_security_report.md`）。

## 2. 公共返回信封

所有工具返回 Pydantic 模型（`extra="forbid"`，多传字段直接被 SDK 拒绝），出参由 SDK 自动生成 `outputSchema`，客户端从 `structuredContent` 拿到结构化结果。每个结果都继承公共信封：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ok` | bool | 成功 `true` / 失败 `false` |
| `error` | str \| null | 失败原因，成功时为空 |
| `kind` | str \| null | 错误类别（见各工具异常码表） |

失败结果统一由 `tool_failure()` 构造，保证错误形态一致；工具内部异常一律降级为失败结果，不向协议层抛异常。

## 3. ping

**入参**：无。

**出参**：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ok` | bool | 恒为 `true` |
| `server` | str | 服务标识 `jwipc-dev-mcp-server` |
| `message` | str | 固定 `pong` |

## 4. list_files

**入参**：

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `path` | str | `"."` | 沙箱内相对路径，必须是目录 |
| `limit` | int \| null | `null` | 返回条目上限；缺省用配置 `list_limit`（200），最大 200 |

**出参**（成功）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `path` | str | 回显请求路径 |
| `entries` | list[FileEntry] | `rel_path`（沙箱内 POSIX 相对路径）/ `is_dir` / `size_bytes`（目录恒为 0） |
| `total` | int | 实际条目总数 |
| `truncated` | bool | 是否因超出上限被截断 |

排序规则：目录在前，其余按名称不区分大小写排序。

**异常码**：

| kind | 触发场景 |
| --- | --- |
| `bad_path` | 路径为空 / 含空字节 / 绝对路径或盘符写法 |
| `forbidden` | `..` 穿越、解析后越出沙箱（含符号链接逃逸） |
| `not_found` | 目录不存在 |
| `not_dir` | 目标是文件不是目录 |

## 5. read_file

**入参**：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `path` | str | 沙箱内相对路径，必须是文件 |

**出参**（成功）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `path` | str | 回显请求路径 |
| `content` | str | 文件全文（UTF-8 解码） |
| `bytes` | int | 文件字节数 |
| `truncated` | bool | 保留字段，当前恒为 `false` |

**异常码**：

| kind | 触发场景 |
| --- | --- |
| `bad_path` / `forbidden` | 同 `list_files`（路径白名单拦截） |
| `not_found` | 文件不存在 |
| `is_dir` | 目标是目录 |
| `needs_confirmation` | 文件超过 `max_read_bytes`（默认 50000 字节）且未过确认门；确认后同一文件本次进程内放行 |
| `binary_or_undecodable` | 内容不是有效 UTF-8（疑似二进制），拒绝读取 |

## 6. git_log

**入参**：

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `path` | str | `"."` | 沙箱内相对路径，必须是 git 仓库（或其子目录） |
| `limit` | int \| null | `null` | 提交条数上限；缺省用配置 `git_log_limit`（20），最大 200 |

**出参**（成功）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `path` | str | 回显请求路径 |
| `commits` | list[CommitLogEntry] | `sha` / `author` / `date`（ISO 格式）/ `subject` / `message_valid`（是否符合提交规范） |
| `returned` | int | 实际返回条数 |
| `truncated` | bool | 是否还有更早提交未返回 |

实现要点：服务端拼装 `git -C <目标> log -n<N> --date=iso --pretty=format:%H%x1f%an%x1f%ad%x1f%s%x1e`，分隔符用 git `%x` 转义（纯 ASCII argv，避免 Windows 传参吞控制字符）；`subprocess` 带 15 秒超时、`stdin=DEVNULL`。**客户端只能传 `path`/`limit`，无法注入任何 git 参数**——命令行由服务端固定拼装，且经过 `GitCommandPolicy` 白名单校验。

**异常码**：

| kind | 触发场景 |
| --- | --- |
| `bad_path` / `forbidden` | 同 `list_files`（路径白名单拦截） |
| `not_found` / `not_dir` | 目录不存在 / 目标是文件 |
| `argument_rejected` | 拼装出的 argv 未通过 `GitCommandPolicy` 白名单（正常调用不会触发，属纵深防御） |
| `git_error` | git 执行失败（目标不是 git 仓库 / 超时 15s / 退出码非 0） |

## 7. check_commit_message

**入参**：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `message` | str | 待校验的提交信息（一行 header） |

**出参**：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `valid` | bool | 是否符合规范 |
| `errors` | list[str] | 不合规时的具体原因列表 |
| `parsed` | CommitParsed | 解析结果：`type` / `scope` / `subject` / `breaking` / `has_body`（解析失败时为 null） |

校验规则复用 `src/devagent/tools/check_commit_message.py`（Week 3 交付），接受 `<type>(<scope>): subject` 或 `<type>: <scope>: subject`（scope 必填）。

**异常码**：

| kind | 触发场景 |
| --- | --- |
| `empty_message` | 提交信息为空或纯空白 |
| `internal` | 校验器自身故障（降级为失败结果，不抛协议异常） |

## 8. 配置与上限

| 配置项 | 默认值 | 环境变量 | 说明 |
| --- | --- | --- | --- |
| 沙箱根目录 | `data/mcp_sandbox` | `JWIPC_MCP_ROOT` | 所有文件类工具的访问边界 |
| 单次读取上限 | 50000 字节 | `JWIPC_MCP_MAX_READ_BYTES` | 超过触发 `needs_confirmation` |
| 目录列表上限 | 200 条 | `JWIPC_MCP_LIST_LIMIT` | 超出截断 |
| 提交日志上限 | 20 条 | `JWIPC_MCP_GIT_LOG_LIMIT` | 超出截断 |

环境变量非法取值直接忽略并回落默认值，保证服务总能启动。

## 9. 调用示例

```python
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path("src").resolve()))
from jwipc_dev_mcp_server.client import connect_stdio

async def main():
    # stdio：拉起 scripts/mcp_week7_server.py 子进程并握手
    async with connect_stdio(sys.executable, ["scripts/mcp_week7_server.py"]) as session:
        res = await session.call_tool("read_file", {"path": "README.md"})
        print(res.structuredContent["content"])

asyncio.run(main())
```

命令行方式见 `scripts/mcp_week7_smoke.py`（冒烟）与 `scripts/mcp_week7_review.py`（业务串联「审查一次提交改动」）。
