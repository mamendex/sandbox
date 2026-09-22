"""Interface de carregamento das tabelas extraídas (parquet) como DataFrames pandas."""

from __future__ import annotations

import os
import time

import pandas as pd

from i3_shift_lake.extract import DEFAULT_CONFIG_DIR, load_table_query


class TableLoader:
    """Encapsula a leitura dos parquets gerados pelo extract, por tabela.

    Cada página extraída vira um arquivo parquet novo (nunca sobrescrito) — então se um
    registro é modificado e reextraído numa carga incremental (mesmo id, `order_by` mais
    recente), as duas versões ficam fisicamente no dataset. Por padrão (`dedupe=True`),
    `load()` resolve isso mantendo só a versão mais recente de cada id, usando a mesma
    ordenação/coluna de particionamento que o `extract` persistiu em `config_dir`.

    Uso em notebook:
        loader = TableLoader(output_dir, schema="meu_schema")
        df = loader.load("accounts")
        df = loader["accounts"]  # atalho equivalente
    """

    def __init__(self, output_dir: str, schema: str, config_dir: str = DEFAULT_CONFIG_DIR):
        self.output_dir = output_dir
        self.schema = schema
        self.config_dir = config_dir

    def _table_path(self, table: str) -> str:
        return os.path.join(self.output_dir, self.schema, table)

    def available_tables(self) -> list[str]:
        schema_dir = os.path.join(self.output_dir, self.schema)
        if not os.path.isdir(schema_dir):
            return []
        return sorted(
            name for name in os.listdir(schema_dir) if os.path.isdir(os.path.join(schema_dir, name))
        )

    def load(
        self,
        table: str,
        columns: list[str] | None = None,
        drop_bucket: bool = True,
        dedupe: bool = True,
    ) -> pd.DataFrame:
        """Carrega uma tabela extraída como DataFrame, lendo todos os parquets particionados.

        Com `dedupe=True` (default): se um id foi modificado e reextraído, mantém só a
        versão mais recente (maior `order_by`) — a versão antiga do mesmo id nunca aparece
        junto. Passe `dedupe=False` para ver todas as versões cruas, como estão no parquet.
        """
        path = self._table_path(table)
        if not os.path.isdir(path):
            raise FileNotFoundError(f"tabela '{table}' não encontrada em {path}")

        order_by = partition_column = None
        if dedupe:
            try:
                table_cfg = load_table_query(self.schema, table, self.config_dir)
                order_by = table_cfg["order_by"]
                partition_column = table_cfg["partition_column"]
            except FileNotFoundError:
                print(
                    f"[TableLoader.load] AVISO: config de {self.schema}.{table} não encontrada em "
                    f"'{self.config_dir}' — carregando sem dedupe (pode haver versões repetidas de um id)",
                    flush=True,
                )
                dedupe = False

        read_columns = columns
        if dedupe and columns is not None:
            faltando = [c for c in order_by if c not in columns]
            if faltando:
                read_columns = list(columns) + faltando

        start = time.perf_counter()
        print(f"[TableLoader.load] lendo {self.schema}.{table} de {path}...", flush=True)
        df = pd.read_parquet(path, columns=read_columns, engine="pyarrow")
        if drop_bucket and "bucket" in df.columns:
            df = df.drop(columns=["bucket"])

        if dedupe:
            antes = len(df)
            df = df.sort_values(order_by).drop_duplicates(subset=[partition_column], keep="last")
            df = df.reset_index(drop=True)
            if columns is not None:
                df = df[list(columns)]
            if len(df) != antes:
                print(
                    f"[TableLoader.load] {self.schema}.{table}: {antes - len(df)} versão(ões) antiga(s) "
                    f"descartada(s) (id modificado e reextraído)",
                    flush=True,
                )

        print(
            f"[TableLoader.load] {self.schema}.{table}: {len(df)} linhas "
            f"({time.perf_counter() - start:.1f}s)",
            flush=True,
        )
        return df

    def __getitem__(self, table: str) -> pd.DataFrame:
        return self.load(table)
