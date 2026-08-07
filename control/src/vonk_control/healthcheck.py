"""Container-local readiness check."""

from sqlalchemy import text

from .db import build_engine
from .settings import Settings


def main() -> None:
    settings = Settings.from_env_and_secrets()
    with build_engine(settings.database_url).connect() as connection:
        connection.execute(text("SELECT 1"))


if __name__ == "__main__":
    main()
