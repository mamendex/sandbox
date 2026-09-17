"""Descoberta do modelo de um schema no Redshift (tabelas, colunas e contagem de linhas).

Assume a existência de uma função `query(sql) -> pandas.DataFrame` para falar com o
Redshift (ver README do pacote).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

QueryFn = Callable[[str], pd.DataFrame]

DEFAULT_CONFIG_DIR = "config"


@dataclass
class TableModel:
    """Modelo descoberto de um schema: colunas (em ordem) e contagem de linhas por tabela."""

    schema: str
    tables: dict[str, list[str]] = field(default_factory=dict)
    row_counts: dict[str, int] = field(default_factory=dict)

    def tables_with_rows(self) -> list[str]:
        """Tabelas com ao menos uma linha, na ordem em que foram descobertas."""
        return [t for t in self.tables if self.row_counts.get(t, 0) > 0]


def get_columns(query: QueryFn, schema: str) -> pd.DataFrame:
    """Busca tabelas e colunas (em ordem ordinal) de um schema."""
    sql = f"""
        SELECT table_name, column_name, ordinal_position
        FROM information_schema.columns
        WHERE table_schema = '{schema}'
        ORDER BY table_name, ordinal_position
    """
    return query(sql)


def get_row_counts(query: QueryFn, schema: str, tables: list[str]) -> dict[str, int]:
    """Conta as linhas de cada tabela do schema (uma query COUNT(*) por tabela)."""
    counts: dict[str, int] = {}
    for table in tables:
        sql = f'SELECT COUNT(*) AS row_count FROM "{schema}"."{table}"'
        counts[table] = int(query(sql)["row_count"].iloc[0])
    return counts


def model_path(schema: str, config_dir: str = DEFAULT_CONFIG_DIR) -> str:
    return os.path.join(config_dir, f"{schema}.json")


def save_model(model: TableModel, config_dir: str = DEFAULT_CONFIG_DIR) -> str:
    """Persiste o modelo descoberto (colunas + contagem de linhas) em `<config_dir>/<schema>.json`."""
    os.makedirs(config_dir, exist_ok=True)
    path = model_path(model.schema, config_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"schema": model.schema, "tables": model.tables, "row_counts": model.row_counts},
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
    return TableModel(schema=data["schema"], tables=data["tables"], row_counts=data["row_counts"])


def discover(query: QueryFn, schema: str, config_dir: str = DEFAULT_CONFIG_DIR) -> TableModel:
    """Descobre o modelo completo (colunas + contagem de linhas) de um schema e persiste em JSON."""
    columns_df = get_columns(query, schema)

    tables: dict[str, list[str]] = {}
    for table, group in columns_df.groupby("table_name", sort=False):
        tables[table] = group.sort_values("ordinal_position")["column_name"].tolist()

    row_counts = get_row_counts(query, schema, list(tables.keys()))

    model = TableModel(schema=schema, tables=tables, row_counts=row_counts)
    save_model(model, config_dir)
    return model
