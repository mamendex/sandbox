"""Baixa datasets públicos (Banco Mundial, via github.com/datasets) e materializa em um
DuckDB local, que serve de fonte de dados para o i3_shift_lake através de `query(sql)`.

DuckDB fala o mesmo dialeto SQL que o i3_shift_lake já usa (information_schema.columns,
identificadores entre aspas duplas, LIMIT/OFFSET), então nenhum código do pacote precisa
mudar — só trocamos o `query()` que é passado para `discover`/`extract_all`.
"""

from __future__ import annotations

import os

import duckdb
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(HERE, "raw")
DB_PATH = os.path.join(HERE, "warehouse.duckdb")
SCHEMA = "worldbank"

# Banco Mundial (World Bank Open Data, CC-BY-4.0), espelhado em CSV por github.com/datasets.
SOURCES = {
    "population": "https://raw.githubusercontent.com/datasets/population/master/data/population.csv",
    "gdp": "https://raw.githubusercontent.com/datasets/gdp/master/data/gdp.csv",
}


def download_csv(name: str, url: str, force: bool = False) -> str:
    """Baixa um CSV público uma vez e guarda em `raw/` (evita rebaixar em toda execução)."""
    os.makedirs(RAW_DIR, exist_ok=True)
    path = os.path.join(RAW_DIR, f"{name}.csv")
    if force or not os.path.isfile(path):
        print(f"[source] baixando {name} de {url}...", flush=True)
        df = pd.read_csv(url)
        df.to_csv(path, index=False)
        print(f"[source] {name}: {len(df)} linhas salvas em {path}", flush=True)
    else:
        print(f"[source] {name}: usando cache local em {path}", flush=True)
    return path


def build_database(force_download: bool = False, db_path: str = DB_PATH, year_cutoff: int | None = None) -> str:
    """(Re)cria o DuckDB local a partir dos CSVs públicos, com colunas `id`/`date_modified`
    (a fonte original não tem essas colunas de controle — são adicionadas aqui para o
    i3_shift_lake poder ordenar/particionar/retomar como faria numa tabela do Redshift).

    `year_cutoff`, se informado, carrega só os anos até esse valor — útil para simular uma
    carga inicial parcial e, depois, a chegada de dados novos com `append_new_years()`.
    """
    con = duckdb.connect(db_path)
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    year_filter = f'WHERE "Year" <= {year_cutoff}' if year_cutoff is not None else ""
    for name, url in SOURCES.items():
        csv_path = download_csv(name, url, force=force_download)
        con.execute(f"DROP TABLE IF EXISTS {SCHEMA}.{name}")
        con.execute(f"""
            CREATE TABLE {SCHEMA}.{name} AS
            SELECT
                ROW_NUMBER() OVER () AS id,
                "Country Name" AS country_name,
                "Country Code" AS country_code,
                "Year" AS year,
                "Value" AS value,
                CURRENT_TIMESTAMP AS date_modified
            FROM read_csv_auto('{csv_path}')
            {year_filter}
        """)
        count = con.execute(f"SELECT COUNT(*) FROM {SCHEMA}.{name}").fetchone()[0]
        print(f"[source] tabela {SCHEMA}.{name} criada com {count} linhas", flush=True)
    con.close()
    return db_path


def append_new_years(min_year: int, db_path: str = DB_PATH) -> dict[str, int]:
    """Simula a chegada de dados novos na fonte: insere as linhas de `raw/*.csv` com
    `Year >= min_year` que ainda não estão na tabela, com `date_modified` = agora (mais
    recente que o restante) — para exercitar a carga incremental do i3_shift_lake."""
    con = duckdb.connect(db_path)
    inserted = {}
    for name in SOURCES:
        csv_path = os.path.join(RAW_DIR, f"{name}.csv")
        before = con.execute(f"SELECT COUNT(*) FROM {SCHEMA}.{name}").fetchone()[0]
        max_id = con.execute(f"SELECT COALESCE(MAX(id), 0) FROM {SCHEMA}.{name}").fetchone()[0]
        con.execute(f"""
            INSERT INTO {SCHEMA}.{name}
            SELECT
                {max_id} + ROW_NUMBER() OVER () AS id,
                c."Country Name" AS country_name,
                c."Country Code" AS country_code,
                c."Year" AS year,
                c."Value" AS value,
                CURRENT_TIMESTAMP AS date_modified
            FROM read_csv_auto('{csv_path}') c
            WHERE c."Year" >= {min_year}
              AND NOT EXISTS (
                  SELECT 1 FROM {SCHEMA}.{name} t
                  WHERE t.country_code = c."Country Code" AND t.year = c."Year"
              )
        """)
        after = con.execute(f"SELECT COUNT(*) FROM {SCHEMA}.{name}").fetchone()[0]
        inserted[name] = after - before
        print(f"[source] {name}: +{after - before} linhas novas (Year >= {min_year})", flush=True)
    con.close()
    return inserted


def get_query_fn(db_path: str = DB_PATH):
    """Devolve `query(sql) -> DataFrame`, cada chamada abrindo/fechando sua própria conexão
    DuckDB (somente leitura) — evita conflitar com uma conexão de escrita concorrente (ex.:
    `append_new_years` rodando entre duas extrações, simulando dados novos chegando)."""

    def query(sql: str) -> pd.DataFrame:
        con = duckdb.connect(db_path, read_only=True)
        try:
            return con.execute(sql).fetchdf()
        finally:
            con.close()

    return query
