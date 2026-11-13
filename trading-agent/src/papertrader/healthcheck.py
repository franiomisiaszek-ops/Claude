"""Container health check: exit 0 if the app's heartbeat is recent, 1 otherwise.

Run as `python -m papertrader.healthcheck` (Docker does this every minute).
"""

from __future__ import annotations

import sys

from papertrader.config import get_settings
from papertrader.dashboard.data import load_status
from papertrader.db.session import make_engine


def main() -> int:
    engine = make_engine(get_settings().database_url, read_only=True)
    try:
        status = load_status(engine)
    except Exception as exc:
        print(f"unhealthy: database not reachable: {exc}")
        return 1
    finally:
        engine.dispose()
    if not status.app_alive:
        print(f"unhealthy: last heartbeat {status.heartbeat_age_seconds}s ago")
        return 1
    print("healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
