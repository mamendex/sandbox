"""Verificações de qualidade da carga.

Cobre o básico (linhas carregadas x rowcount do discover, duplicidade da
chave de particionamento nos parquets) e deixa um mecanismo simples para
plugar checks customizados (chaves estrangeiras, chaves naturais únicas
como cpf/cnpj, etc.).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable

import pandas as pd
import pyarrow.dataset as ds

from redshift_etl.discover import TableModel
from redshift_etl.extract import DEFAULT_CONFIG_DIR, load_table_query


def _table_path(output_dir: str, schema: str, table: str) -> str:
    return os.path.join(output_dir, schema, table)


def count_loaded_rows(output_dir: str, schema: str, table: str) -> int:
    """Conta as linhas de uma tabela extraída lendo só metadados do parquet (sem carregar dados)."""
    path = _table_path(output_dir, schema, table)
    if not os.path.isdir(path):
        raise FileNotFoundError(f"tabela '{table}' não encontrada em {path}")
    return ds.dataset(path, format="parquet").count_rows()


def check_row_counts(model: TableModel, output_dir: str) -> pd.DataFrame:
    """Compara linhas carregadas (parquet) x linhas esperadas (contagem do discover), por tabela."""
    tables = model.tables_with_rows()
    start = time.perf_counter()
    print(f"[check_row_counts] verificando {len(tables)} tabelas do schema {model.schema}", flush=True)

    rows = []
    for table in tables:
        expected = model.row_counts[table]
        try:
            loaded = count_loaded_rows(output_dir, model.schema, table)
            status = "ok" if loaded == expected else "divergente"
        except FileNotFoundError:
            loaded = 0
            status = "nao_extraida"
        rows.append(
            {
                "table_name": table,
                "expected_rows": expected,
                "loaded_rows": loaded,
                "diff": loaded - expected,
                "status": status,
            }
        )

    print(f"[check_row_counts] concluido em {time.perf_counter() - start:.1f}s", flush=True)
    return pd.DataFrame(rows)


def check_duplicate_ids(
    output_dir: str, schema: str, table: str, config_dir: str = DEFAULT_CONFIG_DIR
) -> pd.DataFrame:
    """Verifica duplicidade da chave de particionamento (id/id_c) nos parquets de uma tabela.

    A coluna verificada é a mesma usada para particionar/ordenar na extração
    (persistida em `<config_dir>/<schema>/<table>.json`). Retorna um DataFrame
    com os valores duplicados e quantas vezes aparecem (vazio se não há duplicatas).
    """
    id_column = load_table_query(schema, table, config_dir)["partition_column"]

    path = _table_path(output_dir, schema, table)
    if not os.path.isdir(path):
        raise FileNotFoundError(f"tabela '{table}' não encontrada em {path}")

    start = time.perf_counter()
    print(f"[check_duplicate_ids] varrendo {schema}.{table} pela coluna '{id_column}'...", flush=True)

    ids = ds.dataset(path, format="parquet").to_table(columns=[id_column]).column(id_column).to_pandas()
    counts = ids.value_counts()
    duplicated = counts[counts > 1].reset_index()
    duplicated.columns = [id_column, "occurrences"]

    print(
        f"[check_duplicate_ids] {schema}.{table}: {len(duplicated)} ids duplicados "
        f"({time.perf_counter() - start:.1f}s)",
        flush=True,
    )
    return duplicated


def check_duplicates_all(
    model: TableModel, output_dir: str, config_dir: str = DEFAULT_CONFIG_DIR
) -> pd.DataFrame:
    """Roda `check_duplicate_ids` em todas as tabelas extraídas e monta um resumo."""
    tables = model.tables_with_rows()
    rows = []
    for table in tables:
        try:
            dup_df = check_duplicate_ids(output_dir, model.schema, table, config_dir)
            rows.append(
                {
                    "table_name": table,
                    "duplicated_ids": len(dup_df),
                    "duplicated_rows": int(dup_df["occurrences"].sum()) if not dup_df.empty else 0,
                    "status": "ok" if dup_df.empty else "duplicatas_encontradas",
                }
            )
        except FileNotFoundError as exc:
            rows.append(
                {"table_name": table, "duplicated_ids": None, "duplicated_rows": None, "status": f"erro: {exc}"}
            )
    return pd.DataFrame(rows)


# --- Checks customizados (fks, chaves naturais únicas como cpf/cnpj, etc.) -------------------
#
# Padrão: cada check recebe os dados já carregados (via TableLoader) e devolve um
# CheckResult. Monte a lista de checks que fizer sentido para o seu schema e rode
# com `run_checks`. Use `check_unique`/`check_foreign_key` para os casos comuns, ou
# escreva sua própria função com a mesma assinatura para regras específicas.


@dataclass
class CheckResult:
    check_name: str
    table_name: str
    status: str  # "ok" ou "falhou"
    details: str = ""


def check_unique(df: pd.DataFrame, column: str, table_name: str, check_name: str | None = None) -> CheckResult:
    """Verifica se uma coluna não tem valores duplicados (ex.: cpf, cnpj, qualquer chave natural)."""
    dup_count = int(df[column].duplicated().sum())
    return CheckResult(
        check_name=check_name or f"unique:{column}",
        table_name=table_name,
        status="ok" if dup_count == 0 else "falhou",
        details="" if dup_count == 0 else f"{dup_count} valores duplicados em '{column}'",
    )


def check_foreign_key(
    df: pd.DataFrame,
    column: str,
    ref_df: pd.DataFrame,
    ref_column: str,
    table_name: str,
    check_name: str | None = None,
) -> CheckResult:
    """Verifica se todo valor não nulo de `column` existe em `ref_df[ref_column]`."""
    valid_values = set(ref_df[ref_column].dropna())
    invalid_count = int((~df[column].isna() & ~df[column].isin(valid_values)).sum())
    return CheckResult(
        check_name=check_name or f"fk:{column}->{ref_column}",
        table_name=table_name,
        status="ok" if invalid_count == 0 else "falhou",
        details="" if invalid_count == 0 else f"{invalid_count} linhas com '{column}' sem correspondência",
    )


def run_checks(checks: list[Callable[[], CheckResult]]) -> pd.DataFrame:
    """Executa uma lista de checks (funções sem argumento que retornam CheckResult) e monta o relatório.

    Uso típico com `functools.partial`:

        from functools import partial
        checks = [
            partial(check_unique, df_clientes, "cpf", "clientes"),
            partial(check_foreign_key, df_pedidos, "cliente_id", df_clientes, "id", "pedidos"),
            minha_verificacao_customizada,  # def minha_verificacao_customizada() -> CheckResult: ...
        ]
        relatorio = run_checks(checks)
    """
    return pd.DataFrame([check().__dict__ for check in checks])
