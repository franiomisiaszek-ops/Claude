"""Docker health check: healthy while this system's heartbeat in the database is fresh.

Run as `python -m papertrader.healthcheck`. Exit code 0 = healthy, 1 = unhealthy.
"""

from __future__ import annotations

import sys
from datetime import datetime

from sqlalchemy.orm import Session

from papertrader.config import get_settings
from papertrader.db.provision import SchemaNotReadyError, verify
from papertrader.db.session import make_system_engine
from papertrader.db.state import HEARTBEAT_KEY, get_state
from papertrader.db.types import utcnow
from papertrader.systems import check_system

STALE_SECONDS = 180


def main() -> int:
    settings = get_settings()
    system = check_system(settings.system)
    engine = make_system_engine(settings.database_url, system, read_only=True)
    try:
        if system != "A":
            verify(engine, system)
        with Session(engine) as session:
            heartbeat = get_state(session, HEARTBEAT_KEY)
    except SchemaNotReadyError as exc:
        print(f"unhealthy: {exc}")
        return 1
    except Exception as exc:
        print(f"unhealthy: database not reachable: {exc}")
        return 1
    finally:
        engine.dispose()
    if not heartbeat:
        print("unhealthy: no heartbeat yet")
        return 1
    age = (utcnow() - datetime.fromisoformat(heartbeat["at"])).total_seconds()
    if age > STALE_SECONDS:
        print(f"unhealthy: last heartbeat {age:.0f}s ago")
        return 1
    print(f"healthy ({system})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
