"""Extração paginada de tabelas do Redshift para parquet particionado por bucket."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Callable

import pandas as pd

from redshift_etl.discover import DEFAULT_CONFIG_DIR, TableModel

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


def build_query_template(schema: str, table: str, columns: list[str], order_by: list[str]) -> str:
    """Monta a query de select da tabela, com placeholders `{page_size}`/`{offset}` para paginação."""
    cols_sql = ", ".join(f'"{c}"' for c in columns)
    order_sql = ", ".join(f'"{c}"' for c in order_by)
    return (
        f'SELECT {cols_sql} FROM "{schema}"."{table}" '
        f"ORDER BY {order_sql} "
        "LIMIT {page_size} OFFSET {offset}"
    )


def table_query_path(schema: str, table: str, config_dir: str = DEFAULT_CONFIG_DIR) -> str:
    return os.path.join(config_dir, schema, f"{table}.json")


def save_table_query(
    schema: str,
    table: str,
    columns: list[str],
    order_by: list[str],
    partition_column: str,
    query_template: str,
    config_dir: str = DEFAULT_CONFIG_DIR,
) -> str:
    """Persiste a query de select (e a estratégia de ordenação/particionamento) de uma tabela."""
    table_dir = os.path.join(config_dir, schema)
    os.makedirs(table_dir, exist_ok=True)
    path = table_query_path(schema, table, config_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "schema": schema,
                "table": table,
                "columns": columns,
                "order_by": order_by,
                "partition_column": partition_column,
                "select_query": query_template,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    return path


def load_table_query(schema: str, table: str, config_dir: str = DEFAULT_CONFIG_DIR) -> dict:
    """Carrega a query de select de uma tabela previamente salva por `extract_table`."""
    path = table_query_path(schema, table, config_dir)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


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
    config_dir: str = DEFAULT_CONFIG_DIR,
) -> dict:
    """Extrai uma tabela de forma paginada e grava parquet particionado por bucket.

    A query de select usada (com a ordenação/particionamento escolhidos) é
    persistida em `<config_dir>/<schema>/<table>.json`.
    """
    order_by, partition_col = resolve_key(columns)
    query_template = build_query_template(schema, table, columns, order_by)
    save_table_query(schema, table, columns, order_by, partition_col, query_template, config_dir)

    table_dir = os.path.join(output_dir, schema, table)
    os.makedirs(table_dir, exist_ok=True)

    rows_read = 0
    pages = 0
    offset = 0
    while offset < row_count:
        sql = query_template.format(page_size=page_size, offset=offset)
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
    config_dir: str = DEFAULT_CONFIG_DIR,
) -> pd.DataFrame:
    """Extrai todas as tabelas com ao menos uma linha e monta o relatório final.

    `model` pode vir de `discover()` (fluxo online) ou de `load_model()` (fluxo a
    partir da configuração salva em `config_dir`).
    """
    results = []
    for table in model.tables_with_rows():
        columns = model.tables[table]
        row_count = model.row_counts[table]
        try:
            stats = extract_table(
                query, model.schema, table, columns, row_count, output_dir,
                page_size, num_buckets, config_dir,
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
