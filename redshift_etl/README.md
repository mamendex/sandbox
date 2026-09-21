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
`sample_size` linhas, na mesma ordenação usada na extração completa (então o
sample é sempre o "topo" da ordenação, não linhas aleatórias). O valor usado
fica registrado tanto no relatório quanto no JSON da tabela em `config_dir`.

```python
relatorio = extract_all(query, model, OUTPUT_DIR, sample_size=1_000)
```

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
