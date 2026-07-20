"""Day 1 environment sanity check.

Print the active Python version, the project name read from pyproject.toml,
and the current local time. Run with::

    uv run python src/hello.py
"""

from __future__ import annotations

import sys
import tomllib
from datetime import datetime
from pathlib import Path

# pyproject.toml sits one level above this file (src/hello.py -> project root).
_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def project_name(pyproject: Path = _PYPROJECT) -> str:
    """Return the ``[project].name`` field declared in pyproject.toml."""
    with pyproject.open("rb") as fh:
        data = tomllib.load(fh)
    name = data.get("project", {}).get("name")
    if not isinstance(name, str) or not name:
        msg = f"project.name missing or empty in {pyproject}"
        raise ValueError(msg)
    return name


def main() -> None:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    now = datetime.now().astimezone()

    print(f"Python version : {version}")
    print(f"Project name   : {project_name()}")
    print(f"Current time   : {now.isoformat(timespec='seconds')}")


if __name__ == "__main__":
    main()
