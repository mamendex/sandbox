"""Extração paginada de tabelas do Redshift para parquet particionado por bucket."""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Callable

import pandas as pd

from .discover import DEFAULT_CONFIG_DIR, TableModel

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
    sample_size: int | None = None,
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
                "sample_size": sample_size,
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
    sample_size: int | None = None,
) -> dict:
    """Extrai uma tabela de forma paginada e grava parquet particionado por bucket.

    Com `sample_size`, extrai só as primeiras `sample_size` linhas (na mesma
    ordenação usada para a extração completa) em vez da tabela inteira — útil
    para testes/dev sem ler a base toda.

    A query de select usada (com a ordenação/particionamento escolhidos) é
    persistida em `<config_dir>/<schema>/<table>.json`.
    """
    order_by, partition_col = resolve_key(columns)
    query_template = build_query_template(schema, table, columns, order_by)
    save_table_query(
        schema, table, columns, order_by, partition_col, query_template, config_dir, sample_size
    )

    table_dir = os.path.join(output_dir, schema, table)
    os.makedirs(table_dir, exist_ok=True)

    target_rows = row_count if sample_size is None else min(row_count, sample_size)

    start = time.perf_counter()
    print(f"[extract_table] iniciando {schema}.{table}: {target_rows} linhas alvo, page_size={page_size}", flush=True)

    rows_read = 0
    pages = 0
    offset = 0
    while offset < target_rows:
        current_page_size = min(page_size, target_rows - offset)
        sql = query_template.format(page_size=current_page_size, offset=offset)
        page_df = query(sql)
        if page_df.empty:
            break
        _write_page(page_df, table_dir, partition_col, num_buckets)
        rows_read += len(page_df)
        pages += 1
        offset += current_page_size
        print(
            f"[extract_table] {schema}.{table}: pagina {pages} lida, "
            f"{rows_read}/{target_rows} linhas ({time.perf_counter() - start:.1f}s)",
            flush=True,
        )

    print(
        f"[extract_table] {schema}.{table} concluida: {rows_read} linhas em {pages} paginas "
        f"({time.perf_counter() - start:.1f}s)",
        flush=True,
    )

    return {
        "table_name": table,
        "status": "ok",
        "rows_read": rows_read,
        "sample_size": sample_size,
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
    sample_size: int | None = None,
) -> pd.DataFrame:
    """Extrai todas as tabelas com ao menos uma linha e monta o relatório final.

    `model` pode vir de `discover()` (fluxo online) ou de `load_model()` (fluxo a
    partir da configuração salva em `config_dir`).

    Com `sample_size`, cada tabela é limitada às suas primeiras `sample_size`
    linhas em vez de extraída por completo (útil para testes/dev).
    """
    tables = model.tables_with_rows()
    start = time.perf_counter()
    print(f"[extract_all] iniciando extracao de {len(tables)} tabelas do schema {model.schema}", flush=True)

    results = []
    for i, table in enumerate(tables, start=1):
        columns = model.tables[table]
        row_count = model.row_counts[table]
        print(f"[extract_all] tabela {i}/{len(tables)}: {table}", flush=True)
        try:
            stats = extract_table(
                query, model.schema, table, columns, row_count, output_dir,
                page_size, num_buckets, config_dir, sample_size,
            )
        except ValueError as exc:
            stats = {
                "table_name": table,
                "status": f"erro: {exc}",
                "rows_read": 0,
                "sample_size": sample_size,
                "pages": 0,
                "order_by": None,
                "partition_column": None,
                "output_path": None,
            }
        results.append(stats)

    print(f"[extract_all] concluido em {time.perf_counter() - start:.1f}s", flush=True)

    return pd.DataFrame(
        results,
        columns=[
            "table_name", "status", "rows_read", "sample_size",
            "pages", "order_by", "partition_column", "output_path",
        ],
    )
