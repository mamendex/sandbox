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

import json
import os
import re
import shutil
import sys
import tempfile
from functools import partial

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from i3_shift_lake import (  # noqa: E402
    ping, discover, extract_all, load_model, load_table_query, TableLoader,
    check_row_counts, check_duplicate_ids, check_duplicates_all,
    check_unique, check_foreign_key, run_checks,
    load_checkpoint,
)

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


def _apply_resume_where(df: pd.DataFrame, cols: list[str], raw_values: list[str]) -> pd.DataFrame:
    """Simula `WHERE (cols) > (values)` (comparação por tupla) para o teste do checkpoint."""
    mask = pd.Series(False, index=df.index)
    still_equal = pd.Series(True, index=df.index)
    for col, raw in zip(cols, raw_values):
        series = df[col]
        raw = raw.split("::")[0].strip()  # remove cast explicito, ex: '...'::timestamp
        if pd.api.types.is_datetime64_any_dtype(series):
            val = pd.Timestamp(raw.strip("'"))
        elif raw.startswith("'"):
            val = raw.strip("'")
        else:
            val = int(raw)
        mask = mask | (still_equal & (series > val))
        still_equal = still_equal & (series == val)
    return df[mask]


def fake_query(sql: str) -> pd.DataFrame:
    if sql.strip() == "SELECT 1 AS ok":
        return pd.DataFrame({"ok": [1]})
    if "information_schema.columns" in sql:
        return COLUMNS_DF
    table = re.search(r'FROM "[^"]+"\."([^"]+)"', sql).group(1)
    if sql.strip().upper().startswith("SELECT COUNT(*)"):
        return pd.DataFrame({"row_count": [len(FAKE_TABLES[table])]})

    df = FAKE_TABLES[table]
    where_match = re.search(r"WHERE \(([^)]+)\) > \(([^)]+)\)", sql)
    if where_match:
        cols = [c.strip().strip('"') for c in where_match.group(1).split(",")]
        raw_values = [v.strip() for v in where_match.group(2).split(",")]
        df = _apply_resume_where(df, cols, raw_values)

    # query paginada: SELECT cols FROM "schema"."table" [WHERE ...] ORDER BY ... LIMIT n OFFSET m
    limit = int(sql.split("LIMIT")[1].split("OFFSET")[0].strip())
    offset = int(sql.split("OFFSET")[1].strip())
    order_by_part = sql.split("ORDER BY")[1].split("LIMIT")[0].strip()
    order_cols = [c.strip().strip('"') for c in order_by_part.split(",")]
    sorted_df = df.sort_values(order_cols)
    return sorted_df.iloc[offset : offset + limit].reset_index(drop=True)


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
        loader = TableLoader(output_dir, schema=SCHEMA)
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

        sample_loader = TableLoader(sample_output_dir, schema=SCHEMA)
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
        df_accounts_full = loader.load("accounts")  # agora tem a duplicata proposital
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

        print("\n=== carga incremental: 2a rodada sem dados novos (deve trazer 0 linhas) ===")
        r2 = extract_all(
            fake_query, model, incr_output_dir, page_size=30, num_buckets=4,
            config_dir=incr_config_dir, control_dir=incr_control_dir,
        )
        print(r2.to_string())
        assert (r2["rows_read"] == 0).all()
        assert (r2["pages"] == 0).all()

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

        incr_loader = TableLoader(incr_output_dir, schema=SCHEMA)
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

        print("\nOK: smoke test passou")
    finally:
        shutil.rmtree(base_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
