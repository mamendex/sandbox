"""Camada silver: filtra colunas/linhas, renomeia e junta tabelas da raw em
entidades e relacionamentos — tudo com pandas puro (`rename`/`query`/`merge`),
sem motor de execução próprio.

Cada `build_entity` reconstrói a entidade inteira a partir do estado atual da
raw (já incremental e dedupada pelo `TableLoader`) e regrava o parquet do
zero, particionado por bucket com o mesmo esquema hash da raw — sem carga
incremental nem checkpoint próprios aqui. É mais simples, e as entidades
(a configuração de quais tabelas/colunas/joins formam cada uma) mudam raro;
os dados por trás delas é que crescem, e cada build já parte da raw mais
recente.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field

import pandas as pd

from i3_shift_lake.discover import DEFAULT_CONFIG_DIR
from i3_shift_lake.extract import _write_page, bucket_for, table_query_path
from i3_shift_lake.transform import TableLoader
from i3_shift_lake.validate import CheckResult, check_foreign_key


@dataclass
class Source:
    """Uma tabela raw usada para montar uma entidade/relacionamento silver."""

    table: str
    columns: list[str] | None = None  # filtro de colunas (None = todas); deve incluir
    # qualquer coluna referenciada em `where` e a(s) coluna(s) de join
    rename: dict[str, str] = field(default_factory=dict)  # renomeia ANTES do merge —
    # use para resolver colisao de nomes entre fontes (ex.: "date_modified" nas duas)
    where: str | None = None  # filtro de linhas: string de DataFrame.query(), sobre
    # os nomes de coluna ORIGINAIS da raw (antes do rename)


@dataclass
class Join:
    """Como mesclar a próxima fonte na sequência — mesmo vocabulário do `pd.merge`."""

    left_on: str
    right_on: str
    how: str = "inner"


@dataclass
class ForeignKey:
    """Documenta que uma coluna desta entidade referencia outra entidade silver.
    Usado por `check_foreign_keys` para validar automaticamente."""

    column: str
    entity: str
    entity_column: str = "id"


@dataclass
class Entity:
    """Uma entidade (ou relacionamento — é a mesma coisa aqui) da camada silver."""

    name: str
    id_column: str  # coluna final (apos rename/merge) usada para particionar o parquet
    sources: list[Source]  # a 1a e a base; as demais sao mescladas em sequencia
    joins: list[Join] = field(default_factory=list)  # len(joins) == len(sources) - 1
    columns: list[str] | None = None  # selecao final de colunas (None = todas)
    foreign_keys: list[ForeignKey] = field(default_factory=list)


def _load_source(source: Source, raw: TableLoader) -> pd.DataFrame:
    df = raw.load(source.table, columns=source.columns)
    if source.where:
        df = df.query(source.where)
    if source.rename:
        df = df.rename(columns=source.rename)
    return df


def _table_path(output_dir: str, schema: str, name: str) -> str:
    return os.path.join(output_dir, schema, name)


def _save_entity_query(schema: str, entity: Entity, config_dir: str) -> None:
    """Persiste so o que `TableLoader.load` precisa pra ler a entidade de volta
    (`order_by`/`partition_column`), no mesmo arquivo/formato que `save_table_query`
    usa na raw — sem os campos que so fazem sentido la (query_template, sample_size,
    load_mode)."""
    path = table_query_path(schema, entity.name, config_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"order_by": [entity.id_column], "partition_column": entity.id_column}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def build_entity(
    entity: Entity,
    raw: TableLoader,
    output_dir: str,
    schema: str,
    config_dir: str = DEFAULT_CONFIG_DIR,
    num_buckets: int = 32,
) -> dict:
    """Reconstrói uma entidade silver do zero: lê as fontes na raw (via
    `TableLoader`, já incremental/dedupada), filtra, renomeia, mescla e grava o
    parquet particionado por bucket — substituindo por completo o que existia
    (sem acumular arquivo por rodada como a raw: aqui não há incremental).

    A escrita vai primeiro para um diretório temporário e só troca de lugar com
    o anterior depois de terminar com sucesso — se algo falhar no meio do
    caminho, a versão anterior da entidade continua intacta.
    """
    if len(entity.joins) != len(entity.sources) - 1:
        raise ValueError(
            f"entidade '{entity.name}': esperava {len(entity.sources) - 1} join(s) para "
            f"{len(entity.sources)} fonte(s), recebeu {len(entity.joins)}"
        )

    start = time.perf_counter()
    print(f"[build_entity] {schema}.{entity.name}: lendo {len(entity.sources)} fonte(s)...", flush=True)

    df = _load_source(entity.sources[0], raw)
    for source, join in zip(entity.sources[1:], entity.joins):
        right_df = _load_source(source, raw)
        df = df.merge(right_df, left_on=join.left_on, right_on=join.right_on, how=join.how)

    if entity.columns is not None:
        df = df[list(entity.columns)]

    if entity.id_column not in df.columns:
        raise ValueError(
            f"entidade '{entity.name}': id_column '{entity.id_column}' não existe após "
            f"filtros/renomeios/merge (colunas disponíveis: {list(df.columns)})"
        )

    table_dir = _table_path(output_dir, schema, entity.name)
    tmp_dir = f"{table_dir}.tmp-{uuid.uuid4().hex}"
    os.makedirs(tmp_dir, exist_ok=True)
    _write_page(df, tmp_dir, entity.id_column, num_buckets)
    if os.path.isdir(table_dir):
        shutil.rmtree(table_dir)
    os.rename(tmp_dir, table_dir)

    _save_entity_query(schema, entity, config_dir)

    stats = {
        "entity_name": entity.name,
        "rows": len(df),
        "columns": len(df.columns),
    }
    print(
        f"[build_entity] {schema}.{entity.name}: {stats['rows']} linha(s), "
        f"{stats['columns']} coluna(s) ({time.perf_counter() - start:.1f}s)",
        flush=True,
    )
    return stats


def build_all(
    entities: list[Entity],
    raw: TableLoader,
    output_dir: str,
    schema: str,
    config_dir: str = DEFAULT_CONFIG_DIR,
    num_buckets: int = 32,
) -> pd.DataFrame:
    """Roda `build_entity` para cada entidade da lista, na ordem dada — entidades
    que dependem de outras entidades silver (raro, mas possível compondo `raw`
    com um `TableLoader` apontado pro próprio output da silver) devem vir depois
    das que elas referenciam."""
    rows = [
        build_entity(entity, raw, output_dir, schema, config_dir=config_dir, num_buckets=num_buckets)
        for entity in entities
    ]
    return pd.DataFrame(rows, columns=["entity_name", "rows", "columns"])


def check_foreign_keys(entities: list[Entity], silver: TableLoader) -> pd.DataFrame:
    """Roda `check_foreign_key` (`validate.py`) para cada `ForeignKey` declarada
    nas entidades, usando os dados já materializados na silver."""
    cache: dict[str, pd.DataFrame] = {}

    def _get(name: str) -> pd.DataFrame:
        if name not in cache:
            cache[name] = silver.load(name)
        return cache[name]

    results: list[CheckResult] = []
    for entity in entities:
        if not entity.foreign_keys:
            continue
        df = _get(entity.name)
        for fk in entity.foreign_keys:
            ref_df = _get(fk.entity)
            results.append(check_foreign_key(df, fk.column, ref_df, fk.entity_column, entity.name))
    return pd.DataFrame([r.__dict__ for r in results])
