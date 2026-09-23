#!/usr/bin/env python3
"""Materialize the pinned ReqSys Base.metadata directly in PostgreSQL for parity checks."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

SAFE_SCHEMA = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product-root", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--postgres-dsn-env", default="POSTGRES_DSN")
    args = parser.parse_args()

    if not SAFE_SCHEMA.fullmatch(args.schema):
        raise ValueError(f"invalid PostgreSQL schema: {args.schema!r}")
    dsn = os.environ.get(args.postgres_dsn_env, "")
    if not dsn:
        raise ValueError(f"missing environment variable: {args.postgres_dsn_env}")

    product_root = Path(args.product_root).resolve()
    backend = product_root / "backend"

    # Keep product import isolated from the PostgreSQL test database. The explicit
    # engine below is the only connection used to materialize the expected schema.
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ.setdefault("JWT_SECRET", "ci-placeholder-secret-min-32-chars-long")
    os.environ.setdefault("JWT_ISSUER", "reqsys-ci")
    os.environ.setdefault("JWT_AUDIENCE", "reqsys-ci")
    os.environ.setdefault("ALLOW_DEMO_LOGIN", "true")
    sys.path.insert(0, str(backend))

    import app.main  # noqa: F401,E402
    from app.db import Base  # noqa: E402
    from sqlalchemy import create_engine, text  # noqa: E402

    sqlalchemy_dsn = dsn.replace("postgresql://", "postgresql+psycopg2://", 1)
    engine = create_engine(sqlalchemy_dsn)
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{args.schema}"'))
        translated = engine.execution_options(schema_translate_map={None: args.schema})
        Base.metadata.create_all(bind=translated)
    finally:
        engine.dispose()

    print(f"postgres_materialized_tables={len(Base.metadata.tables)} schema={args.schema}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
