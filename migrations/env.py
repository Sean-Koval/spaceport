"""Owner-only online migrations; URL comes from the process environment.

No credentials are stored in config or logs. Roles must be provisioned separately
by the operator; migration owners need no CREATE ROLE/database permission.
"""
import os

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

config = context.config


def configure(connection=None, url=None):
    context.configure(connection=connection, url=url, target_metadata=None,
        literal_binds=connection is None, transactional_ddl=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    configure(url="postgresql+psycopg://")
else:
    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        configure(connection=supplied_connection)
    else:
        raw = os.environ.get("GATEWAY_MIGRATION_DATABASE_URL")
        if not raw:
            raise RuntimeError("GATEWAY_MIGRATION_DATABASE_URL is required; no implicit database is allowed")
        url = make_url(raw)
        if url.drivername != "postgresql+psycopg":
            raise RuntimeError("Migrations require PostgreSQL with the psycopg driver")
        engine = create_engine(url, poolclass=NullPool, echo=False, hide_parameters=True,
            connect_args={"connect_timeout": 5})
        try:
            with engine.connect() as connection:
                configure(connection=connection)
        finally:
            engine.dispose()
