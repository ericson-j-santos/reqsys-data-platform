from __future__ import annotations

import os
import unittest

import psycopg


def _column_contract(pg: psycopg.Connection, schema: str) -> set[tuple]:
    rows = pg.execute(
        """
        SELECT table_name, column_name, data_type, character_maximum_length,
               CASE WHEN data_type = 'numeric' THEN numeric_precision ELSE NULL END,
               CASE WHEN data_type = 'numeric' THEN numeric_scale ELSE NULL END,
               is_nullable,
               CASE
                 WHEN column_default LIKE 'nextval(%' THEN 'sequence'
                 WHEN upper(coalesce(column_default, '')) IN ('CURRENT_TIMESTAMP', 'NOW()') THEN 'current_timestamp'
                 WHEN column_default IS NULL THEN 'none'
                 ELSE 'literal'
               END AS default_kind
        FROM information_schema.columns
        WHERE table_schema = %s
        ORDER BY table_name, ordinal_position
        """,
        (schema,),
    ).fetchall()
    return {tuple(row) for row in rows if row[0] != "_migration_runs"}


def _primary_keys(pg: psycopg.Connection, schema: str) -> set[tuple]:
    rows = pg.execute(
        """
        SELECT t.relname,
               array_agg(a.attname ORDER BY k.ord)::text
        FROM pg_index i
        JOIN pg_class t ON t.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
        WHERE n.nspname = %s AND i.indisprimary
        GROUP BY t.relname
        """,
        (schema,),
    ).fetchall()
    return {tuple(row) for row in rows}


def _indexes(pg: psycopg.Connection, schema: str) -> set[tuple]:
    rows = pg.execute(
        """
        SELECT t.relname,
               i.indisunique,
               array_agg(a.attname ORDER BY k.ord)::text
        FROM pg_index i
        JOIN pg_class t ON t.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
        WHERE n.nspname = %s AND NOT i.indisprimary
        GROUP BY t.relname, i.indexrelid, i.indisunique
        """,
        (schema,),
    ).fetchall()
    return {tuple(row) for row in rows}


def _foreign_keys(pg: psycopg.Connection, schema: str) -> set[tuple]:
    rows = pg.execute(
        """
        SELECT src.relname,
               array_agg(sa.attname ORDER BY s.ord)::text,
               dst.relname,
               array_agg(da.attname ORDER BY s.ord)::text,
               c.confupdtype,
               c.confdeltype
        FROM pg_constraint c
        JOIN pg_class src ON src.oid = c.conrelid
        JOIN pg_class dst ON dst.oid = c.confrelid
        JOIN pg_namespace n ON n.oid = c.connamespace
        JOIN unnest(c.conkey, c.confkey) WITH ORDINALITY AS s(src_attnum, dst_attnum, ord) ON TRUE
        JOIN pg_attribute sa ON sa.attrelid = src.oid AND sa.attnum = s.src_attnum
        JOIN pg_attribute da ON da.attrelid = dst.oid AND da.attnum = s.dst_attnum
        WHERE n.nspname = %s AND c.contype = 'f'
        GROUP BY c.oid, src.relname, dst.relname, c.confupdtype, c.confdeltype
        """,
        (schema,),
    ).fetchall()
    return {tuple(row) for row in rows}


class ReqSysProductPostgresParityE2E(unittest.TestCase):
    def test_migrator_schema_matches_product_postgres_semantics(self) -> None:
        dsn = os.environ["POSTGRES_DSN"]
        actual = os.environ["REQSYS_MIGRATED_SCHEMA"]
        expected = os.environ["REQSYS_EXPECTED_SCHEMA"]
        with psycopg.connect(dsn) as pg:
            self.assertEqual(_column_contract(pg, actual), _column_contract(pg, expected))
            self.assertEqual(_primary_keys(pg, actual), _primary_keys(pg, expected))
            self.assertEqual(_indexes(pg, actual), _indexes(pg, expected))
            self.assertEqual(_foreign_keys(pg, actual), _foreign_keys(pg, expected))


if __name__ == "__main__":
    unittest.main()
