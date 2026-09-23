"""Smoke test do i3_shift_lake com um `query()` fake (sem precisar de Redshift real).

Roda: python i3_shift_lake/tests/smoke_test.py
(ou `python -m i3_shift_lake.tests.smoke_test` a partir da raiz do repo)

Cobre ping (validação do contrato query()), discover (+ persistência), o
fluxo alternativo via load_model, extract paginado/particionado, sample_size,
transform, validate (contagem/duplicidade/checks customizados) e a carga
incremental/full com checkpoint — de ponta a ponta, em memória, em poucos
segundos.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from functools import partial

import pandas as pd
import pyarrow.dataset as ds

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from i3_shift_lake import (  # noqa: E402
    ping, discover, extract_all, load_model, load_table_query, TableLoader,
    check_row_counts, check_duplicate_ids, check_duplicates_all, load_status,
    check_unique, check_foreign_key, run_checks,
    load_checkpoint, compact_table,
    Entity, Source, Join, ForeignKey, build_entity, build_all, check_foreign_keys,
)
from i3_shift_lake.extract import extract_table, NULL_DATE_SENTINEL, bucket_for  # noqa: E402

SCHEMA = "meu_schema"

# Dados fake simulando 3 tabelas: uma com date_modified+id, outra so com id_c, outra vazia.
# "notes" em accounts fica None nas primeiras 30 linhas (a 1a pagina com page_size=30) e
# preenchida no resto, para reproduzir o bug de schema inconsistente entre paginas
# (coluna de texto inteiramente nula numa pagina -> pyarrow infere tipo `null`).
FAKE_TABLES = {
    "accounts": pd.DataFrame(
        {
            "id": range(1, 251),
            "name": [f"conta {i}" for i in range(1, 251)],
            "notes": [None if i <= 30 else f"nota {i}" for i in range(1, 251)],
            "date_modified": pd.date_range("2024-01-01", periods=250, freq="h"),
        }
    ),
    "custom_leads_c": pd.DataFrame(
        {
            "id_c": [f"lead-{i:03d}" for i in range(1, 41)],
            "score": range(1, 41),
        }
    ),
    "empty_table": pd.DataFrame({"id": [], "name": []}),
}

DATA_TYPES = {
    "id": "integer",
    "name": "character varying",
    "notes": "character varying",
    "date_modified": "timestamp without time zone",
    "id_c": "character varying",
    "score": "integer",
}

COLUMNS_INFO = []
for table, df in FAKE_TABLES.items():
    for pos, col in enumerate(df.columns, start=1):
        COLUMNS_INFO.append(
            {"table_name": table, "column_name": col, "data_type": DATA_TYPES[col], "ordinal_position": pos}
        )
COLUMNS_DF = pd.DataFrame(COLUMNS_INFO)


def _split_top_level(s: str) -> list[str]:
    """Divide por vírgula ignorando vírgulas dentro de parênteses (ex.: dentro de um
    COALESCE(...)) — um `str.split(",")` simples quebraria nesse caso."""
    parts = []
    depth = 0
    current = ""
    for ch in s:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current.strip())
    return parts


def _extract_balanced_parens(s: str, open_idx: int) -> tuple[str, int]:
    """`s[open_idx]` precisa ser '('. Devolve (conteúdo interno, índice logo após o ')')."""
    depth = 0
    for i in range(open_idx, len(s)):
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
            if depth == 0:
                return s[open_idx + 1 : i], i + 1
    raise ValueError(f"parenteses desbalanceados em: {s[open_idx:]}")


def _parse_order_col_expr(expr: str) -> tuple[str, str | None]:
    """`'"col"'` -> (col, None); `'COALESCE("col", TIMESTAMP \\'YYYY-MM-DD\\')'` -> (col, sentinela)."""
    expr = expr.strip()
    m = re.match(r"COALESCE\(\"([^\"]+)\",\s*TIMESTAMP\s*'([^']+)'\)", expr)
    if m:
        return m.group(1), m.group(2)
    return expr.strip('"'), None


def _effective_series(df: pd.DataFrame, col: str, sentinel: str | None) -> pd.Series:
    series = df[col]
    if sentinel is not None:
        series = series.fillna(pd.Timestamp(sentinel))
    return series


def _apply_resume_where(df: pd.DataFrame, col_exprs: list[str], raw_values: list[str]) -> pd.DataFrame:
    """Simula `WHERE (col_exprs) > (values)` (comparação por tupla) para o teste do
    checkpoint — entende tanto colunas simples quanto `COALESCE(col, TIMESTAMP '...')`
    (usado para colunas de data que podem ser nulas)."""
    mask = pd.Series(False, index=df.index)
    still_equal = pd.Series(True, index=df.index)
    for expr, raw in zip(col_exprs, raw_values):
        col, sentinel = _parse_order_col_expr(expr)
        series = _effective_series(df, col, sentinel)
        raw = raw.split("::")[0].strip()  # remove cast explicito, ex: '...'::timestamp
        if raw == "NULL":
            # comparacao com NULL nunca e verdadeira em SQL -> nada passa a partir daqui
            return df.iloc[0:0]
        if pd.api.types.is_datetime64_any_dtype(series):
            val = pd.Timestamp(raw.strip("'"))
        elif raw.startswith("'"):
            val = raw.strip("'")
        else:
            val = int(raw)
        mask = mask | (still_equal & (series > val))
        still_equal = still_equal & (series == val)
    return df[mask]


def make_fake_query(tables: dict, columns_df: pd.DataFrame):
    """Fábrica de `query()` fake sobre um dict de tabelas (mutável — mudanças em `tables`
    após a criação já valem na próxima chamada, simulando dados novos/modificados)."""

    def query(sql: str) -> pd.DataFrame:
        if sql.strip() == "SELECT 1 AS ok":
            return pd.DataFrame({"ok": [1]})
        if "information_schema.columns" in sql:
            return columns_df
        table = re.search(r'FROM "[^"]+"\."([^"]+)"', sql).group(1)
        if sql.strip().upper().startswith("SELECT COUNT(*)"):
            return pd.DataFrame({"row_count": [len(tables[table])]})

        df = tables[table]
        where_idx = sql.find("WHERE (")
        if where_idx != -1:
            open_idx = sql.index("(", where_idx)
            cols_str, after = _extract_balanced_parens(sql, open_idx)
            rest = sql[after:]
            gt_idx = rest.index(">")
            open_idx2 = after + rest.index("(", gt_idx)
            vals_str, _ = _extract_balanced_parens(sql, open_idx2)
            col_exprs = _split_top_level(cols_str)
            raw_values = _split_top_level(vals_str)
            df = _apply_resume_where(df, col_exprs, raw_values)

        # query paginada: SELECT cols FROM "schema"."table" [WHERE ...] ORDER BY ... LIMIT n OFFSET m
        limit = int(sql.split("LIMIT")[1].split("OFFSET")[0].strip())
        offset = int(sql.split("OFFSET")[1].strip())
        order_by_part = sql.split("ORDER BY")[1].split("LIMIT")[0].strip()
        order_specs = [_parse_order_col_expr(e) for e in _split_top_level(order_by_part)]

        sort_df = df.copy()
        sort_keys = []
        for i, (col, sentinel) in enumerate(order_specs):
            key = f"__sort_key_{i}"
            sort_df[key] = _effective_series(df, col, sentinel)
            sort_keys.append(key)
        sorted_df = sort_df.sort_values(sort_keys, na_position="last").drop(columns=sort_keys)
        return sorted_df.iloc[offset : offset + limit].reset_index(drop=True)

    return query


fake_query = make_fake_query(FAKE_TABLES, COLUMNS_DF)


def main() -> None:
    base_dir = tempfile.mkdtemp(prefix="i3_shift_lake_smoke_")
    print(f"diretorio temporario: {base_dir}")
    output_dir = f"{base_dir}/out"
    config_dir = f"{base_dir}/config"
    control_dir = f"{base_dir}/control"

    try:
        print("=== ping (valida o contrato query()) ===")
        assert ping(fake_query) is True
        print("ping OK")

        print("\n=== discover (persiste config) ===")
        model = discover(fake_query, SCHEMA, config_dir=config_dir)
        print("tables:", model.tables)
        print("row_counts:", model.row_counts)
        print("tables_with_rows:", model.tables_with_rows())

        model_json_path = os.path.join(config_dir, f"{SCHEMA}.json")
        assert os.path.isfile(model_json_path), "modelo nao foi persistido"
        with open(model_json_path) as f:
            persisted = json.load(f)
        assert persisted["schema"] == SCHEMA
        assert persisted["row_counts"] == model.row_counts
        print("modelo persistido em:", model_json_path)

        print("\n=== fluxo alternativo: load_model (sem tocar o Redshift) ===")
        model_reloaded = load_model(SCHEMA, config_dir=config_dir)
        assert model_reloaded.tables == model.tables
        assert model_reloaded.row_counts == model.row_counts
        print("load_model OK, tables_with_rows:", model_reloaded.tables_with_rows())

        print("\n=== extract_all (page_size=30, num_buckets=4), a partir do modelo recarregado ===")
        report = extract_all(
            fake_query, model_reloaded, output_dir, page_size=30, num_buckets=4,
            config_dir=config_dir, control_dir=control_dir,
        )
        print(report.to_string())

        print("\n=== query de cada tabela persistida ===")
        for table in ["accounts", "custom_leads_c"]:
            table_query_path = os.path.join(config_dir, SCHEMA, f"{table}.json")
            assert os.path.isfile(table_query_path), f"query de {table} nao foi persistida"
            cfg = load_table_query(SCHEMA, table, config_dir=config_dir)
            print(f"- {table}: order_by={cfg['order_by']} partition_column={cfg['partition_column']}")
            print(f"  select_query: {cfg['select_query']}")
            assert "{page_size}" in cfg["select_query"] and "{offset}" in cfg["select_query"]

        print("\n=== transform: TableLoader ===")
        loader = TableLoader(output_dir, schema=SCHEMA, config_dir=config_dir)
        print("available_tables:", loader.available_tables())

        df_accounts = loader["accounts"]
        print("\naccounts loaded shape:", df_accounts.shape)
        print("accounts columns:", list(df_accounts.columns))
        print("notes dtype:", df_accounts["notes"].dtype)
        assert df_accounts.shape[0] == 250
        assert "bucket" not in df_accounts.columns
        assert set(df_accounts["id"]) == set(range(1, 251))
        # reproduz o bug de schema inconsistente: "notes" e None na 1a pagina (id<=30) e
        # preenchida nas demais; sem a coercao de dtype por column_types isso quebra a
        # leitura do dataset completo com ArrowNotImplementedError (large_string -> null)
        assert df_accounts.loc[df_accounts["id"] <= 30, "notes"].isna().all()
        esperado = df_accounts.loc[df_accounts["id"] > 30, "id"].map(lambda i: f"nota {i}")
        assert (df_accounts.loc[df_accounts["id"] > 30, "notes"] == esperado).all()
        print("coluna 'notes' lida corretamente mesmo com pagina 100% nula (bug de schema corrigido)")

        df_leads = loader.load("custom_leads_c")
        print("\ncustom_leads_c loaded shape:", df_leads.shape)
        assert df_leads.shape[0] == 40
        assert set(df_leads["id_c"]) == {f"lead-{i:03d}" for i in range(1, 41)}

        try:
            loader.load("empty_table")
            raise AssertionError("esperava FileNotFoundError para tabela vazia (nao extraida)")
        except FileNotFoundError:
            print("\nempty_table corretamente nao extraida (0 linhas) -> FileNotFoundError ao carregar")

        print("\n=== extract_all com sample_size=10 (dev/teste, base parcial) ===")
        sample_output_dir = f"{base_dir}/out_sample"
        sample_config_dir = f"{base_dir}/config_sample"
        sample_control_dir = f"{base_dir}/control_sample"

        sample_report = extract_all(
            fake_query, model, sample_output_dir, page_size=30, num_buckets=4,
            config_dir=sample_config_dir, control_dir=sample_control_dir, sample_size=10,
        )
        print(sample_report.to_string())
        assert (sample_report["rows_read"] <= 10).all()
        assert (sample_report["sample_size"] == 10).all()

        sample_loader = TableLoader(sample_output_dir, schema=SCHEMA, config_dir=sample_config_dir)
        df_accounts_sample = sample_loader["accounts"]
        print("\naccounts (sample) shape:", df_accounts_sample.shape)
        assert df_accounts_sample.shape[0] == 10
        # mesma ordenacao da extracao completa -> sample deve ser o "topo" da ordenacao
        assert set(df_accounts_sample["id"]) == set(range(1, 11))

        sample_cfg = load_table_query(SCHEMA, "accounts", config_dir=sample_config_dir)
        assert sample_cfg["sample_size"] == 10
        print("sample_size persistido na query de accounts:", sample_cfg["sample_size"])

        print("\n=== validate: check_row_counts (extracao completa em output_dir) ===")
        row_counts_report = check_row_counts(model, output_dir)
        print(row_counts_report.to_string())
        assert (row_counts_report["status"] == "ok").all()
        assert (row_counts_report["diff"] == 0).all()

        print("\n=== validate: load_status (relatorio da situacao da carga) ===")
        status_report = load_status(output_dir, SCHEMA, config_dir=config_dir)
        print(status_report.to_string())
        status_by_table = status_report.set_index("table_name")
        assert status_by_table.loc["accounts", "status"] == "ok"
        assert status_by_table.loc["accounts", "pct"] == 100.0
        assert status_by_table.loc["accounts", "parquet_files"] > 0
        assert status_by_table.loc["custom_leads_c", "status"] == "ok"
        assert status_by_table.loc["custom_leads_c", "pct"] == 100.0
        # empty_table tem 0 linhas no discover -> extract_all pula (tables_with_rows),
        # entao nunca foi extraida: cenario real de tabela conhecida mas ainda sem parquet.
        assert status_by_table.loc["empty_table", "status"] == "nao_extraida"
        assert status_by_table.loc["empty_table", "parquet_files"] == 0
        assert status_by_table.loc["empty_table", "rows_loaded"] == 0
        assert pd.isna(status_by_table.loc["empty_table", "pct"])
        print("load_status: ok / percentuais corretos / tabela nao extraida sinalizada")

        print("\n=== validate: load_status sem discover salvo (config_dir vazio) ===")
        no_discover_dir = os.path.join(base_dir, "config_sem_discover")
        status_no_discover = load_status(output_dir, SCHEMA, config_dir=no_discover_dir)
        assert (status_no_discover["status"] == "sem_discover").all()
        assert status_no_discover["expected_rows"].isna().all()
        print("load_status sem discover: status 'sem_discover' para todas as tabelas extraidas")

        print("\n=== validate: check_duplicate_ids (sem duplicatas esperado) ===")
        dup_report = check_duplicates_all(model, output_dir, config_dir=config_dir)
        print(dup_report.to_string())
        assert (dup_report["status"] == "ok").all()
        assert (dup_report["duplicated_ids"] == 0).all()

        print("\n=== validate: forcando uma duplicata de id em accounts ===")
        accounts_dir = os.path.join(output_dir, SCHEMA, "accounts")
        dup_row = pd.DataFrame(
            {
                "id": [1],
                "name": ["conta 1 duplicada"],
                "notes": ["nota da duplicata"],
                "date_modified": [pd.Timestamp("2024-01-01")],
            }
        )
        dup_row["bucket"] = 0
        dup_row.to_parquet(accounts_dir, engine="pyarrow", partition_cols=["bucket"], index=False)

        dup_check_accounts = check_duplicate_ids(output_dir, SCHEMA, "accounts", config_dir=config_dir)
        print(dup_check_accounts.to_string())
        assert len(dup_check_accounts) == 1
        assert dup_check_accounts.iloc[0]["id"] == 1
        assert dup_check_accounts.iloc[0]["occurrences"] == 2
        print("duplicata de id=1 detectada corretamente")

        print("\n=== validate: checks customizados (unique / fk) ===")
        # dedupe=False: queremos ver a duplicata proposital crua aqui (por padrao o
        # TableLoader ja a resolveria sozinho, como comprovado no bloco de garantias abaixo)
        df_accounts_full = loader.load("accounts", dedupe=False)
        custom_checks = [
            partial(check_unique, df_accounts_full, "id", "accounts"),
            partial(check_unique, df_leads, "id_c", "custom_leads_c"),
            partial(check_foreign_key, df_leads, "id_c", df_leads, "id_c", "custom_leads_c", "fk_autoreferencia_ok"),
        ]
        custom_report = run_checks(custom_checks)
        print(custom_report.to_string())
        assert custom_report.loc[custom_report["check_name"] == "unique:id", "status"].iloc[0] == "falhou"
        assert custom_report.loc[custom_report["check_name"] == "unique:id_c", "status"].iloc[0] == "ok"
        assert custom_report.loc[custom_report["check_name"] == "fk_autoreferencia_ok", "status"].iloc[0] == "ok"

        print("\n=== carga incremental: 1a rodada (sem checkpoint previo = full de fato) ===")
        incr_output_dir = f"{base_dir}/out_incr"
        incr_config_dir = f"{base_dir}/config_incr"
        incr_control_dir = f"{base_dir}/control_incr"

        r1 = extract_all(
            fake_query, model, incr_output_dir, page_size=30, num_buckets=4,
            config_dir=incr_config_dir, control_dir=incr_control_dir,
        )
        print(r1.to_string())
        assert (r1["load_mode"] == "incremental").all()
        assert r1.loc[r1["table_name"] == "accounts", "rows_read"].iloc[0] == 250
        assert r1.loc[r1["table_name"] == "custom_leads_c", "rows_read"].iloc[0] == 40

        ckpt_accounts = load_checkpoint(SCHEMA, "accounts", control_dir=incr_control_dir)
        ckpt_leads = load_checkpoint(SCHEMA, "custom_leads_c", control_dir=incr_control_dir)
        print("checkpoint accounts:", ckpt_accounts)
        print("checkpoint custom_leads_c:", ckpt_leads)
        assert ckpt_accounts["order_by"] == ["date_modified", "id"]
        assert ckpt_accounts["last_values"][1] == 250
        assert ckpt_leads["order_by"] == ["id_c"]
        assert ckpt_leads["last_values"][0] == "lead-040"

        stopped_at_accounts_r1 = r1.loc[r1["table_name"] == "accounts", "stopped_at"].iloc[0]
        print("stopped_at (accounts, r1):", stopped_at_accounts_r1)
        assert stopped_at_accounts_r1["id"] == 250

        print("\n=== carga incremental: 2a rodada sem dados novos (deve trazer 0 linhas) ===")
        r2 = extract_all(
            fake_query, model, incr_output_dir, page_size=30, num_buckets=4,
            config_dir=incr_config_dir, control_dir=incr_control_dir,
        )
        print(r2.to_string())
        assert (r2["rows_read"] == 0).all()
        assert (r2["pages"] == 0).all()
        # sem linha nova, stopped_at deve continuar no mesmo ponto da rodada anterior
        assert r2.loc[r2["table_name"] == "accounts", "stopped_at"].iloc[0]["id"] == 250

        print("\n=== carga incremental: chegam dados novos, 3a rodada deve trazer so o incremento ===")
        novas_contas = pd.DataFrame(
            {
                "id": range(251, 256),
                "name": [f"conta {i}" for i in range(251, 256)],
                "notes": [f"nota {i}" for i in range(251, 256)],
                "date_modified": pd.date_range("2024-02-01", periods=5, freq="h"),
            }
        )
        FAKE_TABLES["accounts"] = pd.concat([FAKE_TABLES["accounts"], novas_contas], ignore_index=True)

        novos_leads = pd.DataFrame({"id_c": [f"lead-{i:03d}" for i in range(41, 46)], "score": range(41, 46)})
        FAKE_TABLES["custom_leads_c"] = pd.concat([FAKE_TABLES["custom_leads_c"], novos_leads], ignore_index=True)

        r3 = extract_all(
            fake_query, model, incr_output_dir, page_size=30, num_buckets=4,
            config_dir=incr_config_dir, control_dir=incr_control_dir,
        )
        print(r3.to_string())
        assert r3.loc[r3["table_name"] == "accounts", "rows_read"].iloc[0] == 5
        assert r3.loc[r3["table_name"] == "custom_leads_c", "rows_read"].iloc[0] == 5
        # stopped_at avancou para a ultima linha nova (id=255)
        assert r3.loc[r3["table_name"] == "accounts", "stopped_at"].iloc[0]["id"] == 255

        incr_loader = TableLoader(incr_output_dir, schema=SCHEMA, config_dir=incr_config_dir)
        df_accounts_incr = incr_loader["accounts"]
        df_leads_incr = incr_loader["custom_leads_c"]
        print("\naccounts acumulado:", df_accounts_incr.shape, "| custom_leads_c acumulado:", df_leads_incr.shape)
        assert df_accounts_incr.shape[0] == 255
        assert set(df_accounts_incr["id"]) == set(range(1, 256))
        assert df_leads_incr.shape[0] == 45
        assert set(df_leads_incr["id_c"]) == {f"lead-{i:03d}" for i in range(1, 46)}

        dup_check_incr = check_duplicate_ids(incr_output_dir, SCHEMA, "accounts", config_dir=incr_config_dir)
        assert dup_check_incr.empty
        print("nenhuma duplicata apos as 3 rodadas incrementais (sem pular nem repetir linhas)")

        print("\n=== carga full: deve descartar checkpoint/dados antigos e recomecar do zero ===")
        r4 = extract_all(
            fake_query, model, incr_output_dir, page_size=30, num_buckets=4,
            config_dir=incr_config_dir, control_dir=incr_control_dir, load_mode="full",
        )
        print(r4.to_string())
        assert (r4["load_mode"] == "full").all()
        assert r4.loc[r4["table_name"] == "accounts", "rows_read"].iloc[0] == 255
        assert r4.loc[r4["table_name"] == "custom_leads_c", "rows_read"].iloc[0] == 45

        df_accounts_full = incr_loader.load("accounts")
        print("accounts apos full:", df_accounts_full.shape)
        assert df_accounts_full.shape[0] == 255  # e nao 250+5+255 -> confirma que o full limpou o output_dir antes
        assert set(df_accounts_full["id"]) == set(range(1, 256))

        ckpt_accounts_full = load_checkpoint(SCHEMA, "accounts", control_dir=incr_control_dir)
        assert ckpt_accounts_full["last_values"][1] == 255
        print("checkpoint apos full:", ckpt_accounts_full)

        print("\n=== checkpoint com valor nulo, SEM column_types (fallback antigo) ===")
        # reproduz o bug real: 'column "none" does not exist' quando o checkpoint guarda
        # um None (data nula na ultima linha extraida) e o proximo WHERE por tupla embutia
        # str(None) sem aspas na query. Sem column_types, o framework nao sabe que a coluna
        # e uma data e nao consegue aplicar o COALESCE -- degrada para o comportamento
        # anterior (nao quebra, mas o checkpoint fica em NULL e pode "travar" o incremental;
        # ver o bloco seguinte, com column_types, para a correcao de verdade).
        null_tables = {
            "tabela_com_data_nula": pd.DataFrame(
                {
                    "id": [1, 2, 3],
                    "date_modified": [pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-02"), None],
                }
            )
        }
        null_columns_df = pd.DataFrame(
            [
                {"table_name": "tabela_com_data_nula", "column_name": "id", "data_type": "integer", "ordinal_position": 1},
                {
                    "table_name": "tabela_com_data_nula", "column_name": "date_modified",
                    "data_type": "timestamp without time zone", "ordinal_position": 2,
                },
            ]
        )

        null_query = make_fake_query(null_tables, null_columns_df)

        null_output_dir = f"{base_dir}/out_null"
        null_config_dir = f"{base_dir}/config_null"
        null_control_dir = f"{base_dir}/control_null"

        buf = io.StringIO()
        with redirect_stdout(buf):
            stats_null = extract_table(
                null_query, SCHEMA, "tabela_com_data_nula", ["id", "date_modified"],
                null_output_dir, page_size=10, config_dir=null_config_dir, control_dir=null_control_dir,
            )
        printed = buf.getvalue()
        print(printed)
        assert "AVISO" in printed and "valor nulo" in printed, "esperava o aviso de checkpoint com valor nulo"
        assert stats_null["rows_read"] == 3
        assert pd.isna(stats_null["stopped_at"]["date_modified"])

        ckpt_null_path = os.path.join(null_control_dir, SCHEMA, "tabela_com_data_nula", "checkpoint.json")
        with open(ckpt_null_path) as f:
            ckpt_null_raw = json.load(f)
        assert ckpt_null_raw["order_by"] == ["date_modified", "id"]
        assert ckpt_null_raw["last_values"][0] is None, "checkpoint deveria guardar null, nao a string 'NaT'"

        # rodar de novo (incremental, retomando do checkpoint com NULL) nao pode mais quebrar
        # com 'column "none" does not exist' -- so pode nao trazer linha nenhuma (NULL nunca
        # e "maior que" nada em SQL), o que e esperado e nao um crash.
        stats_null_2 = extract_table(
            null_query, SCHEMA, "tabela_com_data_nula", ["id", "date_modified"],
            null_output_dir, page_size=10, config_dir=null_config_dir, control_dir=null_control_dir,
        )
        assert stats_null_2["status"] == "ok"
        print("checkpoint com valor nulo: sem crash ao retomar (rows_read =", stats_null_2["rows_read"], ")")

        print("\n=== checkpoint com valor nulo, COM column_types (correcao com COALESCE) ===")
        # mesmo cenario, mas passando column_types -- agora o framework sabe que
        # "date_modified" e uma data e aplica COALESCE(date_modified, NULL_DATE_SENTINEL)
        # no ORDER BY e no filtro de retomada. NULL_DATE_SENTINEL e a menor data possivel,
        # entao a linha com data nula (id=3) passa a ordenar PRIMEIRO (nao mais por ultimo);
        # o checkpoint fica com a data real mais recente (id=2), nao com o sentinela --
        # mas o importante e que NULL nunca mais entra na comparacao, sem aviso, sem crash.
        null2_tables = {
            "tabela_com_data_nula": pd.DataFrame(
                {
                    "id": [1, 2, 3],
                    "date_modified": [pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-02"), None],
                }
            )
        }
        null2_query = make_fake_query(null2_tables, null_columns_df)
        column_types_null2 = {"id": "integer", "date_modified": "timestamp without time zone"}

        null2_output_dir = f"{base_dir}/out_null2"
        null2_config_dir = f"{base_dir}/config_null2"
        null2_control_dir = f"{base_dir}/control_null2"

        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            stats_null2 = extract_table(
                null2_query, SCHEMA, "tabela_com_data_nula", ["id", "date_modified"],
                null2_output_dir, page_size=10, config_dir=null2_config_dir, control_dir=null2_control_dir,
                column_types=column_types_null2,
            )
        printed2 = buf2.getvalue()
        print(printed2)
        assert "AVISO" not in printed2, "com column_types, o COALESCE deveria evitar o aviso de valor nulo"
        assert stats_null2["rows_read"] == 3
        assert stats_null2["stopped_at"]["date_modified"] == pd.Timestamp("2024-01-02")
        assert stats_null2["stopped_at"]["id"] == 2

        # chega uma atualizacao de verdade no registro que tinha data nula (id=3):
        # agora tem date_modified real, mais recente que o checkpoint atual (id=2, 2024-01-02)
        null2_tables["tabela_com_data_nula"].loc[
            null2_tables["tabela_com_data_nula"]["id"] == 3, "date_modified"
        ] = pd.Timestamp("2024-03-01")

        stats_null2_b = extract_table(
            null2_query, SCHEMA, "tabela_com_data_nula", ["id", "date_modified"],
            null2_output_dir, page_size=10, config_dir=null2_config_dir, control_dir=null2_control_dir,
            column_types=column_types_null2,
        )
        assert stats_null2_b["rows_read"] == 1, (
            "a atualizacao real do id=3 (que antes tinha data nula) deveria ser pega no incremental"
        )
        print(
            "checkpoint com valor nulo (com column_types): sem aviso, sem travar, e a "
            "atualizacao real do id=3 foi capturada no incremental seguinte"
        )

        # caso extremo: TODAS as linhas tem date_modified nula (o cenario real relatado,
        # onde a absoluta maioria da tabela nunca foi modificada) -- aqui sim o checkpoint
        # deve terminar gravado no sentinela, sem crash e sem NULL no JSON.
        allnull_tables = {
            "tabela_toda_nula": pd.DataFrame({"id": [1, 2, 3], "date_modified": [None, None, None]})
        }
        allnull_columns_df = pd.DataFrame(
            [
                {"table_name": "tabela_toda_nula", "column_name": "id", "data_type": "integer", "ordinal_position": 1},
                {
                    "table_name": "tabela_toda_nula", "column_name": "date_modified",
                    "data_type": "timestamp without time zone", "ordinal_position": 2,
                },
            ]
        )
        allnull_query = make_fake_query(allnull_tables, allnull_columns_df)
        allnull_output_dir = f"{base_dir}/out_allnull"
        allnull_config_dir = f"{base_dir}/config_allnull"
        allnull_control_dir = f"{base_dir}/control_allnull"

        stats_allnull = extract_table(
            allnull_query, SCHEMA, "tabela_toda_nula", ["id", "date_modified"],
            allnull_output_dir, page_size=10, config_dir=allnull_config_dir, control_dir=allnull_control_dir,
            column_types=column_types_null2,
        )
        assert stats_allnull["rows_read"] == 3
        assert stats_allnull["stopped_at"]["date_modified"] == pd.Timestamp(NULL_DATE_SENTINEL)

        ckpt_allnull_path = os.path.join(allnull_control_dir, SCHEMA, "tabela_toda_nula", "checkpoint.json")
        with open(ckpt_allnull_path) as f:
            ckpt_allnull_raw = json.load(f)
        assert ckpt_allnull_raw["last_values"][0] == f"{NULL_DATE_SENTINEL}T00:00:00", (
            "com todas as datas nulas, o checkpoint deve guardar o sentinela, nao null"
        )
        print(f"tabela 100% nula: checkpoint gravado no sentinela ({NULL_DATE_SENTINEL}), sem crash")

        print(
            "\n=== garantia da carga incremental: sem gaps, sem overlap de versao igual, "
            "substitui ao modificar ==="
        )
        N = 47  # nao divide "redondo" com page_size=7 -> forca varias paginas com sobra nas bordas
        versao_tables = {
            "eventos": pd.DataFrame(
                {
                    "id": range(1, N + 1),
                    "valor": ["v1"] * N,
                    "date_modified": pd.date_range("2023-01-01", periods=N, freq="h"),
                }
            )
        }
        versao_columns_df = pd.DataFrame(
            [
                {"table_name": "eventos", "column_name": "id", "data_type": "integer", "ordinal_position": 1},
                {"table_name": "eventos", "column_name": "valor", "data_type": "character varying", "ordinal_position": 2},
                {
                    "table_name": "eventos", "column_name": "date_modified",
                    "data_type": "timestamp without time zone", "ordinal_position": 3,
                },
            ]
        )
        versao_query = make_fake_query(versao_tables, versao_columns_df)

        versao_output_dir = f"{base_dir}/out_versao"
        versao_config_dir = f"{base_dir}/config_versao"
        versao_control_dir = f"{base_dir}/control_versao"
        versao_cols = ["id", "valor", "date_modified"]

        def extrai_eventos():
            return extract_table(
                versao_query, SCHEMA, "eventos", versao_cols, versao_output_dir,
                page_size=7, config_dir=versao_config_dir, control_dir=versao_control_dir,
            )

        # 1a carga: extrai tudo
        r_v1 = extrai_eventos()
        assert r_v1["rows_read"] == N

        loader_versao = TableLoader(versao_output_dir, schema=SCHEMA, config_dir=versao_config_dir)
        df_v1 = loader_versao["eventos"]
        assert df_v1.shape[0] == N, "sem gaps: todas as N linhas devem estar presentes apos a 1a carga"
        assert set(df_v1["id"]) == set(range(1, N + 1)), "sem gaps: nenhum id pode faltar"
        assert (df_v1["valor"] == "v1").all()
        print(f"1a carga: {N} linhas, sem gaps (ids 1..{N} todos presentes)")

        # 2a carga sem mudancas na fonte: nao pode reler nada
        r_v2 = extrai_eventos()
        assert r_v2["rows_read"] == 0, "id nao modificado nao deveria ser lido de novo"
        dup_check_v2 = check_duplicate_ids(versao_output_dir, SCHEMA, "eventos", config_dir=versao_config_dir)
        assert dup_check_v2.empty, "nao deveria haver overlap real apos a 2a carga (nada foi relido)"
        print("2a carga sem mudancas: 0 linhas relidas, sem overlap")

        # modifica 3 registros existentes (mesmo id, novo valor, date_modified mais recente)
        ids_modificados = [5, 20, 40]
        nova_data = pd.Timestamp("2023-01-01") + pd.Timedelta(hours=N + 10)
        tabela_eventos = versao_tables["eventos"]
        for idx in ids_modificados:
            tabela_eventos.loc[tabela_eventos["id"] == idx, "valor"] = "v2"
            tabela_eventos.loc[tabela_eventos["id"] == idx, "date_modified"] = nova_data

        # 3a carga: deve reler EXATAMENTE os ids modificados, nada mais
        r_v3 = extrai_eventos()
        assert r_v3["rows_read"] == len(ids_modificados), "deveria reler so os ids modificados"

        # a releitura de um id modificado (versao diferente) NAO e um overlap
        dup_check_v3 = check_duplicate_ids(versao_output_dir, SCHEMA, "eventos", config_dir=versao_config_dir)
        assert dup_check_v3.empty, "reextrair um id modificado (versao diferente) nao e um overlap"

        # TableLoader deve devolver so N linhas (nao N+3): a versao nova substitui a antiga
        df_v3 = loader_versao["eventos"]
        assert df_v3.shape[0] == N, "a versao antiga do id modificado nao pode continuar aparecendo"
        assert set(df_v3["id"]) == set(range(1, N + 1))
        for idx in ids_modificados:
            valor_atual = df_v3.loc[df_v3["id"] == idx, "valor"].iloc[0]
            assert valor_atual == "v2", f"id {idx} deveria estar na versao nova (v2), veio '{valor_atual}'"
        nao_modificados = [i for i in range(1, N + 1) if i not in ids_modificados]
        assert (df_v3.loc[df_v3["id"].isin(nao_modificados), "valor"] == "v1").all()
        print(
            f"3a carga: {len(ids_modificados)} ids modificados relidos e substituidos "
            f"(sem overlap, sem gap, ainda {N} linhas no total)"
        )

        # simula um overlap real (ex.: bug de paginacao): grava manualmente a MESMA linha
        # (mesmo id, mesma data_modified) de novo no parquet, sem passar pelo extract.
        # usa bucket_for de verdade (nao um valor fixo): uma escrita real do _write_page
        # sempre cai no mesmo bucket pro mesmo id, e compact_table conta com essa invariante
        # (dedupe e feito por bucket) — um overlap de paginacao de verdade nunca atravessa buckets.
        overlap_row = df_v1[df_v1["id"] == 1].copy()  # id=1 nunca foi modificado
        overlap_row["bucket"] = bucket_for(1, 32)
        overlap_dir = os.path.join(versao_output_dir, SCHEMA, "eventos")
        overlap_row.to_parquet(overlap_dir, engine="pyarrow", partition_cols=["bucket"], index=False)

        dup_check_overlap = check_duplicate_ids(versao_output_dir, SCHEMA, "eventos", config_dir=versao_config_dir)
        assert not dup_check_overlap.empty, "overlap real (mesma linha lida 2x) deveria ser detectado"
        assert dup_check_overlap.iloc[0]["id"] == 1

        df_apos_overlap = loader_versao["eventos"]
        assert df_apos_overlap.shape[0] == N, "TableLoader deve dedupar overlap real tambem"
        print("overlap real (mesma versao lida 2x) detectado por check_duplicate_ids e dedupado pelo TableLoader")

        print("\n=== compact_table: junta arquivos por bucket, descarta linhas mortas ===")
        eventos_dataset = ds.dataset(overlap_dir, format="parquet")
        files_antes = len(eventos_dataset.files)
        rows_cru_antes = eventos_dataset.count_rows()
        assert rows_cru_antes == N + len(ids_modificados) + 1, (
            "linhas cruas esperadas: N originais + versoes antigas dos modificados + 1 overlap manual"
        )

        # min_age_hours bem alto: nada e elegivel, nada muda
        report_nada = compact_table(
            versao_output_dir, SCHEMA, "eventos", config_dir=versao_config_dir, min_age_hours=100000,
        )
        assert report_nada.empty, "com min_age_hours absurdamente alto, nenhum arquivo deveria ser elegivel"
        assert len(ds.dataset(overlap_dir, format="parquet").files) == files_antes
        print("min_age_hours alto: nenhum arquivo recente foi tocado")

        # dry_run=True: calcula e reporta, mas nao mexe em nada
        report_dry = compact_table(
            versao_output_dir, SCHEMA, "eventos", config_dir=versao_config_dir,
            min_age_hours=0, dry_run=True,
        )
        assert not report_dry.empty, "com min_age_hours=0 e dados existentes, deveria haver o que compactar"
        assert len(ds.dataset(overlap_dir, format="parquet").files) == files_antes, "dry_run nao pode alterar arquivos"
        print(f"dry_run: {len(report_dry)} bucket(s) seriam compactados, nada foi alterado de fato")

        # compactacao de verdade: junta arquivos e descarta versoes mortas (via dedupe)
        report_real = compact_table(
            versao_output_dir, SCHEMA, "eventos", config_dir=versao_config_dir, min_age_hours=0,
        )
        assert not report_real.empty

        eventos_dataset_depois = ds.dataset(overlap_dir, format="parquet")
        files_depois = len(eventos_dataset_depois.files)
        rows_cru_depois = eventos_dataset_depois.count_rows()
        assert files_depois < files_antes, "compactacao deveria reduzir o numero de arquivos"
        assert rows_cru_depois == N, (
            "com dedupe=True, a compactacao deveria descartar as versoes mortas e o overlap real, "
            "sobrando so as N versoes vigentes"
        )
        print(f"compactacao: {files_antes} -> {files_depois} arquivos, {rows_cru_antes} -> {rows_cru_depois} linhas cruas")

        # dados continuam corretos e identicos aos de antes da compactacao, do ponto de vista de quem le
        df_pos_compact = TableLoader(versao_output_dir, schema=SCHEMA, config_dir=versao_config_dir)["eventos"]
        pd.testing.assert_frame_equal(
            df_pos_compact.sort_values("id").reset_index(drop=True),
            df_apos_overlap.sort_values("id").reset_index(drop=True),
        )
        dup_check_pos_compact = check_duplicate_ids(versao_output_dir, SCHEMA, "eventos", config_dir=versao_config_dir)
        assert dup_check_pos_compact.empty, "sem overlaps reais depois da compactacao (foram descartados)"
        print("compactacao e transparente para quem le: mesmos dados de antes, sem overlap")

        # idempotencia: rodar de novo nao tem mais nada pra ganhar
        report_idempotente = compact_table(
            versao_output_dir, SCHEMA, "eventos", config_dir=versao_config_dir, min_age_hours=0,
        )
        assert report_idempotente.empty, "sem arquivos novos desde a ultima compactacao, nao ha nada a fazer"
        print("compactacao e idempotente: rodar de novo sem novas cargas nao muda nada")

        print("\n=== silver: build_entity/build_all monta entidades a partir da raw ===")
        silver_raw_tables = {
            "XPTO": pd.DataFrame(
                {
                    "id": range(1, 6),
                    "NOME": [f"Produto {i}" for i in range(1, 6)],
                    "date_modified": pd.date_range("2024-01-01", periods=5, freq="D"),
                }
            ),
            "BETA": pd.DataFrame(
                {
                    "id": range(1, 11),
                    "rotulo": [f"beta-{i}" for i in range(1, 11)],
                    "ativo": [True] * 8 + [False] * 2,  # ids 9 e 10 inativos
                    "date_modified": pd.date_range("2024-02-01", periods=10, freq="D"),
                }
            ),
            "GAMA": pd.DataFrame(
                {
                    "id_c": [1, 2, 3, 4, 5, 6, 7, 8, 9, 99],  # 99 nao existe em BETA
                    "pontuacao": [10, 20, 30, 40, 50, 60, 70, 80, 90, 990],
                    "date_modified": pd.date_range("2024-03-01", periods=10, freq="D"),
                }
            ),
            "X": pd.DataFrame(
                {
                    "id": range(1, 6),
                    "XPTO_ID": [1, 2, 3, 4, 5],
                    "ALFA_ID": [1, 2, 3, 4, 999],  # 999 nao existe em alfa: violacao de FK proposital
                    "date_modified": pd.date_range("2024-04-01", periods=5, freq="D"),
                }
            ),
        }
        silver_columns_df = pd.DataFrame(columns=["table_name", "column_name", "data_type", "ordinal_position"])
        silver_query = make_fake_query(silver_raw_tables, silver_columns_df)

        raw_output_dir = f"{base_dir}/out_silver_raw"
        raw_config_dir = f"{base_dir}/config_silver_raw"
        raw_control_dir = f"{base_dir}/control_silver_raw"

        xpto_types = {"id": "integer", "date_modified": "timestamp without time zone"}
        beta_types = {"id": "integer", "ativo": "boolean", "date_modified": "timestamp without time zone"}
        gama_types = {"id_c": "integer", "date_modified": "timestamp without time zone"}
        x_types = {"id": "integer", "date_modified": "timestamp without time zone"}

        for table, cols, types in [
            ("XPTO", ["id", "NOME", "date_modified"], xpto_types),
            ("BETA", ["id", "rotulo", "ativo", "date_modified"], beta_types),
            ("GAMA", ["id_c", "pontuacao", "date_modified"], gama_types),
            ("X", ["id", "XPTO_ID", "ALFA_ID", "date_modified"], x_types),
        ]:
            extract_table(
                silver_query, SCHEMA, table, cols, raw_output_dir,
                config_dir=raw_config_dir, control_dir=raw_control_dir, column_types=types,
            )

        raw_loader = TableLoader(raw_output_dir, schema=SCHEMA, config_dir=raw_config_dir)

        # entidade xpto = tabela XPTO da raw (so renomeia NOME -> nome)
        xpto_entity = Entity(
            name="xpto", id_column="id",
            sources=[Source(table="XPTO", rename={"NOME": "nome"})],
        )
        # entidade alfa = juncao de BETA.id com GAMA.id_c, filtrando BETA pelos ativos
        # e renomeando date_modified de GAMA pra evitar colisao com o de BETA
        alfa_entity = Entity(
            name="alfa", id_column="id",
            sources=[
                Source(table="BETA", where="ativo == True"),
                Source(table="GAMA", rename={"date_modified": "date_modified_gama"}),
            ],
            joins=[Join(left_on="id", right_on="id_c", how="inner")],
        )
        # relacionamento rel = tabela X, vincula xpto.id (rel.xpto_id) e alfa.id (rel.alfa_id)
        rel_entity = Entity(
            name="rel", id_column="id",
            sources=[Source(table="X", rename={"XPTO_ID": "xpto_id", "ALFA_ID": "alfa_id"})],
            foreign_keys=[
                ForeignKey(column="xpto_id", entity="xpto", entity_column="id"),
                ForeignKey(column="alfa_id", entity="alfa", entity_column="id"),
            ],
        )

        silver_output_dir = f"{base_dir}/out_silver"
        silver_config_dir = f"{base_dir}/config_silver"
        entities = [xpto_entity, alfa_entity, rel_entity]

        build_report = build_all(entities, raw_loader, silver_output_dir, SCHEMA, config_dir=silver_config_dir)
        print(build_report.to_string())
        report_by_name = build_report.set_index("entity_name")
        assert report_by_name.loc["xpto", "rows"] == 5
        assert report_by_name.loc["alfa", "rows"] == 8, "so os ids ativos de BETA (1..8) que existem em GAMA"
        assert report_by_name.loc["rel", "rows"] == 5

        silver_loader = TableLoader(silver_output_dir, schema=SCHEMA, config_dir=silver_config_dir)

        df_xpto = silver_loader["xpto"]
        assert "nome" in df_xpto.columns and "NOME" not in df_xpto.columns
        assert set(df_xpto["nome"]) == {f"Produto {i}" for i in range(1, 6)}
        print("xpto: passthrough da raw com rename de coluna, 5 linhas")

        df_alfa = silver_loader["alfa"]
        assert set(df_alfa["id"]) == set(range(1, 9)), "so os ids ativos de BETA que existem em GAMA"
        assert "date_modified_gama" in df_alfa.columns and "date_modified" in df_alfa.columns, (
            "rename anti-colisao: as duas datas (BETA e GAMA) devem sobreviver, sem _x/_y"
        )
        print("alfa: merge BETA.id x GAMA.id_c com filtro de linha (ativo) e rename anti-colisao, 8 linhas")

        df_rel = silver_loader["rel"]
        assert set(df_rel.columns) >= {"id", "xpto_id", "alfa_id"}
        print("rel: relacionamento com FKs renomeadas para xpto_id/alfa_id")

        fk_report = check_foreign_keys(entities, silver_loader)
        print(fk_report.to_string())
        fk_xpto = fk_report[(fk_report["table_name"] == "rel") & (fk_report["check_name"] == "fk:xpto_id->id")].iloc[0]
        fk_alfa = fk_report[(fk_report["table_name"] == "rel") & (fk_report["check_name"] == "fk:alfa_id->id")].iloc[0]
        assert fk_xpto["status"] == "ok", "todo rel.xpto_id existe em xpto.id"
        assert fk_alfa["status"] == "falhou", "rel.alfa_id=999 nao existe em alfa.id (violacao proposital)"
        print("check_foreign_keys: pega a violacao proposital em rel.alfa_id, xpto_id ok")

        # full rebuild: reativa um id de BETA (que ja existe em GAMA) e reconstroi so a
        # entidade alfa -- tem que refletir a mudanca por completo, sem sobrar nada da
        # versao anterior (sem acumular arquivo por build, diferente da raw)
        silver_raw_tables["BETA"].loc[silver_raw_tables["BETA"]["id"] == 9, "ativo"] = True
        silver_raw_tables["BETA"].loc[silver_raw_tables["BETA"]["id"] == 9, "date_modified"] = (
            pd.Timestamp("2024-02-01") + pd.Timedelta(days=100)
        )
        extract_table(
            silver_query, SCHEMA, "BETA", ["id", "rotulo", "ativo", "date_modified"], raw_output_dir,
            config_dir=raw_config_dir, control_dir=raw_control_dir, column_types=beta_types,
        )
        build_entity(alfa_entity, raw_loader, silver_output_dir, SCHEMA, config_dir=silver_config_dir)

        df_alfa_v2 = TableLoader(silver_output_dir, schema=SCHEMA, config_dir=silver_config_dir)["alfa"]
        assert set(df_alfa_v2["id"]) == set(range(1, 10)), "id=9 reativado (e presente em GAMA) deveria entrar"
        alfa_files_depois = len(
            ds.dataset(os.path.join(silver_output_dir, SCHEMA, "alfa"), format="parquet").files
        )
        assert alfa_files_depois <= len(df_alfa_v2), (
            "rebuild completo escreve tudo numa unica passada: no maximo 1 arquivo por bucket "
            "tocado, nunca acumula arquivo de builds anteriores"
        )
        print(f"rebuild completo: alfa refeita do zero, agora com {len(df_alfa_v2)} linhas, sem acumular arquivos")

        # validacoes: numero de joins tem que bater com numero de fontes - 1
        try:
            build_entity(
                Entity(name="invalido", id_column="id", sources=[Source(table="BETA"), Source(table="GAMA")], joins=[]),
                raw_loader, silver_output_dir, SCHEMA, config_dir=silver_config_dir,
            )
            assert False, "deveria falhar por numero de joins incompativel com numero de fontes"
        except ValueError:
            pass

        # validacao: id_column tem que existir apos filtros/renomeios/merge
        try:
            build_entity(
                Entity(name="invalido2", id_column="coluna_inexistente", sources=[Source(table="XPTO")]),
                raw_loader, silver_output_dir, SCHEMA, config_dir=silver_config_dir,
            )
            assert False, "deveria falhar por id_column inexistente"
        except ValueError:
            pass
        print("build_entity valida joins incompativeis e id_column inexistente")

        print("\nOK: smoke test passou")
    finally:
        shutil.rmtree(base_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
