"""Camada silver: filtra colunas/linhas, renomeia e junta tabelas da raw em
entidades e relacionamentos — tudo com pandas puro (`rename`/`query`/`merge`),
sem motor de execução próprio.

`build_entity` cobre o caso direto (o que já está declarado em `Source`/`Join`
é suficiente); `load_entity`/`save_entity` separam a leitura+merge da gravação,
pra quando é preciso selecionar colunas, filtrar linhas, ajustar valores ou
renomear com pandas puro entre uma coisa e outra. Nos três casos, a entidade
inteira é reconstruída a partir do estado atual da raw (já incremental e
dedupada pelo `TableLoader`) e o parquet é regravado do zero, particionado por
bucket com o mesmo esquema hash da raw — sem carga incremental nem checkpoint
próprios aqui. É mais simples, e as entidades (a configuração de quais
tabelas/colunas/joins formam cada uma) mudam raro; os dados por trás delas é
que crescem, e cada build já parte da raw mais recente.
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
from i3_shift_lake.extract import _write_page, bucket_for, load_table_query, table_query_path
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


def load_entity(entity: Entity, raw: TableLoader) -> pd.DataFrame:
    """Executa só a parte de leitura+merge de uma entidade (sem gravar nada): lê as
    fontes na raw (via `TableLoader`, já incremental/dedupada), renomeia/filtra cada
    uma (`Source.rename`/`Source.where`) e mescla na sequência declarada (`Entity.joins`).

    Devolve o DataFrame pronto pra você continuar com pandas puro — selecionar
    colunas, filtrar linhas, ajustar valores, renomear — antes de persistir com
    `save_entity`. Pra entidades que não precisam de nada além do que já está
    declarado em `Source`/`Join`, use `build_entity` (que já persiste também).
    """
    if len(entity.joins) != len(entity.sources) - 1:
        raise ValueError(
            f"entidade '{entity.name}': esperava {len(entity.sources) - 1} join(s) para "
            f"{len(entity.sources)} fonte(s), recebeu {len(entity.joins)}"
        )

    df = _load_source(entity.sources[0], raw)
    for source, join in zip(entity.sources[1:], entity.joins):
        right_df = _load_source(source, raw)
        df = df.merge(right_df, left_on=join.left_on, right_on=join.right_on, how=join.how)

    if entity.columns is not None:
        df = df[list(entity.columns)]

    return df


def save_entity(
    df: pd.DataFrame,
    name: str,
    id_column: str,
    output_dir: str,
    schema: str,
    config_dir: str = DEFAULT_CONFIG_DIR,
    num_buckets: int = 32,
) -> dict:
    """Grava um DataFrame já pronto (de `load_entity` + pandas, ou de qualquer outra
    origem) como parquet particionado por bucket em `id_column` — substituindo por
    completo o que existia (sem acumular arquivo por rodada, como a raw). Mesmo
    formato físico da raw, então é lido de volta com o `TableLoader` normalmente.

    A escrita vai primeiro para um diretório temporário e só troca de lugar com o
    anterior depois de terminar com sucesso — se algo falhar no meio do caminho, a
    versão anterior continua intacta.
    """
    if id_column not in df.columns:
        raise ValueError(f"'{name}': id_column '{id_column}' não existe no DataFrame (colunas: {list(df.columns)})")

    start = time.perf_counter()
    table_dir = _table_path(output_dir, schema, name)
    tmp_dir = f"{table_dir}.tmp-{uuid.uuid4().hex}"
    os.makedirs(tmp_dir, exist_ok=True)
    _write_page(df, tmp_dir, id_column, num_buckets)
    if os.path.isdir(table_dir):
        shutil.rmtree(table_dir)
    os.rename(tmp_dir, table_dir)

    path = table_query_path(schema, name, config_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"order_by": [id_column], "partition_column": id_column}, f, ensure_ascii=False, indent=2)

    stats = {"entity_name": name, "rows": len(df), "columns": len(df.columns)}
    print(
        f"[save_entity] {schema}.{name}: {stats['rows']} linha(s), "
        f"{stats['columns']} coluna(s) ({time.perf_counter() - start:.1f}s)",
        flush=True,
    )
    return stats


def build_entity(
    entity: Entity,
    raw: TableLoader,
    output_dir: str,
    schema: str,
    config_dir: str = DEFAULT_CONFIG_DIR,
    num_buckets: int = 32,
) -> dict:
    """Atalho: `load_entity` + `save_entity`, sem etapa de pandas no meio — pra
    entidades que não precisam de nada além do filtro/rename/merge já declarados
    em `Source`/`Join`. Veja `load_entity`/`save_entity` se precisar processar o
    DataFrame com pandas entre a leitura e a gravação."""
    print(f"[build_entity] {schema}.{entity.name}: lendo {len(entity.sources)} fonte(s)...", flush=True)
    df = load_entity(entity, raw)
    if entity.id_column not in df.columns:
        raise ValueError(
            f"entidade '{entity.name}': id_column '{entity.id_column}' não existe após "
            f"filtros/renomeios/merge (colunas disponíveis: {list(df.columns)})"
        )
    return save_entity(df, entity.name, entity.id_column, output_dir, schema, config_dir, num_buckets)


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


class SilverModel:
    """Atalho declarativo para montar entidades/relacionamentos silver a partir da
    raw, sem repetir `id_column`/`entity_column` — eles são derivados automaticamente
    do `partition_column` que a própria raw já persistiu (`load_table_query`) e do
    `id_column` das entidades já registradas neste `SilverModel`.

    Uso em notebook:
        modelo = SilverModel(raw)  # raw = TableLoader apontado pro output da raw

        modelo.entidade("xpto", "XPTO")
        modelo.entidade("alfa", "BETA", "BETA_c")  # merge com a tabela de campos custom
        modelo.relacionamento("rel", "xpto", "alfa", "X", "xpto_id", "alfa_id")

        # sem processamento extra: monta e grava tudo de uma vez
        modelo.build_all(SILVER_OUTPUT_DIR, SCHEMA, config_dir=SILVER_CONFIG_DIR)

        # com processamento extra (filtro/selecao/ajuste/rename com pandas puro
        # entre a leitura e a gravacao):
        df = modelo.carregar("alfa")
        df = df[df["ativo"]][["id", "rotulo", "pontuacao"]].rename(columns={"rotulo": "nome"})
        modelo.persistir("alfa", df, SILVER_OUTPUT_DIR, SCHEMA, config_dir=SILVER_CONFIG_DIR)
    """

    def __init__(self, raw: TableLoader, raw_config_dir: str | None = None):
        self.raw = raw
        self.raw_config_dir = raw_config_dir or raw.config_dir
        self.entities: dict[str, Entity] = {}

    def _raw_table_query(self, table: str) -> dict:
        try:
            return load_table_query(self.raw.schema, table, self.raw_config_dir)
        except FileNotFoundError as exc:
            raise ValueError(
                f"tabela raw '{table}' não tem config persistida em '{self.raw_config_dir}' — "
                f"rode extract_table()/extract_all() para ela antes de declarar a entidade"
            ) from exc

    def _get_entity(self, nome: str) -> Entity:
        if nome not in self.entities:
            raise ValueError(
                f"entidade '{nome}' não foi registrada — chame .entidade(...) ou "
                f".relacionamento(...) para ela antes"
            )
        return self.entities[nome]

    def entidade(
        self,
        nome_da_entidade: str,
        nome_da_tabela_raw: str,
        nome_da_tabela_raw_custom: str | None = None,
    ) -> Entity:
        """Entidade silver = 1 tabela raw (passthrough) ou 2 tabelas raw mescladas por id
        (`nome_da_tabela_raw_custom`, ex.: a tabela de campos customizados de um objeto
        Salesforce) — a tabela custom deixa de existir como conceito separado na silver,
        vira só colunas extras da entidade. Colunas com o mesmo nome nas duas tabelas
        (exceto a própria chave de junção) recebem o sufixo `_custom` automaticamente,
        pra nunca colidir sem avisar."""
        id_column = self._raw_table_query(nome_da_tabela_raw)["partition_column"]

        if nome_da_tabela_raw_custom is None:
            entity = Entity(name=nome_da_entidade, id_column=id_column, sources=[Source(table=nome_da_tabela_raw)])
        else:
            base_cfg = self._raw_table_query(nome_da_tabela_raw)
            custom_cfg = self._raw_table_query(nome_da_tabela_raw_custom)
            custom_id_column = custom_cfg["partition_column"]
            colisao = (set(base_cfg["columns"]) & set(custom_cfg["columns"])) - {custom_id_column}
            rename = {c: f"{c}_custom" for c in colisao}
            entity = Entity(
                name=nome_da_entidade,
                id_column=id_column,
                sources=[
                    Source(table=nome_da_tabela_raw),
                    Source(table=nome_da_tabela_raw_custom, rename=rename),
                ],
                joins=[Join(left_on=id_column, right_on=rename.get(custom_id_column, custom_id_column), how="left")],
            )

        self.entities[nome_da_entidade] = entity
        return entity

    def relacionamento(
        self,
        nome_relacionamento: str,
        nome_da_entidade_a: str,
        nome_da_entidade_b: str,
        nome_da_tabela_rel_raw: str,
        nome_fk_a: str,
        nome_fk_b: str,
    ) -> Entity:
        """Relacionamento silver = 1 tabela raw, vinculando duas entidades já
        registradas neste `SilverModel` (via `entidade()`) por suas colunas de FK."""
        entidade_a = self._get_entity(nome_da_entidade_a)
        entidade_b = self._get_entity(nome_da_entidade_b)

        id_column = self._raw_table_query(nome_da_tabela_rel_raw)["partition_column"]
        entity = Entity(
            name=nome_relacionamento,
            id_column=id_column,
            sources=[Source(table=nome_da_tabela_rel_raw)],
            foreign_keys=[
                ForeignKey(column=nome_fk_a, entity=nome_da_entidade_a, entity_column=entidade_a.id_column),
                ForeignKey(column=nome_fk_b, entity=nome_da_entidade_b, entity_column=entidade_b.id_column),
            ],
        )
        self.entities[nome_relacionamento] = entity
        return entity

    def carregar(self, nome_da_entidade: str) -> pd.DataFrame:
        """Executa o load+merge da entidade/relacionamento já registrado (sem
        gravar nada) — devolve o DataFrame pronto pra você continuar filtrando/
        selecionando/ajustando/renomeando com pandas puro antes de `.persistir(...)`."""
        return load_entity(self._get_entity(nome_da_entidade), self.raw)

    def persistir(
        self,
        nome_da_entidade: str,
        df: pd.DataFrame,
        output_dir: str,
        schema: str,
        config_dir: str = DEFAULT_CONFIG_DIR,
        num_buckets: int = 32,
        id_column: str | None = None,
    ) -> dict:
        """Grava o DataFrame já processado (de `.carregar(...)` + pandas) como parquet
        particionado por id. Usa o `id_column` da entidade já registrada por padrão;
        passe `id_column` explicitamente se o seu processamento renomeou essa coluna."""
        entity = self._get_entity(nome_da_entidade)
        return save_entity(df, nome_da_entidade, id_column or entity.id_column, output_dir, schema, config_dir, num_buckets)

    def build_all(self, output_dir: str, schema: str, config_dir: str = DEFAULT_CONFIG_DIR, num_buckets: int = 32) -> pd.DataFrame:
        """Materializa todas as entidades/relacionamentos registrados, na ordem em
        que foram declarados (`entidade`/`relacionamento`)."""
        return build_all(list(self.entities.values()), self.raw, output_dir, schema, config_dir, num_buckets)

    def check_foreign_keys(self, silver: TableLoader) -> pd.DataFrame:
        """Roda `check_foreign_key` para cada FK declarada nos relacionamentos registrados."""
        return check_foreign_keys(list(self.entities.values()), silver)
