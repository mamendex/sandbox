"""Interface de carregamento das tabelas extraídas (parquet) como DataFrames pandas."""

from __future__ import annotations

import os
import time

import pandas as pd


class TableLoader:
    """Encapsula a leitura dos parquets gerados pelo extract, por tabela.

    Uso em notebook:
        loader = TableLoader(output_dir, schema="meu_schema")
        df = loader.load("accounts")
        df = loader["accounts"]  # atalho equivalente
    """

    def __init__(self, output_dir: str, schema: str):
        self.output_dir = output_dir
        self.schema = schema

    def _table_path(self, table: str) -> str:
        return os.path.join(self.output_dir, self.schema, table)

    def available_tables(self) -> list[str]:
        schema_dir = os.path.join(self.output_dir, self.schema)
        if not os.path.isdir(schema_dir):
            return []
        return sorted(
            name for name in os.listdir(schema_dir) if os.path.isdir(os.path.join(schema_dir, name))
        )

    def load(self, table: str, columns: list[str] | None = None, drop_bucket: bool = True) -> pd.DataFrame:
        """Carrega uma tabela extraída como DataFrame, lendo todos os parquets particionados."""
        path = self._table_path(table)
        if not os.path.isdir(path):
            raise FileNotFoundError(f"tabela '{table}' não encontrada em {path}")

        start = time.perf_counter()
        print(f"[TableLoader.load] lendo {self.schema}.{table} de {path}...", flush=True)
        df = pd.read_parquet(path, columns=columns, engine="pyarrow")
        if drop_bucket and "bucket" in df.columns:
            df = df.drop(columns=["bucket"])
        print(
            f"[TableLoader.load] {self.schema}.{table}: {len(df)} linhas "
            f"({time.perf_counter() - start:.1f}s)",
            flush=True,
        )
        return df

    def __getitem__(self, table: str) -> pd.DataFrame:
        return self.load(table)
