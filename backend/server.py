"""Runnable entrypoint: ``python -m backend.server`` (defaults to 127.0.0.1:8055)."""
from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("TRADING_DESK_HOST", "127.0.0.1")
    port = int(os.environ.get("TRADING_DESK_PORT", "8055"))
    uvicorn.run("backend.app:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
