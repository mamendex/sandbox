"""Controle de carga incremental: até onde a extração de cada tabela já foi.

Um checkpoint guarda os últimos valores das colunas de ordenação (em geral
data de controle + id) da última linha extraída de uma tabela. A próxima
carga incremental usa esse ponto para continuar dali em diante, em vez de
reler a tabela inteira.
"""

from __future__ import annotations

import json
import os

DEFAULT_CONTROL_DIR = "control"


def checkpoint_path(schema: str, table: str, control_dir: str = DEFAULT_CONTROL_DIR) -> str:
    return os.path.join(control_dir, schema, table, "checkpoint.json")


def _to_jsonable(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):  # escalar numpy (int64, float64, bool_, ...)
        return value.item()
    return value


def save_checkpoint(
    schema: str,
    table: str,
    order_by: list[str],
    last_values: list,
    control_dir: str = DEFAULT_CONTROL_DIR,
) -> str:
    """Persiste o ponto onde a extração parou (últimos valores das colunas de `order_by`)."""
    path = checkpoint_path(schema, table, control_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "schema": schema,
                "table": table,
                "order_by": order_by,
                "last_values": [_to_jsonable(v) for v in last_values],
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    return path


def load_checkpoint(schema: str, table: str, control_dir: str = DEFAULT_CONTROL_DIR) -> dict | None:
    """Carrega o checkpoint salvo de uma tabela, ou `None` se ela nunca foi carregada."""
    path = checkpoint_path(schema, table, control_dir)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def clear_checkpoint(schema: str, table: str, control_dir: str = DEFAULT_CONTROL_DIR) -> None:
    """Remove o checkpoint de uma tabela (usado antes de uma carga full)."""
    path = checkpoint_path(schema, table, control_dir)
    if os.path.isfile(path):
        os.remove(path)
