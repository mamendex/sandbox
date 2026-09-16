"""Extração paginada de tabelas do Redshift para parquet particionado por bucket."""

from __future__ import annotations

import hashlib
import os
from typing import Callable

import pandas as pd

from redshift_etl.discover import TableModel

QueryFn = Callable[[str], pd.DataFrame]

# Ordem de prioridade (coluna de data, coluna de id) usada para ORDER BY e paginação.
_ORDER_PRIORITY = [
    ("date_modified", "id"),
    ("date_created", "id"),
    ("date_modified", "id_c"),
    ("date_created", "id_c"),
]


def resolve_key(columns: list[str]) -> tuple[list[str], str]:
    """Escolhe as colunas de ORDER BY e a coluna de particionamento (id ou id_c).

    Prioridade: date_modified+id, date_created+id, date_modified+id_c,
    date_created+id_c, id, id_c.
    """
    cols = set(columns)
    for date_col, id_col in _ORDER_PRIORITY:
        if date_col in cols and id_col in cols:
            return [date_col, id_col], id_col
    if "id" in cols:
        return ["id"], "id"
    if "id_c" in cols:
        return ["id_c"], "id_c"
    raise ValueError("nenhuma coluna 'id'/'id_c' encontrada para ordenar e particionar")


def build_page_query(
    schema: str, table: str, columns: list[str], order_by: list[str], page_size: int, offset: int
) -> str:
    cols_sql = ", ".join(f'"{c}"' for c in columns)
    order_sql = ", ".join(f'"{c}"' for c in order_by)
    return (
        f'SELECT {cols_sql} FROM "{schema}"."{table}" '
        f"ORDER BY {order_sql} "
        f"LIMIT {page_size} OFFSET {offset}"
    )


def bucket_for(value, num_buckets: int) -> int:
    """Bucket estável (independe de PYTHONHASHSEED) para particionar o parquet."""
    digest = hashlib.md5(str(value).encode("utf-8")).hexdigest()
    return int(digest, 16) % num_buckets


def _write_page(df: pd.DataFrame, table_dir: str, partition_col: str, num_buckets: int) -> None:
    df = df.copy()
    df["bucket"] = df[partition_col].map(lambda v: bucket_for(v, num_buckets))
    df.to_parquet(table_dir, engine="pyarrow", partition_cols=["bucket"], index=False)


def extract_table(
    query: QueryFn,
    schema: str,
    table: str,
    columns: list[str],
    row_count: int,
    output_dir: str,
    page_size: int = 50_000,
    num_buckets: int = 32,
) -> dict:
    """Extrai uma tabela de forma paginada e grava parquet particionado por bucket."""
    order_by, partition_col = resolve_key(columns)

    table_dir = os.path.join(output_dir, schema, table)
    os.makedirs(table_dir, exist_ok=True)

    rows_read = 0
    pages = 0
    offset = 0
    while offset < row_count:
        sql = build_page_query(schema, table, columns, order_by, page_size, offset)
        page_df = query(sql)
        if page_df.empty:
            break
        _write_page(page_df, table_dir, partition_col, num_buckets)
        rows_read += len(page_df)
        pages += 1
        offset += page_size

    return {
        "table_name": table,
        "status": "ok",
        "rows_read": rows_read,
        "pages": pages,
        "order_by": ", ".join(order_by),
        "partition_column": partition_col,
        "output_path": table_dir,
    }


def extract_all(
    query: QueryFn,
    model: TableModel,
    output_dir: str,
    page_size: int = 50_000,
    num_buckets: int = 32,
) -> pd.DataFrame:
    """Extrai todas as tabelas com ao menos uma linha e monta o relatório final."""
    results = []
    for table in model.tables_with_rows():
        columns = model.tables[table]
        row_count = model.row_counts[table]
        try:
            stats = extract_table(
                query, model.schema, table, columns, row_count, output_dir, page_size, num_buckets
            )
        except ValueError as exc:
            stats = {
                "table_name": table,
                "status": f"erro: {exc}",
                "rows_read": 0,
                "pages": 0,
                "order_by": None,
                "partition_column": None,
                "output_path": None,
            }
        results.append(stats)

    return pd.DataFrame(
        results,
        columns=["table_name", "status", "rows_read", "pages", "order_by", "partition_column", "output_path"],
    )
