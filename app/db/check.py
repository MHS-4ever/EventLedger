import sys

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError


def main() -> int:
    try:
        from app.db.session import SessionLocal
    except ValidationError:
        print(
            "Database configuration is invalid. Ensure POSTGRES_DB, "
            "POSTGRES_USER, and POSTGRES_PASSWORD are set in .env.",
            file=sys.stderr,
        )
        return 2

    try:
        with SessionLocal() as session:
            database_name, user_name = session.execute(
                text("SELECT current_database(), current_user")
            ).one()
    except SQLAlchemyError as error:
        print(
            f"Database connection failed ({type(error).__name__}). "
            "Verify PostgreSQL is running and the .env settings are correct.",
            file=sys.stderr,
        )
        return 1

    print(f"Connected to database '{database_name}' as user '{user_name}'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
