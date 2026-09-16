# redshift_etl

ETL simples baseado em pandas para Redshift: discover -> extract -> transform.
Pensado para ser importado em notebooks Jupyter.

Pré-requisito: uma função `query(sql) -> pandas.DataFrame` que execute a query
contra o Redshift (ex.: um wrapper existente de conexão via `psycopg2`/`sqlalchemy`).

## Uso em notebook

```python
from redshift_etl import discover, extract_all, TableLoader

SCHEMA = "meu_schema"
OUTPUT_DIR = "./dados_extraidos"

# 1. discover: modelo (tabelas/colunas) + contagem de linhas
model = discover(query, SCHEMA)

# 2. extract: paginado, ordenado, particionado por bucket (default 32)
relatorio = extract_all(query, model, OUTPUT_DIR, page_size=50_000, num_buckets=32)
relatorio  # DataFrame com tabela, status, linhas lidas, ordenação usada, etc.

# 3. transform: carregar qualquer tabela extraída como DataFrame pandas
loader = TableLoader(OUTPUT_DIR, schema=SCHEMA)
df_contas = loader.load("accounts")
df_contas = loader["accounts"]  # atalho equivalente
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
