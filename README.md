# AI Agent 应用开发

> **第 1 周 · AI 与 Python 应用基础**
> 12 周 AI Agent 应用开发 Roadmap 的起点。从 C++ 工程思维切换到 Python AI 应用工程，建立可复现的工程环境、统一的代码质量与测试基线。

---

## 1. 项目目标

本周结束时能够从空目录重建本项目，并独立完成以下事项：

- 解释 **AI / 机器学习 / 深度学习 / 生成式 AI / LLM / Agent / RAG / Tool / Workflow / MCP** 的基本区别与彼此关系。
- 使用 **Python 3.12 + uv + pyproject.toml + uv.lock** 还原统一开发环境。
- 使用 **Ruff** 规范代码风格与静态检查，使用 **pytest** 跑通最小测试套件。
- 调用团队模型接口（Day 3 起），用 **Pydantic** 做结构化输出，用 **FastAPI** 暴露 `/health`、`/chat`、`/analyze-requirement`。
- 全部接口、配置、密钥都符合部门目录与提交规范。

后续路线见：`docs/weekXX_summary.md`、`docs/architecture.md`（第 9 周起补齐）。

---

## 2. 目录结构

```
week01_ai_basics/
├── README.md                # 本文件
├── pyproject.toml           # 项目元数据 + 依赖 + Ruff/pytest 配置
├── uv.lock                  # 依赖锁定（必须入库）
├── .env.example             # 环境变量模板（真实 .env 不入库）
├── .gitignore               # Git 忽略规则
├── .python-version          # 锁定 Python 3.12（入库）
├── main.py                  # uv init 生成的最小入口
├── src/                     # 业务代码
├── tests/                   # pytest 测试
└── docs/                    # 学习笔记、阶段交付物
    └── day01_environment.md # Day 1 环境记录（当天交付物）
```

> 部门统一工程目录见 Roadmap 第 16 页 `七、统一工程目录与每周交付标准`。本项目在第 1 周仅启用 `src/`、`tests/`、`docs/`，`Dockerfile` / `compose.yaml` 从第 9 周加入。

---

## 3. 技术栈

| 层次        | 选型                        | 用途                                  |
| ----------- | --------------------------- | ------------------------------------- |
| 运行环境    | Python 3.12                 | 主线语言；与 uv 锁定                  |
| 依赖管理    | uv + pyproject.toml         | 虚拟环境、依赖安装、锁文件            |
| 代码质量    | Ruff（lint + format）       | 静态检查 + 格式化 + import 排序        |
| 单元测试    | pytest                      | 单元 / 集成测试                       |
| HTTP 客户端 | httpx                       | 同步 + 异步模型接口调用（Day 3 起）   |
| 数据模型    | Pydantic v2                 | 配置校验、结构化输出                   |
| API 服务    | FastAPI + Uvicorn           | 第 1 周末最小服务                     |
| 配置加载    | python-dotenv               | 读取 `.env` 中的密钥与配置            |

---

## 4. 环境要求

| 工具   | 版本     | 校验命令                  |
| ------ | -------- | ------------------------- |
| Python | 3.12.x   | `uv run python --version` |
| uv     | ≥ 0.5    | `uv --version`            |
| Git    | 任意新版 | `git --version`           |

> **不要在系统级 pip 安装依赖**。所有依赖都通过 `uv add` 加入 `pyproject.toml`，并由 `uv.lock` 锁定。

---

## 5. 快速启动

```bash
# 1. 克隆仓库
git clone <repo-url> week01_ai_basics
cd week01_ai_basics

# 2. 同步依赖（首次会创建 .venv 并安装锁定的全部包）
uv sync

# 3. 复制环境变量模板
cp .env.example .env         # Git Bash / PowerShell
# cmd 中：  copy .env.example .env

# 4. 探活 / 运行主入口
uv run python main.py
# 或后续 Day 5 起：  uv run fastapi dev main.py
```

> 命令运行环境说明：
> - `mkdir` / `cp` / `rm` 走 **Git Bash**。
> - `copy` / `del` / `dir` 走 **cmd**。
> - `uv add` / `uv run` 在以上两种环境下命令一致。

---

## 6. Git 提交规范

遵循软件部门现有 Git Commit 模板（避免 `update`、`test`、`fix bug` 等无意义提交）：

```
ix: usb: notice: Some CPUs have low bandwidth

...

OA: XQSQ202403255
bug: #13385
from: rockchip

os: debian10,android11
soc: rk3568,rk3566

```

常用 `type`：

| type       | 用途                       |
| ---------- | -------------------------- |
| `feat`     | 新功能                     |
| `fix`      | 缺陷修复                   |
| `docs`     | 仅文档变更                 |
| `style`    | 格式调整（无逻辑）         |
| `refactor` | 重构（非新功能 / 非修复）  |
| `test`     | 测试相关                   |
| `chore`    | 构建 / 工具 / 依赖         |

