from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.orm import Session


@lru_cache(maxsize=1)
def expected_alembic_revision() -> str:
    project_root = Path(__file__).resolve().parents[2]
    config = Config(str(project_root / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)
    heads = scripts.get_heads()
    if len(heads) != 1:
        raise RuntimeError("EventLedger must have exactly one Alembic head")
    return heads[0]


def database_is_ready(session: Session) -> bool:
    session.execute(text("SELECT 1"))
    current_revision = session.scalar(
        text("SELECT version_num FROM alembic_version")
    )
    return current_revision == expected_alembic_revision()
