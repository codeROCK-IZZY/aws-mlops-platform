"""Alembic environment: uses the application's settings for the DB URL."""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from app.db.models import metadata

config = context.config
if config.config_file_name is not None and not config.attributes.get("skip_logging_config"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _url() -> str:
    url = config.attributes.get("database_url") or os.environ.get("MLP_DATABASE_URL")
    if not url:
        raise RuntimeError("MLP_DATABASE_URL is not set")
    return str(url)


def include_object(obj, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
    # Monthly partitions are created at runtime and are not part of the model.
    return not (type_ == "table" and name and name.startswith("sensor_readings_"))


def run_migrations_online() -> None:
    engine = create_engine(_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=metadata, include_object=include_object)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
