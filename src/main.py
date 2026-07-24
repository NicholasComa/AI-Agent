"""Day 5 - FastAPI 服务入口。

运行本模块有两种方式：

1. **通过 FastAPI CLI（开发推荐）**::

       uv run fastapi dev src/main.py     # 自动重载
       uv run fastapi run src/main.py     # 生产模式

   CLI 会导入本模块并寻找名为 ``app`` 的属性。下面的模块级 ``app``
   由 :func:`src.app.create_app` 构建。

2. **直接通过 Python（测试与一次性启动用）**::

       uv run python src/main.py

   这会进入 ``if __name__ == "__main__"`` 分支并手动启动 uvicorn。

为什么 :func:`dotenv.load_dotenv` 要在导入时运行
-----------------------------------------------

:class:`AppConfig` 加载器会读取 ``os.environ``；如果不在最前面调用
``load_dotenv``，``.env`` 中设置的 ``MODEL_NAME``、``API_BASE_URL``
和 ``API_KEY`` 就无法传递到 :class:`LlmClient`。当 ``override=False``
时，任何已存在的环境变量都保持不变（适用于 CI / 容器等由外部注入密钥的场景）。
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

# 在导入应用工厂之前加载 .env - 工厂的配置加载器在构建时会读取 os.environ。
load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env", override=False)

from app import app  # noqa: E402,F401  FastAPI CLI 需要这个顶层的 ``app`` 属性

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
    )
