"""Descoberta do modelo de um schema (tabelas, colunas e contagem de linhas).

Fala com a fonte de dados através de uma função `query(sql) -> pandas.DataFrame`
(ver contrato em `i3_shift_lake.query.QueryFn`) — Redshift, DuckDB ou qualquer
fonte que fale SQL padrão.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

import pandas as pd

from i3_shift_lake.query import QueryFn

DEFAULT_CONFIG_DIR = "config"


@dataclass
class TableModel:
    """Modelo descoberto de um schema: colunas (em ordem), tipos e contagem de linhas por tabela."""

    schema: str
    tables: dict[str, list[str]] = field(default_factory=dict)
    column_types: dict[str, dict[str, str]] = field(default_factory=dict)
    row_counts: dict[str, int] = field(default_factory=dict)

    def tables_with_rows(self) -> list[str]:
        """Tabelas com ao menos uma linha, na ordem em que foram descobertas."""
        return [t for t in self.tables if self.row_counts.get(t, 0) > 0]


def get_columns(query: QueryFn, schema: str) -> pd.DataFrame:
    """Busca tabelas, colunas (em ordem ordinal) e o tipo de dado de um schema."""
    sql = f"""
        SELECT table_name, column_name, data_type, ordinal_position
        FROM information_schema.columns
        WHERE table_schema = '{schema}'
        ORDER BY table_name, ordinal_position
    """
    return query(sql)


def get_row_counts(query: QueryFn, schema: str, tables: list[str]) -> dict[str, int]:
    """Conta as linhas de cada tabela do schema (uma query COUNT(*) por tabela)."""
    counts: dict[str, int] = {}
    for i, table in enumerate(tables, start=1):
        start = time.perf_counter()
        print(f"[get_row_counts] ({i}/{len(tables)}) contando {schema}.{table}...", flush=True)
        sql = f'SELECT COUNT(*) AS row_count FROM "{schema}"."{table}"'
        counts[table] = int(query(sql)["row_count"].iloc[0])
        print(
            f"[get_row_counts] {schema}.{table}: {counts[table]} linhas "
            f"({time.perf_counter() - start:.1f}s)",
            flush=True,
        )
    return counts


def model_path(schema: str, config_dir: str = DEFAULT_CONFIG_DIR) -> str:
    return os.path.join(config_dir, f"{schema}.json")


def save_model(model: TableModel, config_dir: str = DEFAULT_CONFIG_DIR) -> str:
    """Persiste o modelo descoberto (colunas, tipos e contagem de linhas) em `<config_dir>/<schema>.json`."""
    os.makedirs(config_dir, exist_ok=True)
    path = model_path(model.schema, config_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "schema": model.schema,
                "tables": model.tables,
                "column_types": model.column_types,
                "row_counts": model.row_counts,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    return path


def load_model(schema: str, config_dir: str = DEFAULT_CONFIG_DIR) -> TableModel:
    """Carrega um modelo previamente salvo por `discover`/`save_model`, sem acessar o Redshift."""
    path = model_path(schema, config_dir)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return TableModel(
        schema=data["schema"],
        tables=data["tables"],
        column_types=data.get("column_types", {}),
        row_counts=data["row_counts"],
    )


def discover(query: QueryFn, schema: str, config_dir: str = DEFAULT_CONFIG_DIR) -> TableModel:
    """Descobre o modelo completo (colunas, tipos e contagem de linhas) de um schema e persiste em JSON."""
    start = time.perf_counter()
    print(f"[discover] iniciando descoberta do schema {schema}...", flush=True)

    columns_df = get_columns(query, schema)

    tables: dict[str, list[str]] = {}
    column_types: dict[str, dict[str, str]] = {}
    for table, group in columns_df.groupby("table_name", sort=False):
        group = group.sort_values("ordinal_position")
        tables[table] = group["column_name"].tolist()
        column_types[table] = dict(zip(group["column_name"], group["data_type"]))
    print(f"[discover] {len(tables)} tabelas encontradas em {schema}, contando linhas...", flush=True)

    row_counts = get_row_counts(query, schema, list(tables.keys()))

    model = TableModel(schema=schema, tables=tables, column_types=column_types, row_counts=row_counts)
    save_model(model, config_dir)
    print(f"[discover] concluido em {time.perf_counter() - start:.1f}s", flush=True)
    return model
