"""The freeze rule: nothing system A depends on may change during the experiment.

A runs from a Docker image built at the freeze commit, with config/a.yaml. These tests
fail if a change would make the shared database or A's settings differ from what that
image expects. If one fails, do not "fix" the test: put the change in B or C instead.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from papertrader.config import load_config
from papertrader.db import models  # noqa: F401  (registers the tables)
from papertrader.db.base import Base
from papertrader.research.reuse import FROZEN_A_FINGERPRINT, agents_fingerprint
from papertrader.systems import FROZEN_REVISION

from ..conftest import PROJECT_ROOT

HERE = Path(__file__).parent
FROZEN_A_CONFIG = "4292847a1d870775"  # sha256 of config/a.yaml at the start of the experiment


def test_no_new_migrations() -> None:
    config = Config()
    config.set_main_option("script_location", str(PROJECT_ROOT / "src/papertrader/db/migrations"))
    assert ScriptDirectory.from_config(config).get_heads() == [FROZEN_REVISION]


def test_tables_and_columns_unchanged() -> None:
    frozen = json.loads((HERE / "frozen_schema.json").read_text())
    now = {
        name: {c.name: str(c.type) for c in table.columns}
        for name, table in sorted(Base.metadata.tables.items())
    }
    assert now == frozen, "the shared tables changed; new tables for B or C need their own schema"


def test_a_config_unchanged() -> None:
    path = PROJECT_ROOT / "config" / "a.yaml"
    assert hashlib.sha256(path.read_bytes()).hexdigest()[:16] == FROZEN_A_CONFIG
    config = load_config(path)
    assert not config.telegram.enabled  # the hub sends A's messages
    assert agents_fingerprint(config) == FROZEN_A_FINGERPRINT
