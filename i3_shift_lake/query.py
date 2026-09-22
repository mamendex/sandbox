"""Contrato de conexão que o i3_shift_lake espera de quem o usa: uma função
`query(sql) -> pandas.DataFrame`.

`QueryFn` é um `typing.Protocol` — tipagem estrutural, não nominal: qualquer
função (ou objeto com `__call__`) com essa assinatura já satisfaz o contrato,
sem precisar herdar nada nem importar essa classe. Ela existe só para dar um
nome e um lugar único para documentar a API esperada (em vez de um
`Callable[[str], pd.DataFrame]` anônimo espalhado pelo código) e para o editor/
type-checker apontar erros de assinatura antes de rodar.
"""

from __future__ import annotations

from typing import Protocol

import pandas as pd


class QueryFn(Protocol):
    """`query(sql: str) -> pandas.DataFrame`.

    Executa `sql` contra a fonte de dados e devolve o resultado como DataFrame,
    com os nomes de coluna do SELECT preservados (o pacote casa esses nomes com
    o schema descoberto em `discover()`). Qualquer callable serve, por exemplo:

        def query(sql: str) -> pd.DataFrame:
            return pd.read_sql(sql, minha_conexao)

    Testado com Redshift (via psycopg2/sqlalchemy/redshift_connector) e DuckDB.
    Qualquer fonte que fale SQL "normal" — `information_schema.columns`,
    identificadores entre aspas duplas, `LIMIT`/`OFFSET`, comparação por tupla
    no `WHERE` — deve funcionar sem adaptação.
    """

    def __call__(self, sql: str) -> pd.DataFrame: ...


def ping(query: QueryFn) -> bool:
    """Valida rapidamente uma implementação de `query()` antes de rodar o pipeline inteiro.

    Roda um `SELECT` trivial e confere que o retorno é um DataFrame com o valor
    esperado. Levanta `TypeError`, com uma mensagem explicando o que não bateu,
    se `query()` não seguir o contrato — útil como primeiro passo de diagnóstico
    ao plugar uma fonte nova.
    """
    result = query("SELECT 1 AS ok")
    if not isinstance(result, pd.DataFrame):
        raise TypeError(
            f"query() deveria devolver um pandas.DataFrame, devolveu {type(result).__name__}. "
            "Confira se a implementação de query(sql) segue i3_shift_lake.query.QueryFn."
        )
    if result.empty or "ok" not in result.columns or result["ok"].iloc[0] != 1:
        raise TypeError(
            f"query('SELECT 1 AS ok') devolveu um resultado inesperado:\n{result}\n"
            "Confira se query(sql) está executando o SQL corretamente."
        )
    return True
