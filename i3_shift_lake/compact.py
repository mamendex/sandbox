"""Compactação de tabelas: reduz o número de arquivos parquet acumulados por bucket.

Cada carga (rodada de extração) grava um arquivo parquet novo por bucket
tocado — nunca reescreve os arquivos já existentes (ver `extract._write_page`).
Isso é proposital: escrita barata e atômica, e dá pra reverter uma carga
problemática apagando só os arquivos daquela rodada. O custo é que o número
de arquivos por bucket só cresce com o tempo, mesmo que o volume de linhas
novas por carga seja pequeno.

`compact_table` junta, por bucket, os arquivos mais antigos que uma janela
de retenção (`min_age_hours`) num único arquivo. Arquivos mais novos que
isso nunca são tocados — de propósito, pra preservar a possibilidade de
reverter uma carga recente, que é justamente quando essa capacidade mais
importa.
"""

from __future__ import annotations

import os
import time
import uuid

import pandas as pd

from i3_shift_lake.discover import DEFAULT_CONFIG_DIR
from i3_shift_lake.extract import load_table_query


def _table_path(output_dir: str, schema: str, table: str) -> str:
    return os.path.join(output_dir, schema, table)


def compact_table(
    output_dir: str,
    schema: str,
    table: str,
    config_dir: str = DEFAULT_CONFIG_DIR,
    min_age_hours: float = 24.0,
    dedupe: bool = True,
    dry_run: bool = False,
) -> pd.DataFrame:
    """Junta, por bucket, os arquivos parquet mais antigos que `min_age_hours` num único
    arquivo. Devolve um relatório com uma linha por bucket efetivamente compactado
    (`files_before`/`files_after`, `rows_before`/`rows_after`); buckets sem nada a ganhar
    (0 arquivos elegíveis, ou já num único arquivo sem linha morta pra descartar) nem aparecem.

    Arquivos mais novos que `min_age_hours` nunca são lidos nem apagados — ficam de fora
    de propósito, para preservar a janela de rollback de uma carga recente.

    Com `dedupe=True` (default), ao juntar os arquivos elegíveis de um bucket, descarta
    linhas cuja versão (id + `order_by`) já foi substituída por uma versão mais nova em
    QUALQUER arquivo do bucket (antigo ou recente) — a mesma lógica que `TableLoader.load()`
    já aplica a cada leitura, só que aqui o resultado fica gravado em disco (permanente),
    reduzindo também o volume de dados morto (versões antigas de ids modificados, overlaps
    reais de paginação). Requer a config persistida da tabela (`order_by`/`partition_column`
    de `extract_table`); se não encontrar, cai para `dedupe=False` com aviso (mesmo padrão
    do `TableLoader.load`).

    Com `dry_run=True`, calcula e devolve o relatório do que aconteceria, sem escrever nem
    apagar nada — útil para conferir o impacto antes de rodar de verdade.
    """
    table_path = _table_path(output_dir, schema, table)
    if not os.path.isdir(table_path):
        raise FileNotFoundError(f"tabela '{table}' não encontrada em {table_path}")

    dedup_cols = winners = None
    if dedupe:
        try:
            table_cfg = load_table_query(schema, table, config_dir)
            order_by = table_cfg["order_by"]
            partition_column = table_cfg["partition_column"]
            dedup_cols = list(dict.fromkeys(order_by + [partition_column]))
            # calculado uma vez, para a tabela inteira (nao por bucket): um id sempre cai
            # no mesmo bucket em condicoes normais (mesmo hash), mas calcular por bucket
            # dependeria dessa invariante se sustentar entre escritas (ex.: num_buckets
            # nunca ter mudado) — global e mais barato (1 leitura em vez de 1 por bucket)
            # e nao depende disso.
            winners = (
                pd.read_parquet(table_path, columns=dedup_cols, engine="pyarrow")
                .sort_values(order_by)
                .drop_duplicates(subset=[partition_column], keep="last")[dedup_cols]
            )
        except FileNotFoundError:
            print(
                f"[compact_table] AVISO: config de {schema}.{table} não encontrada em "
                f"'{config_dir}' — compactando sem dedupe (só junta arquivos, sem descartar "
                f"versões substituídas)",
                flush=True,
            )
            dedupe = False

    buckets = sorted(name for name in os.listdir(table_path) if name.startswith("bucket="))
    cutoff = time.time() - min_age_hours * 3600
    start = time.perf_counter()
    print(
        f"[compact_table] {schema}.{table}: verificando {len(buckets)} bucket(s) "
        f"(min_age_hours={min_age_hours}, dedupe={dedupe}, dry_run={dry_run})",
        flush=True,
    )

    rows = []
    for bucket_name in buckets:
        bucket_dir = os.path.join(table_path, bucket_name)
        files = [f for f in os.listdir(bucket_dir) if f.endswith(".parquet")]
        old_files = [f for f in files if os.path.getmtime(os.path.join(bucket_dir, f)) <= cutoff]
        if not old_files:
            continue

        old_paths = [os.path.join(bucket_dir, f) for f in old_files]
        # pd.read_parquet numa lista de arquivos dentro de bucket=N/ reconstroi sozinho uma
        # coluna fisica "bucket" (infere particionamento hive pelo path) — os arquivos
        # originais do _write_page nunca tem essa coluna de verdade, só a pasta. Sem
        # derrubar aqui, o arquivo compactado gravaria "bucket" como coluna de dados, e o
        # pyarrow rejeita depois por ambiguidade de tipo (dado vs particao) ao ler a tabela.
        old_df = pd.read_parquet(old_paths, engine="pyarrow").drop(columns=["bucket"], errors="ignore")
        rows_before = len(old_df)
        files_before = len(old_files)

        if dedupe:
            # inner join mantem so as linhas cuja chave (order_by + id) e a vigente; ainda
            # precisa de um drop_duplicates por cima porque um overlap real (bug de
            # paginacao) tem 2+ linhas fisicas com a MESMA chave — o merge por chave
            # sozinho manteria as duas, ja que ele casa por igualdade, nao por identidade
            kept_df = old_df.merge(winners, on=dedup_cols, how="inner").drop_duplicates(
                subset=dedup_cols, keep="first"
            )
        else:
            kept_df = old_df

        rows_after = len(kept_df)
        files_after = 0 if kept_df.empty else 1
        if files_before <= 1 and rows_after == rows_before:
            continue  # ja e 1 arquivo so e nao ha linha morta pra descartar: nada a ganhar

        rows.append(
            {
                "bucket": bucket_name,
                "files_before": files_before,
                "files_after": files_after,
                "rows_before": rows_before,
                "rows_after": rows_after,
            }
        )

        if dry_run:
            continue

        if not kept_df.empty:
            new_path = os.path.join(bucket_dir, f"compacted-{uuid.uuid4().hex}.parquet")
            kept_df.to_parquet(new_path, engine="pyarrow", index=False)
        for path in old_paths:
            os.remove(path)

    report = pd.DataFrame(
        rows, columns=["bucket", "files_before", "files_after", "rows_before", "rows_after"]
    )
    files_removed = int((report["files_before"] - report["files_after"]).sum()) if not report.empty else 0
    rows_removed = int((report["rows_before"] - report["rows_after"]).sum()) if not report.empty else 0
    acao = "simulacao (dry_run)" if dry_run else "concluida"
    print(
        f"[compact_table] {schema}.{table}: {acao} — {len(report)} bucket(s) compactado(s), "
        f"{files_removed} arquivo(s) a menos, {rows_removed} linha(s) morta(s) descartada(s) "
        f"({time.perf_counter() - start:.1f}s)",
        flush=True,
    )
    return report
