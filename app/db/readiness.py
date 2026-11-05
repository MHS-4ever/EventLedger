from sqlalchemy.orm import Session

from app.db.compatibility import check_schema_compatibility


def database_is_ready(session: Session) -> bool:
    return check_schema_compatibility(session).compatible
