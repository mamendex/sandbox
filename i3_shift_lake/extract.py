"""Extração paginada de tabelas para parquet particionado por bucket."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time

import pandas as pd

from i3_shift_lake.checkpoint import DEFAULT_CONTROL_DIR, clear_checkpoint, load_checkpoint, save_checkpoint
from i3_shift_lake.discover import DEFAULT_CONFIG_DIR, TableModel
from i3_shift_lake.query import QueryFn

# Ordem de prioridade (coluna de data, coluna de id) usada para ORDER BY e paginação.
_ORDER_PRIORITY = [
    ("date_modified", "id"),
    ("date_created", "id"),
    ("date_modified", "id_c"),
    ("date_created", "id_c"),
]

# Mapeia o data_type do Redshift (information_schema.columns) para um dtype pandas.
# Aplicado em toda página antes de gravar, para fixar o schema de cada coluna e
# evitar que uma página com uma coluna inteiramente nula (ex.: texto opcional só
# com None) seja inferida pelo pyarrow como tipo `null`, incompatível com o
# `large_string`/`int64`/etc. das demais páginas na hora de ler o dataset completo.
_REDSHIFT_TO_PANDAS_DTYPE = {
    "smallint": "Int64",
    "int2": "Int64",
    "integer": "Int64",
    "int": "Int64",
    "int4": "Int64",
    "bigint": "Int64",
    "int8": "Int64",
    "decimal": "float64",
    "numeric": "float64",
    "real": "float64",
    "float4": "float64",
    "double precision": "float64",
    "float8": "float64",
    "float": "float64",
    "boolean": "boolean",
    "bool": "boolean",
    "char": "string",
    "character": "string",
    "nchar": "string",
    "bpchar": "string",
    "varchar": "string",
    "character varying": "string",
    "nvarchar": "string",
    "text": "string",
    "date": "datetime64[ns]",
    "timestamp": "datetime64[ns]",
    "timestamp without time zone": "datetime64[ns]",
    "timestamptz": "datetime64[ns]",
    "timestamp with time zone": "datetime64[ns]",
}


def _target_dtype(redshift_type: str) -> str | None:
    key = redshift_type.strip().lower().split("(")[0].strip()
    return _REDSHIFT_TO_PANDAS_DTYPE.get(key)


def coerce_dtypes(df: pd.DataFrame, column_types: dict[str, str]) -> pd.DataFrame:
    """Fixa o dtype de cada coluna conforme o `data_type` do Redshift (via `TableModel.column_types`).

    Colunas cujo tipo não está mapeado, ou cuja conversão falhe, ficam como o
    pandas inferiu — a coerção é best-effort, não uma validação de schema.
    """
    for column, redshift_type in column_types.items():
        target = _target_dtype(redshift_type)
        if target is None or column not in df.columns:
            continue
        try:
            df[column] = df[column].astype(target)
        except (TypeError, ValueError):
            pass
    return df


def _format_literal(value, redshift_type: str | None) -> str:
    """Formata um valor de checkpoint como literal SQL (com aspas para texto/data).

    Datas/timestamps levam um cast explícito (`::timestamp`) em vez de depender de cast
    implícito de string dentro da comparação por tupla — o Redshift aceita normalmente,
    e alguns motores (ex.: DuckDB) exigem o cast explícito nesse contexto.

    Um valor nulo (None/NaN/NaT) vira o literal SQL `NULL` — nunca um `str(value)` sem
    aspas (isso gerava `column "none" does not exist", pois um `None` bruto na query é
    lido como um identificador, não como literal).
    """
    if pd.isna(value):
        return "NULL"
    target = _target_dtype(redshift_type) if redshift_type else None
    if target == "datetime64[ns]":
        escaped = str(value).replace("'", "''")
        return f"'{escaped}'::timestamp"
    if target == "string" or (target is None and isinstance(value, str)):
        escaped = str(value).replace("'", "''")
        return f"'{escaped}'"
    return str(value)


def build_resume_filter(order_by: list[str], last_values: list, column_types: dict[str, str]) -> str:
    """Monta o `WHERE (colunas) > (valores)` para retomar de um checkpoint (comparação por tupla:
    continua exatamente depois da última linha extraída, sem pular nem duplicar linhas com o
    mesmo valor de data quando a ordenação é data+id)."""
    cols_sql = ", ".join(f'"{c}"' for c in order_by)
    literals_sql = ", ".join(_format_literal(v, column_types.get(c)) for c, v in zip(order_by, last_values))
    return f"({cols_sql}) > ({literals_sql})"


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


def build_query_template(
    schema: str, table: str, columns: list[str], order_by: list[str], where: str | None = None
) -> str:
    """Monta a query de select da tabela, com placeholders `{page_size}`/`{offset}` para paginação.

    `where`, quando informado (ver `build_resume_filter`), filtra a partir de um checkpoint
    para retomar uma carga incremental.
    """
    cols_sql = ", ".join(f'"{c}"' for c in columns)
    order_sql = ", ".join(f'"{c}"' for c in order_by)
    where_sql = f"WHERE {where} " if where else ""
    return (
        f'SELECT {cols_sql} FROM "{schema}"."{table}" '
        f"{where_sql}"
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
    load_mode: str = "incremental",
    resumed_from: list | None = None,
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
                "load_mode": load_mode,
                "resumed_from": resumed_from,
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


def _format_position(values) -> str:
    """Formata os valores de `order_by` (dict ou lista) de forma compacta para log:
    datas viram `YYYYMMDDHHMMSS`, o resto vira `str(valor)`, sem nomes de coluna —
    só para caber numa linha de progresso; o relatório continua com o dict completo."""
    items = values.values() if isinstance(values, dict) else values
    parts = []
    for v in items:
        if pd.isna(v):
            parts.append("None")
        elif hasattr(v, "strftime"):
            parts.append(v.strftime("%Y%m%d%H%M%S"))
        else:
            parts.append(str(v))
    return "{'" + ",".join(parts) + "'}"


def extract_table(
    query: QueryFn,
    schema: str,
    table: str,
    columns: list[str],
    output_dir: str,
    page_size: int = 50_000,
    num_buckets: int = 32,
    config_dir: str = DEFAULT_CONFIG_DIR,
    sample_size: int | None = None,
    column_types: dict[str, str] | None = None,
    load_mode: str = "incremental",
    control_dir: str = DEFAULT_CONTROL_DIR,
) -> dict:
    """Extrai uma tabela de forma paginada e grava parquet particionado por bucket.

    `load_mode`:
    - "incremental" (default): retoma do checkpoint salvo em `<control_dir>/<schema>/<table>/`
      (últimos valores de `order_by` da carga anterior), lendo só as linhas novas dali em
      diante. Sem checkpoint prévio, equivale a uma carga full.
    - "full": ignora/descarta checkpoint e dados já extraídos dessa tabela em `output_dir`
      e recomeça do zero.
    Em ambos os casos, um novo checkpoint é salvo ao final com o último valor lido.

    Com `sample_size`, extrai só as primeiras `sample_size` linhas (a partir do ponto de
    retomada) em vez da tabela/incremento inteiro — útil para testes/dev.

    `column_types` (de `TableModel.column_types[table]`) fixa o dtype de cada coluna em
    toda página, evitando schemas inconsistentes entre parquets por causa de páginas com
    colunas inteiramente nulas.

    A query de select usada (com a ordenação/particionamento/filtro de retomada
    escolhidos) é persistida em `<config_dir>/<schema>/<table>.json`.
    """
    if load_mode not in ("full", "incremental"):
        raise ValueError(f"load_mode inválido: {load_mode!r} (use 'full' ou 'incremental')")

    order_by, partition_col = resolve_key(columns)
    column_types = column_types or {}
    table_dir = os.path.join(output_dir, schema, table)

    resume_from = None
    if load_mode == "full":
        shutil.rmtree(table_dir, ignore_errors=True)
        clear_checkpoint(schema, table, control_dir)
    else:
        checkpoint = load_checkpoint(schema, table, control_dir)
        if checkpoint and checkpoint.get("order_by") == order_by:
            resume_from = checkpoint["last_values"]
        elif checkpoint:
            print(
                f"[extract_table] checkpoint de {schema}.{table} ignorado "
                f"(ordenacao mudou: {checkpoint.get('order_by')} -> {order_by})",
                flush=True,
            )

    where = build_resume_filter(order_by, resume_from, column_types) if resume_from else None
    query_template = build_query_template(schema, table, columns, order_by, where)
    save_table_query(
        schema, table, columns, order_by, partition_col, query_template,
        config_dir, sample_size, load_mode, resume_from,
    )

    os.makedirs(table_dir, exist_ok=True)

    start = time.perf_counter()
    print(
        f"[extract_table] iniciando {schema}.{table} (load_mode={load_mode}"
        + (f", retomando {_format_position(resume_from)}" if resume_from else "")
        + (f", sample_size={sample_size}" if sample_size is not None else "")
        + ")",
        flush=True,
    )

    rows_read = 0
    pages = 0
    offset = 0
    last_row = None
    while sample_size is None or rows_read < sample_size:
        current_page_size = page_size if sample_size is None else min(page_size, sample_size - rows_read)
        sql = query_template.format(page_size=current_page_size, offset=offset)
        page_df = query(sql)
        if page_df.empty:
            break
        if column_types:
            page_df = coerce_dtypes(page_df, column_types)
        _write_page(page_df, table_dir, partition_col, num_buckets)
        rows_read += len(page_df)
        pages += 1
        offset += len(page_df)
        last_row = page_df.iloc[-1]
        posicao_atual = _format_position(last_row[c] for c in order_by)
        print(
            f"[extract_table] {schema}.{table}: pag {pages}, {rows_read} linhas, "
            f"posicao {posicao_atual} ({time.perf_counter() - start:.1f}s)",
            flush=True,
        )
        if len(page_df) < current_page_size:
            break  # ultima pagina (menos linhas do que pedido)

    if last_row is not None:
        # a carga avancou nesta rodada -> checkpoint novo (o ponto onde parou agora)
        stopped_at = {c: last_row[c] for c in order_by}
        if any(pd.isna(v) for v in stopped_at.values()):
            print(
                f"[extract_table] AVISO: {schema}.{table} parou com valor nulo em "
                f"{_format_position(stopped_at)} — comparação com NULL nunca é verdadeira em SQL, "
                f"a próxima carga incremental pode não trazer linha nova a partir daqui "
                f"(confira se essa coluna de ordenação pode mesmo ser nula na fonte)",
                flush=True,
            )
        save_checkpoint(schema, table, order_by, [last_row[c] for c in order_by], control_dir)
    elif resume_from is not None:
        # nenhuma linha nova nesta rodada -> segue parado onde estava antes
        stopped_at = dict(zip(order_by, resume_from))
    else:
        stopped_at = None

    print(
        f"[extract_table] {schema}.{table}: concluida {rows_read} linhas, {pages} pag, "
        f"parou {_format_position(stopped_at) if stopped_at else '-'} "
        f"({time.perf_counter() - start:.1f}s)",
        flush=True,
    )

    return {
        "table_name": table,
        "status": "ok",
        "rows_read": rows_read,
        "load_mode": load_mode,
        "sample_size": sample_size,
        "pages": pages,
        "order_by": ", ".join(order_by),
        "partition_column": partition_col,
        "stopped_at": stopped_at,
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
    load_mode: str = "incremental",
    control_dir: str = DEFAULT_CONTROL_DIR,
) -> pd.DataFrame:
    """Extrai todas as tabelas com ao menos uma linha e monta o relatório final.

    `model` pode vir de `discover()` (fluxo online) ou de `load_model()` (fluxo a
    partir da configuração salva em `config_dir`).

    `load_mode` ("incremental", default, ou "full") e `sample_size` são repassados
    a `extract_table` para cada tabela — ver a docstring de `extract_table`.
    """
    tables = model.tables_with_rows()
    start = time.perf_counter()
    print(
        f"[extract_all] iniciando extracao de {len(tables)} tabelas do schema {model.schema} "
        f"(load_mode={load_mode})",
        flush=True,
    )

    results = []
    for i, table in enumerate(tables, start=1):
        columns = model.tables[table]
        column_types = model.column_types.get(table, {})
        print(f"[extract_all] tabela {i}/{len(tables)}: {table}", flush=True)
        try:
            stats = extract_table(
                query, model.schema, table, columns, output_dir,
                page_size, num_buckets, config_dir, sample_size, column_types,
                load_mode, control_dir,
            )
        except ValueError as exc:
            stats = {
                "table_name": table,
                "status": f"erro: {exc}",
                "rows_read": 0,
                "load_mode": load_mode,
                "sample_size": sample_size,
                "pages": 0,
                "order_by": None,
                "partition_column": None,
                "stopped_at": None,
                "output_path": None,
            }
        results.append(stats)

    print(f"[extract_all] concluido em {time.perf_counter() - start:.1f}s", flush=True)

    return pd.DataFrame(
        results,
        columns=[
            "table_name", "status", "rows_read", "load_mode", "sample_size",
            "pages", "order_by", "partition_column", "stopped_at", "output_path",
        ],
    )
