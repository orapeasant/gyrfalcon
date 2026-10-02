import os

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import make_url

from gyrfalcon.db.schema import metadata

config = context.config
target_metadata = metadata


def database_url() -> str:
    url = os.environ.get("GYRFALCON_DB_DSN") or config.get_main_option("sqlalchemy.url")
    parsed = make_url(url)
    if parsed.drivername in {"postgres", "postgresql"}:
        parsed = parsed.set(drivername="postgresql+psycopg")
    return parsed.render_as_string(hide_password=False)


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is None:
        connectable = create_engine(database_url(), poolclass=pool.NullPool)
        with connectable.connect() as connection:
            _run(connection)
        return
    _run(connection)


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
