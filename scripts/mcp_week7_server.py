"""开发用 MCP 服务的启动入口。

之所以保留为脚本（而不是用 ``python -m`` 模块的方式），是为了让 MCP 客户端和
Inspector 能用一条命令直接拉起它，不需要额外的参数。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # noqa: E402

from jwipc_dev_mcp_server import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
