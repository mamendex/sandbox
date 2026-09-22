# public_data_lake

Segundo projeto de demonstração do `redshift_etl`: baixa uma base pública real
(Banco Mundial — população e PIB por país/ano, via
[github.com/datasets](https://github.com/datasets), dados originais do
[World Bank Open Data](https://data.worldbank.org/), CC-BY-4.0), materializa
num [DuckDB](https://duckdb.org/) local e usa o `redshift_etl` (o mesmo
pacote usado para o Redshift) para descobrir o schema, extrair de forma
paginada e particionada, e validar a carga — sem mudar uma linha do pacote.

Funciona porque o DuckDB fala essencialmente o mesmo dialeto SQL que o
`redshift_etl` já assume: `information_schema.columns`, identificadores entre
aspas duplas, `LIMIT`/`OFFSET`, comparação por tupla no `WHERE`. Só trocamos
a função `query(sql) -> pandas.DataFrame` que é passada para
`discover`/`extract_all` — tudo o mais é o pacote de sempre.

Notebook de exemplo: [`notebooks/migrar_dataset_publico.ipynb`](notebooks/migrar_dataset_publico.ipynb).

## Estrutura

- `source.py`: baixa os CSVs públicos (cacheados em `raw/`, gerado — não
  versionado) e monta um DuckDB local (`warehouse.duckdb`, também gerado) com
  duas tabelas (`population`, `gdp`), cada uma com colunas `id` e
  `date_modified` adicionadas na ingestão (a fonte original não tem colunas
  de controle — são necessárias para o `redshift_etl` ordenar/particionar/
  retomar como faria numa tabela real do Redshift). Expõe `get_query_fn()`,
  que devolve a função `query(sql)` a ser passada ao `redshift_etl`.
- `notebooks/migrar_dataset_publico.ipynb`: discover → extract (paginado,
  particionado, incremental) → transform → validate, incluindo uma simulação
  de "chegada de dados novos" para mostrar a carga incremental de verdade
  (o dataset em si é uma foto estática; `source.append_new_years()` insere
  anos mais recentes com um `date_modified` novo, para exercitar o
  checkpoint).

## Uso rápido

```python
import sys
sys.path.insert(0, "..")  # raiz do repo, para importar redshift_etl

from public_data_lake.source import build_database, get_query_fn
from redshift_etl import discover, extract_all, TableLoader

build_database()  # baixa os CSVs (1a vez) e monta o DuckDB local
query = get_query_fn()

model = discover(query, "public_data", config_dir="./config")
extract_all(query, model, "./dados_extraidos", page_size=2000, num_buckets=8, control_dir="./control")

loader = TableLoader("./dados_extraidos", schema="public_data")
df_pop = loader["population"]
```

Testado ponta a ponta com os dados reais: ~17 mil linhas em `population` e
~14 mil em `gdp`, extração paginada (`page_size=2000` gera de 6 a 9 páginas
por tabela), particionamento por `id` em 8 buckets, `check_row_counts` e
`check_duplicate_ids` batendo, e uma rodada incremental trazendo só as
linhas novas simuladas (sem duplicar nem perder nada).
