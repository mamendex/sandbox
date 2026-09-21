# redshift_etl

ETL simples baseado em pandas para Redshift: discover -> extract -> transform -> validate.
Pensado para ser importado em notebooks Jupyter.

Pré-requisito: uma função `query(sql) -> pandas.DataFrame` que execute a query
contra o Redshift (ex.: um wrapper existente de conexão via `psycopg2`/`sqlalchemy`).

Notebook de exemplo (JupyterHub): [`notebooks/exemplo_uso.ipynb`](../notebooks/exemplo_uso.ipynb).

## Uso em notebook

```python
from redshift_etl import discover, load_model, extract_all, TableLoader

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
loader = TableLoader(OUTPUT_DIR, schema=SCHEMA)
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

## Validação da carga (`redshift_etl/validate.py`)

```python
from redshift_etl import check_row_counts, check_duplicates_all, check_unique, check_foreign_key, run_checks

# linhas carregadas (parquet) x linhas esperadas (discover), por tabela
check_row_counts(model, OUTPUT_DIR)

# duplicidade da chave de particionamento (id/id_c) nos parquets de cada tabela
check_duplicates_all(model, OUTPUT_DIR, config_dir=CONFIG_DIR)
```

- `count_loaded_rows(output_dir, schema, table)` / `check_row_counts(model, output_dir)`:
  conta linhas lendo só metadados do parquet (sem carregar os dados) e compara
  com a contagem levantada pelo `discover`.
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
from redshift_etl import check_unique, check_foreign_key, run_checks

df_clientes = loader.load("clientes")
df_pedidos = loader.load("pedidos")

checks = [
    partial(check_unique, df_clientes, "cpf", "clientes"),
    partial(check_foreign_key, df_pedidos, "cliente_id", df_clientes, "id", "pedidos"),
]
relatorio_qualidade = run_checks(checks)
```
