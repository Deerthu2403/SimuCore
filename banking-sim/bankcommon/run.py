"""Process entry point:  python -m bankcommon.run <service>"""

from __future__ import annotations

import importlib
import logging
import os
import sys

import uvicorn

from bankcommon.config import Settings, registry


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in registry().services:
        sys.exit(f"usage: python -m bankcommon.run <{'|'.join(registry().services)}>")
    name = sys.argv[1]
    logging.basicConfig(level="INFO", format=f"%(asctime)s %(levelname)s {name} %(message)s")
    logging.getLogger("httpx").setLevel("WARNING")
    app = importlib.import_module(f"services.{name}.app").create_app(Settings.from_env())
    server = uvicorn.Server(uvicorn.Config(
        app, host=os.environ.get("BANK_BIND", "127.0.0.1"),
        port=registry().services[name]["port"], access_log=False, log_level="warning",
        timeout_graceful_shutdown=5))

    def stop() -> None:
        server.should_exit = True

    app.state.ctx.shutdown = stop
    server.run()


if __name__ == "__main__":
    main()
