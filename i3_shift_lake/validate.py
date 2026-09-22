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

from i3_shift_lake.discover import DEFAULT_CONFIG_DIR, TableModel, load_model
from i3_shift_lake.extract import load_table_query


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


def load_status(output_dir: str, schema: str, config_dir: str = DEFAULT_CONFIG_DIR) -> pd.DataFrame:
    """Relatório da situação da carga: para cada tabela, quantos arquivos parquet,
    quantas linhas carregadas, e o percentual carregado frente ao último `discover()`
    (se houver modelo salvo em `config_dir`).

    Não precisa de um `TableModel` em mãos — lê `output_dir` diretamente e, se existir,
    o modelo persistido por `discover()`/`save_model()` em `config_dir`. Tabelas que o
    discover conhece mas que ainda não foram extraídas também aparecem (0 arquivos/linhas,
    status `nao_extraida`); sem um discover salvo, `expected_rows`/`pct` ficam vazios.
    """
    schema_dir = os.path.join(output_dir, schema)
    extracted_tables = (
        sorted(name for name in os.listdir(schema_dir) if os.path.isdir(os.path.join(schema_dir, name)))
        if os.path.isdir(schema_dir)
        else []
    )

    try:
        expected_by_table = load_model(schema, config_dir).row_counts
    except FileNotFoundError:
        expected_by_table = {}

    all_tables = sorted(set(extracted_tables) | set(expected_by_table.keys()))
    print(f"[load_status] verificando {len(all_tables)} tabelas do schema {schema}", flush=True)

    rows = []
    for table in all_tables:
        table_path = _table_path(output_dir, schema, table)
        if table in extracted_tables:
            dataset = ds.dataset(table_path, format="parquet")
            parquet_files = len(dataset.files)
            rows_loaded = dataset.count_rows()
        else:
            parquet_files = 0
            rows_loaded = 0

        expected_rows = expected_by_table.get(table)
        if table not in extracted_tables:
            status = "nao_extraida"
            pct = None
        elif expected_rows is None:
            status = "sem_discover"
            pct = None
        else:
            status = "ok" if rows_loaded == expected_rows else "divergente"
            if expected_rows > 0:
                pct = round(100.0 * rows_loaded / expected_rows, 1)
            else:
                # discover encontrou 0 linhas nessa tabela; se surgiram linhas depois
                # sem um discover novo, não dá pra expressar isso como percentual
                pct = 100.0 if rows_loaded == 0 else None

        rows.append(
            {
                "table_name": table,
                "parquet_files": parquet_files,
                "rows_loaded": rows_loaded,
                "expected_rows": expected_rows,
                "pct": pct,
                "status": status,
            }
        )

    return pd.DataFrame(
        rows,
        columns=["table_name", "parquet_files", "rows_loaded", "expected_rows", "pct", "status"],
    )


def check_duplicate_ids(
    output_dir: str, schema: str, table: str, config_dir: str = DEFAULT_CONFIG_DIR
) -> pd.DataFrame:
    """Verifica overlaps reais nos parquets de uma tabela: a MESMA versão de um id
    (mesmos valores de `order_by`, não só o id) lida mais de uma vez.

    Um id aparecer mais de uma vez com valores DIFERENTES de `order_by` não é reportado
    aqui — é o esperado quando o registro é modificado e reextraído numa carga
    incremental (`TableLoader.load()` já resolve isso, mantendo só a versão mais
    recente). Esta checagem foca no bug real: duas leituras da mesma linha exata,
    geralmente por overlap de paginação.
    """
    table_cfg = load_table_query(schema, table, config_dir)
    id_column = table_cfg["partition_column"]
    dedup_cols = list(dict.fromkeys(table_cfg["order_by"] + [id_column]))

    path = _table_path(output_dir, schema, table)
    if not os.path.isdir(path):
        raise FileNotFoundError(f"tabela '{table}' não encontrada em {path}")

    start = time.perf_counter()
    print(f"[check_duplicate_ids] varrendo {schema}.{table} pelas colunas {dedup_cols}...", flush=True)

    df = ds.dataset(path, format="parquet").to_table(columns=dedup_cols).to_pandas()
    counts = df.groupby(dedup_cols, dropna=False).size().reset_index(name="occurrences")
    duplicated = counts[counts["occurrences"] > 1].reset_index(drop=True)

    print(
        f"[check_duplicate_ids] {schema}.{table}: {len(duplicated)} overlap(s) real(is) "
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
