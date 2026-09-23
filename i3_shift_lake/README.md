# i3_shift_lake

ETL simples baseado em pandas: discover -> extract -> transform -> validate.
Pensado para ser importado em notebooks Jupyter. Feito para o Redshift, mas
não depende dele — qualquer fonte que fale SQL padrão serve (ver `QueryFn`
abaixo). Prova disso: [`../worldbank_lake`](../worldbank_lake/README.md), um
segundo projeto que usa este pacote contra dados públicos via DuckDB.

## Pré-requisito: `query(sql) -> pandas.DataFrame`

O pacote não abre conexão sozinho. Você passa uma função (ou qualquer
callable) que execute o SQL e devolva um `pandas.DataFrame`:

```python
def query(sql: str) -> pd.DataFrame:
    return pd.read_sql(sql, minha_conexao)  # psycopg2, sqlalchemy, redshift_connector, duckdb, ...
```

Não precisa herdar nada nem importar nada para isso funcionar — `i3_shift_lake.query.QueryFn`
é um `typing.Protocol` que só documenta e tipa essa assinatura (tipagem
estrutural: uma função comum já satisfaz o contrato). Antes de rodar o
pipeline inteiro, valide sua implementação com `ping()`:

```python
from i3_shift_lake import ping

ping(query)  # roda um SELECT 1 e levanta TypeError com uma mensagem clara se algo não bater
```

Notebook de exemplo (JupyterHub): [`notebooks/exemplo_uso.ipynb`](../notebooks/exemplo_uso.ipynb).

## Uso em notebook

```python
from i3_shift_lake import discover, load_model, extract_all, TableLoader

SCHEMA = "meu_schema"
OUTPUT_DIR = "./dados_extraidos"
CONFIG_DIR = "./config"  # default já é "config"

# 1. discover: modelo (tabelas/colunas) + contagem de linhas, persistido em
#    <CONFIG_DIR>/<SCHEMA>.json
model = discover(query, SCHEMA, config_dir=CONFIG_DIR)

# ...em uma sessão futura, sem acessar o Redshift, dá para retomar o fluxo
# carregando o modelo já salvo:
# model = load_model(SCHEMA, config_dir=CONFIG_DIR)

# 2. extract: paginado, ordenado, particionado por bucket (default 32).
#    A query de select de cada tabela é salva em <CONFIG_DIR>/<SCHEMA>/<tabela>.json
relatorio = extract_all(query, model, OUTPUT_DIR, page_size=50_000, num_buckets=32, config_dir=CONFIG_DIR)
relatorio  # DataFrame com tabela, status, linhas lidas, ordenação usada, etc.

# 3. transform: carregar qualquer tabela extraída como DataFrame pandas
loader = TableLoader(OUTPUT_DIR, schema=SCHEMA, config_dir=CONFIG_DIR)
df_contas = loader.load("accounts")
df_contas = loader["accounts"]  # atalho equivalente
```

## Configuração persistida (`config_dir`, default `"config"`)

- `<config_dir>/<schema>.json`: modelo descoberto (colunas por tabela + contagem
  de linhas), gravado por `discover()` (ou manualmente via `save_model()`).
- `<config_dir>/<schema>/<tabela>.json`: query de select da tabela (com
  placeholders `{page_size}`/`{offset}` para a paginação), junto com as colunas,
  a ordenação e a coluna de particionamento escolhidas. Gravado por
  `extract_table()`/`extract_all()` durante a extração.

Esses arquivos permitem iniciar o fluxo de duas formas equivalentes:
direto do `discover()` (consulta o Redshift) ou a partir da configuração
salva anteriormente via `load_model()` (sem acessar o Redshift para
descobrir o modelo).

## Extração parcial (`sample_size`)

Para testes/dev, sem ler a base inteira, passe `sample_size` a
`extract_table()`/`extract_all()`: cada tabela é limitada às suas primeiras
`sample_size` linhas a partir do ponto de leitura (do início, em carga full,
ou do checkpoint, em incremental), na mesma ordenação usada na extração
completa — então o sample é sempre o "topo" da ordenação, não linhas
aleatórias. O valor usado fica registrado tanto no relatório quanto no JSON
da tabela em `config_dir`. Um sample não avança o checkpoint incremental além
do que de fato leu, então rodar sem `sample_size` depois continua corretamente
do mesmo ponto.

```python
relatorio = extract_all(query, model, OUTPUT_DIR, sample_size=1_000)
```

## Carga incremental (`load_mode`, `control_dir`)

`extract_table()`/`extract_all()` aceitam `load_mode` ("incremental", default,
ou "full") e `control_dir` (default `"control"`).

- **incremental**: para cada tabela, salva um checkpoint com os últimos
  valores das colunas de ordenação (em geral data de controle + id) em
  `<control_dir>/<schema>/<tabela>/checkpoint.json`. Na próxima carga, retoma
  exatamente dali (`WHERE (data, id) > (última_data, último_id)`), trazendo só
  as linhas novas — sem checkpoint prévio, equivale a uma carga full. Como os
  parquets são sempre acrescentados (nunca sobrescritos), o resultado final é
  o acumulado de todas as cargas incrementais.
- **full**: apaga os parquets já extraídos da tabela (`output_dir`) e o
  checkpoint, e recomeça do zero — use quando quiser reconstruir a tabela
  inteira em vez de só acrescentar o que é novo.

O relatório de `extract_all()`/`extract_table()` traz a coluna `stopped_at`
com os valores de `order_by` onde a carga parou (ex.: `{"date_modified":
"2024-06-01 12:00:00", "id": 4821}`) — é esse ponto que fica salvo no
checkpoint e de onde a próxima carga incremental retoma. Nos prints de
progresso, página a página, o mesmo ponto aparece de forma compacta, sem
nomes de coluna e datas em `YYYYMMDDHHMMSS`, para caber numa linha só:
`posicao {'20240601120000,4821'}`.

```python
# primeira carga (ou renovação completa de uma tabela)
extract_all(query, model, OUTPUT_DIR, config_dir=CONFIG_DIR, control_dir=CONTROL_DIR, load_mode="full")

# cargas seguintes: só o que mudou desde a última vez
extract_all(query, model, OUTPUT_DIR, config_dir=CONFIG_DIR, control_dir=CONTROL_DIR)  # load_mode="incremental" é o default
```

> Dê um `control_dir` (e `output_dir`/`config_dir`) próprio para cada pipeline
> lógico. O checkpoint é identificado só por schema+tabela dentro do
> `control_dir` — reusar o mesmo `control_dir` para duas extrações
> independentes da "mesma" tabela (ex.: um teste e a carga de produção) faz
> uma pisar no checkpoint da outra.
>
> Se a ordenação de uma tabela mudar (colunas do schema mudaram desde o
> checkpoint salvo), o checkpoint é ignorado com um aviso, e a tabela é lida
> do zero nessa rodada.

Para inspecionar ou resetar manualmente: `load_checkpoint(schema, tabela, control_dir=...)`
e `clear_checkpoint(schema, tabela, control_dir=...)`.

## Sem gaps, sem overlap de versão igual, substitui ao modificar

Cada página vira um arquivo parquet novo (nunca sobrescrito). Se um registro é
modificado entre uma carga e outra (mesmo id, `date_modified`/`date_created`
mais recente), o filtro `WHERE (data, id) > (checkpoint)` garante que ele seja
lido de novo — e as duas versões (antiga e nova) acabam fisicamente presentes
no dataset. As garantias do framework são:

- **Sem gaps**: a comparação por tupla (`(data, id) > (última_data, último_id)`)
  cobre exatamente a fronteira onde a carga anterior parou, sem pular linhas —
  mesmo quando várias linhas têm a mesma data.
- **Sem reler o que não mudou**: um id cujo `date_modified` não avançou não
  volta a bater no filtro — não é lido de novo.
- **A versão nova substitui a antiga**: `TableLoader.load()` (com
  `dedupe=True`, o default) resolve isso na leitura, mantendo só a linha com o
  maior valor de `order_by` por id — a versão antiga do mesmo id nunca aparece
  junto. Use `dedupe=False` para ver todas as versões cruas, como estão no
  parquet.
- **Overlap de verdade (bug) é detectável**: `check_duplicate_ids()` foi
  ajustado para não confundir uma releitura por modificação (que é esperada)
  com um overlap real. Ele só acusa quando a *mesma versão* de um id (mesmos
  valores de `order_by`, não só o id) aparece mais de uma vez — o que só
  deveria acontecer por um bug de paginação, nunca pela carga incremental
  normal.

```python
loader = TableLoader(OUTPUT_DIR, schema=SCHEMA, config_dir=CONFIG_DIR)
df = loader["accounts"]  # já com a versão mais recente de cada id

check_duplicate_ids(OUTPUT_DIR, SCHEMA, "accounts", config_dir=CONFIG_DIR)  # só overlaps reais
```

Essas três garantias (sem gap, sem releitura do que não mudou, substituição
correta ao modificar) têm um teste de ponta a ponta dedicado em
[`tests/smoke_test.py`](tests/smoke_test.py) — incluindo a simulação de um
overlap real de propósito, para provar que ele é detectado e resolvido.

## Regras de ordenação e particionamento

Para cada tabela, a ordenação (e paginação) usa a primeira combinação disponível:

1. `date_modified` + `id`
2. `date_created` + `id`
3. `date_modified` + `id_c`
4. `date_created` + `id_c`
5. `id`
6. `id_c`

A coluna de particionamento dos parquets (`id` ou `id_c`, a que existir) é
transformada em um bucket estável (`hash % num_buckets`) e gravada como
partição `bucket=N` dentro de `OUTPUT_DIR/<schema>/<tabela>/`.

## Arquivos acumulados por carga e compactação (`compact_table`)

Cada carga (rodada de extração) grava um arquivo parquet novo por bucket
tocado — nunca reescreve os arquivos já existentes. Isso é proposital:
escrita barata e atômica (nunca precisa reler/regravar o dataset inteiro), e
dá pra reverter uma carga problemática apagando só os arquivos daquela
rodada específica. O custo é que o número de arquivos por bucket só cresce
com o tempo — mesmo cargas incrementais pequenas tendem a tocar quase todos
os buckets (o hash distribui os ids uniformemente), então o total de
arquivos cresce com o **número de cargas**, não com o volume de dados novo.

`compact_table(output_dir, schema, table, config_dir, min_age_hours=24, dedupe=True, dry_run=False)`
junta, por bucket, os arquivos mais antigos que `min_age_hours` num único
arquivo:

```python
from i3_shift_lake import compact_table

# so espia o que aconteceria, sem mexer em nada
compact_table(OUTPUT_DIR, SCHEMA, "accounts", config_dir=CONFIG_DIR, dry_run=True)

# compacta de verdade: junta arquivos com mais de 24h (default) em cada bucket
compact_table(OUTPUT_DIR, SCHEMA, "accounts", config_dir=CONFIG_DIR)
```

- Arquivos **mais novos** que `min_age_hours` nunca são lidos nem apagados —
  de propósito, para preservar a janela de rollback de uma carga recente
  (que é justamente quando essa capacidade mais importa).
- Com `dedupe=True` (default), além de juntar fisicamente os arquivos
  elegíveis, descarta linhas cuja versão (`order_by` + id) já foi
  substituída por uma versão mais nova em qualquer arquivo do bucket
  (antigo ou recente) — a mesma lógica que `TableLoader.load()` já aplica a
  cada leitura, só que aqui o resultado fica gravado em disco de vez,
  reduzindo também o volume de dados morto (versões antigas de ids
  modificados, overlaps reais de paginação). Requer a config persistida da
  tabela; sem ela, cai para `dedupe=False` com aviso.
- Devolve um relatório (`bucket`, `files_before`/`files_after`,
  `rows_before`/`rows_after`) com uma linha por bucket efetivamente
  compactado — buckets sem nada a ganhar nem aparecem.
- É transparente para quem lê os dados depois (`TableLoader`,
  `check_duplicate_ids` etc. continuam vendo exatamente os mesmos dados) e
  idempotente (rodar de novo sem cargas novas não muda nada).

## Colunas de data com valores nulos (`NULL_DATE_SENTINEL`)

Uma coluna de data usada em `order_by` (`date_modified`/`date_created`) pode
ter `NULL` em algumas — ou na maioria — das linhas (comum quando a coluna só é
preenchida na primeira atualização de um registro; até lá fica nula). Um
`NULL` ali é um problema real: `WHERE (data, id) > (NULL, x)` nunca é
verdadeiro em SQL, então, se o checkpoint parar numa linha com data nula, a
carga incremental fica travada em 0 linhas para sempre — mesmo que dados
novos cheguem depois.

Com `column_types` disponível (o caso normal, vindo do `discover()`), o
`ORDER BY` e o filtro de retomada passam a usar
`COALESCE("data", TIMESTAMP '1900-01-01')` no lugar da coluna crua — `NULL`
nunca mais entra na comparação. O dado armazenado no parquet continua o
real (`NULL` onde houver); só a ordenação/checkpoint internos usam o
substituto. Como `1900-01-01` é a menor data possível, linhas com data nula
passam a ordenar primeiro (tratadas como "as mais antigas") — se um desses
registros for modificado de verdade depois, a nova data é maior que
qualquer checkpoint já alcançado e ele é pego normalmente na carga
incremental seguinte.

O valor sentinela é `i3_shift_lake.NULL_DATE_SENTINEL` (`"1900-01-01"`).

> Sem `column_types` (ex.: config salva por uma versão anterior do pacote),
> o framework não sabe que a coluna é uma data e não aplica o `COALESCE` —
> continua funcionando sem quebrar, mas com o risco de travar descrito
> acima. Rode `discover()` de novo para atualizar o `config_dir`.

## Schema consistente entre páginas (`column_types`)

Cada página é gravada como um arquivo parquet separado. Se uma coluna de texto
vier inteiramente `None` numa página específica, o pandas/pyarrow pode inferir
o tipo dessa coluna, naquele arquivo, como `null` — diferente do `string`/`int`
inferido em outras páginas onde a coluna tem valores reais. Ao ler o dataset
completo (`TableLoader`, `check_row_counts`, `check_duplicate_ids`), o pyarrow
precisa unificar os schemas dos arquivos e, em algumas versões, isso falha com
`ArrowNotImplementedError: Unsupported cast from large_string to null`.

Para evitar isso, `discover()` também guarda o `data_type` de cada coluna
(`model.column_types[tabela][coluna]`, vindo de `information_schema.columns`)
e `extract_table()`/`extract_all()` usam esse tipo para fixar o dtype de toda
página antes de gravar — assim nenhuma página fica "adivinhando" sozinha o
tipo de uma coluna totalmente nula. A coerção é best-effort: tipos não
mapeados ou que falhem na conversão ficam como o pandas inferiu.

> Modelos salvos por uma versão anterior (sem `column_types` no JSON) continuam
> carregando normalmente com `load_model()` — nesse caso a coerção é pulada e o
> comportamento fica igual ao de antes dessa correção. Rode `discover()` de
> novo para atualizar o `config_dir` com os tipos.

## Validação da carga (`i3_shift_lake/validate.py`)

```python
from i3_shift_lake import load_status, check_row_counts, check_duplicates_all, check_unique, check_foreign_key, run_checks

# relatório rápido por tabela: sem precisar de um `model` em mãos
load_status(OUTPUT_DIR, SCHEMA, config_dir=CONFIG_DIR)

# linhas carregadas (parquet) x linhas esperadas (discover), por tabela
check_row_counts(model, OUTPUT_DIR)

# duplicidade da chave de particionamento (id/id_c) nos parquets de cada tabela
check_duplicates_all(model, OUTPUT_DIR, config_dir=CONFIG_DIR)
```

- `load_status(output_dir, schema, config_dir)`: relatório da situação da
  carga, uma linha por tabela, com `parquet_files`, `rows_loaded`,
  `expected_rows` (contagem do último `discover()` salvo em `config_dir`,
  se houver) e `pct` (`rows_loaded / expected_rows * 100`, arredondado em
  1 casa). Não precisa de um `TableModel` em mãos — lê `output_dir`
  diretamente e carrega o modelo persistido sozinho. Tabelas que o
  `discover` conhece mas que ainda não têm parquet aparecem com
  `status="nao_extraida"` (0 arquivos/linhas); sem nenhum `discover` salvo
  em `config_dir`, o relatório ainda lista as tabelas extraídas, mas
  `expected_rows`/`pct` ficam vazios (`status="sem_discover"`). O `status`
  em si continua sendo `"ok"`/`"divergente"` pela igualdade exata de linhas,
  não pelo `pct` — o percentual é só informativo.
- `count_loaded_rows(output_dir, schema, table)` / `check_row_counts(model, output_dir)`:
  conta linhas lendo só metadados do parquet (sem carregar os dados) e compara
  com a contagem levantada pelo `discover`. Diferente de `load_status`, exige
  o `model` do `discover` em mãos (mas dá o `status` "nao_extraida" por
  tabela na mesma linha, sem precisar rodar duas consultas separadas).
- `check_duplicate_ids(output_dir, schema, table, config_dir)` /
  `check_duplicates_all(model, output_dir, config_dir)`: verifica se a coluna
  usada para particionar/ordenar (`id` ou `id_c`, a mesma persistida em
  `config_dir`) tem valores duplicados entre os parquets extraídos.

Espaço para checks customizados de qualidade (chaves estrangeiras, chaves
naturais únicas como cpf/cnpj, etc.): `check_unique(df, coluna, tabela)` e
`check_foreign_key(df, coluna, ref_df, ref_coluna, tabela)` cobrem os casos
comuns; escreva sua própria função com a mesma assinatura (retornando um
`CheckResult`) para regras específicas, e rode tudo junto com `run_checks`:

```python
from functools import partial
from i3_shift_lake import check_unique, check_foreign_key, run_checks

df_clientes = loader.load("clientes")
df_pedidos = loader.load("pedidos")

checks = [
    partial(check_unique, df_clientes, "cpf", "clientes"),
    partial(check_foreign_key, df_pedidos, "cliente_id", df_clientes, "id", "pedidos"),
]
relatorio_qualidade = run_checks(checks)
```

## Camada silver: entidades e relacionamentos (`i3_shift_lake/silver.py`)

A raw só faz o "shift" da fonte pra parquet — sem renomear nada, sem juntar
tabelas. A silver é onde isso acontece: filtro de colunas, filtro de linhas,
merge de tabelas e renomeio de colunas/tabelas, seguindo a mesma filosofia
do resto do pacote — tudo com pandas puro (`rename`/`query`/`merge`), sem
motor de execução ou DSL próprios. Uma "entidade" (ou "relacionamento" — é a
mesma coisa aqui) é só uma declaração de que fontes raw usar e como
combiná-las; `build_entity` executa isso e grava o resultado em parquet,
particionado por bucket como a raw (`OUTPUT_DIR_SILVER/<schema>/<entidade>/`).

Diferença importante em relação à raw: **cada build reconstrói a entidade
inteira do zero** a partir do estado atual da raw (já incremental e
dedupada pelo `TableLoader`) e substitui por completo o que existia — não
há carga incremental nem checkpoint próprios pra silver. Isso é uma escolha
deliberada de simplicidade: as entidades (a configuração de quais
tabelas/colunas/joins formam cada uma) mudam raro, e cada build já parte da
raw mais recente, então não precisa reprocessar só o incremento — e sem
incremental, também não há o problema de arquivo acumulado que motivou o
`compact_table` na raw.

```python
from i3_shift_lake import Entity, Source, Join, ForeignKey, build_entity, build_all, check_foreign_keys, TableLoader

raw = TableLoader(OUTPUT_DIR, schema=SCHEMA, config_dir=CONFIG_DIR)

# a entidade xpto na camada silver e a tabela XPTO na raw
xpto = Entity(name="xpto", id_column="id", sources=[Source(table="XPTO")])

# a entidade alfa na silver e a juncao da tabela BETA.id com a GAMA.id_c da raw
alfa = Entity(
    name="alfa",
    id_column="id",
    sources=[Source(table="BETA"), Source(table="GAMA")],
    joins=[Join(left_on="id", right_on="id_c")],
)

# o relacionamento rel na silver e a tabela X, vinculando xpto.id (rel.xpto_id)
# e alfa.id (rel.alfa_id)
rel = Entity(
    name="rel",
    id_column="id",
    sources=[Source(table="X", rename={"XPTO_ID": "xpto_id", "ALFA_ID": "alfa_id"})],
    foreign_keys=[
        ForeignKey(column="xpto_id", entity="xpto", entity_column="id"),
        ForeignKey(column="alfa_id", entity="alfa", entity_column="id"),
    ],
)

build_all([xpto, alfa, rel], raw, SILVER_OUTPUT_DIR, SCHEMA, config_dir=SILVER_CONFIG_DIR)

# le a silver de volta com o mesmo TableLoader que ja existe pra raw
silver = TableLoader(SILVER_OUTPUT_DIR, schema=SCHEMA, config_dir=SILVER_CONFIG_DIR)
df_alfa = silver.load("alfa")

# roda check_foreign_key (validate.py) pra cada ForeignKey declarada
check_foreign_keys([xpto, alfa, rel], silver)
```

- `Source(table, columns=None, rename={}, where=None)`: uma fonte raw. `where`
  é uma string de `DataFrame.query()` sobre as colunas **originais** (antes
  do rename); `columns` filtra colunas (e precisa incluir qualquer coluna
  usada em `where` ou nos `joins`); `rename` roda antes do merge — use pra
  resolver colisão de nome entre fontes (ex.: as duas tendo `date_modified`).
- `Join(left_on, right_on, how="inner")`: mesmo vocabulário do `pd.merge`.
  Uma `Entity` com N fontes precisa de N-1 joins, aplicados em sequência
  (a 2ª fonte junta com a 1ª, a 3ª com o resultado disso, etc.).
- `Entity(name, id_column, sources, joins=[], columns=None, foreign_keys=[])`:
  `id_column` é a coluna (já com nome final, pós-rename/merge) usada pra
  particionar o parquet — assim como `id`/`id_c` na raw. `columns` restringe
  a seleção final (`None` = mantém tudo). `foreign_keys` só documenta e
  habilita `check_foreign_keys` — não afeta o build.
- `build_entity(entity, raw, output_dir, schema, config_dir, num_buckets=32)`:
  lê as fontes via `TableLoader` (raw), aplica filtro/rename/merge e grava o
  parquet inteiro de novo, num diretório temporário que só troca de lugar
  com o anterior depois de terminar com sucesso — se algo falhar no meio,
  a versão anterior da entidade continua intacta. `build_all` roda uma lista
  de entidades em sequência (a ordem importa se uma entidade compuser outra
  entidade silver como fonte, via um segundo `TableLoader` apontado pro
  output da silver).
- `check_foreign_keys(entities, silver)`: roda `check_foreign_key` pra cada
  `ForeignKey` declarada, carregando cada entidade referenciada uma única
  vez (cache interno), mesmo que várias FKs apontem pra ela.

A entidade resultante tem exatamente o mesmo formato físico da raw (parquet
particionado por bucket), então é lida com o **mesmo `TableLoader`** — não
existe um leitor separado pra silver.
