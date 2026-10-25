"""Request schema and one Cloud SQL connection pool per worker process."""

import atexit
import os
from threading import Lock

from sqlalchemy import (
    Boolean, Column, DateTime, Integer, MetaData, String, Table, Text,
    create_engine,
)

metadata = MetaData()
requests = Table(
    "requests", metadata,
    Column("id", String(36), primary_key=True),
    Column("country", Text),
    Column("client_ip", Text),
    Column("gender", Text),
    # The supplied client sends age and income group labels, not numbers.
    Column("age", Text),
    Column("income", Text),
    # X-time has no timezone. Preserve it without inventing a UTC interpretation.
    Column("client_time", Text),
    Column("is_banned", Boolean, nullable=False),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("requested_file", Text),
    Column("status_code", Integer, nullable=False),
    Column("experiment_id", Text, nullable=False, index=True),
)
failed_requests = Table(
    "failed_requests", metadata,
    Column("id", String(36), primary_key=True),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("requested_file", Text),
    Column("error_code", Integer, nullable=False),
)

_engine = None
_connector = None
_lock = Lock()


def get_engine():
    """Initialize lazily after Gunicorn forks; serialize concurrent first use."""
    global _engine, _connector
    with _lock:
        if _engine is not None:
            return _engine
        from google.cloud.sql.connector import Connector, IPTypes

        instance = os.environ["INSTANCE_CONNECTION_NAME"]
        user = os.environ["DB_USER"]
        password = os.environ["DB_PASS"]
        database = os.environ["DB_NAME"]
        ip_type = IPTypes[os.environ.get("DB_IP_TYPE", "PUBLIC").upper()]
        connector = Connector(refresh_strategy="LAZY")

        def connect():
            connection = connector.connect(
                instance, "pg8000", user=user, password=password, db=database,
                ip_type=ip_type, timeout=30,
            )
            try:
                cursor = connection.cursor()
                cursor.execute("SET TIME ZONE 'UTC'")
                cursor.execute("SET statement_timeout = '30s'")
                connection.commit()
                cursor.close()
            except Exception:
                connection.close()
                raise
            return connection

        try:
            engine = create_engine(
                "postgresql+pg8000://", creator=connect,
                pool_size=int(os.environ.get("DB_POOL_SIZE", "5")),
                max_overflow=int(os.environ.get("DB_MAX_OVERFLOW", "5")),
                pool_timeout=30, pool_recycle=1800, pool_pre_ping=True,
            )
        except Exception:
            connector.close()
            raise
        _connector, _engine = connector, engine
        return _engine


def close_database():
    global _engine, _connector
    with _lock:
        if _engine is not None:
            _engine.dispose()
            _engine = None
        if _connector is not None:
            _connector.close()
            _connector = None


atexit.register(close_database)


def create_schema(engine=None):
    """Create missing tables/indexes without dropping or clearing existing data."""
    with (engine if engine is not None else get_engine()).begin() as connection:
        # Serialize repeated setup invocations on PostgreSQL.
        if connection.dialect.name == "postgresql":
            from sqlalchemy import text
            connection.execute(text("SELECT pg_advisory_xact_lock(528005)"))
        metadata.create_all(connection, checkfirst=True)


def record_request(record, engine=None):
    """Commit both rows together. Never retry a potentially committed transaction."""
    with (engine if engine is not None else get_engine()).begin() as connection:
        connection.execute(requests.insert().values(**record))
        if record["status_code"] != 200:
            connection.execute(failed_requests.insert().values(
                id=record["id"], requested_at=record["requested_at"],
                requested_file=record["requested_file"],
                error_code=record["status_code"],
            ))
