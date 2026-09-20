"""Alembic environment. Kept minimal: online migrations only, URL from the
same Settings the application uses."""

from alembic import context
from sqlalchemy import engine_from_config, pool

from gateway.config import Settings
from gateway.models.ledger import Base

config = context.config

# Prefer an explicitly set URL (tests set one), otherwise the app settings.
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", Settings().database_url)

target_metadata = Base.metadata


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
