#!/usr/bin/env python3
"""Materialize the pinned ReqSys SQLAlchemy Base metadata as a fresh SQLite schema."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    product_root = Path(args.product_root).resolve()
    backend = product_root / "backend"
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    os.environ["DATABASE_URL"] = f"sqlite:///{output}"
    os.environ.setdefault("JWT_SECRET", "ci-placeholder-secret-min-32-chars-long")
    os.environ.setdefault("JWT_ISSUER", "reqsys-ci")
    os.environ.setdefault("JWT_AUDIENCE", "reqsys-ci")
    os.environ.setdefault("ALLOW_DEMO_LOGIN", "true")
    sys.path.insert(0, str(backend))

    import app.main  # noqa: F401,E402
    from app.db import Base  # noqa: E402
    from sqlalchemy import create_engine  # noqa: E402

    engine = create_engine(f"sqlite:///{output}")
    try:
        Base.metadata.create_all(bind=engine)
    finally:
        engine.dispose()

    print(f"materialized_tables={len(Base.metadata.tables)} output={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
