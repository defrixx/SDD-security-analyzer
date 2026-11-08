"""Local application lifecycle commands. Never execute catalog instructions."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .i18n import translate


def main():
    parser = argparse.ArgumentParser(prog="spec-enricher")
    parser.add_argument(
        "command",
        choices=[
            "serve",
            "session-bootstrap",
            "bootstrap-url",
            "healthcheck",
            "backup",
            "restore",
            "maintenance-off",
        ],
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--replace-existing", action="store_true")
    args = parser.parse_args()
    data_dir = Path(os.environ.get("APP_DATA_DIR", ".local-data"))
    origin = os.environ.get("APP_ALLOWED_ORIGIN", "http://127.0.0.1:8080")
    from .installation import Lease, active_data

    if args.command in {"backup", "restore", "maintenance-off"}:
        import json

        from .backup import create_backup, maintenance, restore_backup
        from .storage import Store

        root = Path(os.environ.get("APP_BACKUP_DIR", "/backups"))
        try:
            if args.command == "backup":
                if args.output is None:
                    parser.error(translate("backup требует --output внутри APP_BACKUP_DIR"))
                result = create_backup(data_dir, args.output, root)
            elif args.command == "restore":
                if args.input is None:
                    parser.error(translate("restore требует --input внутри APP_BACKUP_DIR"))
                result = restore_backup(
                    data_dir, args.input, root, replace_existing=args.replace_existing
                )
            else:
                with Lease(data_dir, ".maintenance.lock", exclusive=True):
                    store = Store(active_data(data_dir) / "app.sqlite3")
                    try:
                        maintenance(store, False)
                    finally:
                        store.close()
                result = {"maintenance": False}
        except (ValueError, OSError) as exc:
            raise SystemExit("Lifecycle operation rejected: " + str(exc)) from None
        print(json.dumps(result, ensure_ascii=False))
        return
    if args.command in {"session-bootstrap", "bootstrap-url"}:
        from .security import create_bootstrap

        token = create_bootstrap(active_data(data_dir))
        print(origin.rstrip("/") + "/#bootstrap=" + token)
    elif args.command == "healthcheck":
        import json
        import urllib.request

        with urllib.request.urlopen(origin + "/health", timeout=3) as response:
            if response.status != 200 or json.load(response).get("status") != "ok":
                raise SystemExit(1)
    else:
        import uvicorn

        from .api import create_app

        host = os.environ.get("APP_HOST", "127.0.0.1")
        if host not in {"127.0.0.1", "0.0.0.0"}:
            parser.error(translate("APP_HOST должен быть 127.0.0.1 либо container bind 0.0.0.0"))
        if host == "0.0.0.0" and not Path("/.dockerenv").exists():
            parser.error(translate("Нативный запуск разрешён только на loopback"))
        app = create_app(data_dir, origin)
        print(
            translate(
                "Для локального входа: python -m spec_enricher.cli bootstrap-url (в контейнере: docker compose exec app …)."
            )
        )
        uvicorn.run(
            app,
            host=host,
            port=int(os.environ.get("APP_PORT", "8080")),
            access_log=False,
            proxy_headers=False,
        )


if __name__ == "__main__":
    main()
