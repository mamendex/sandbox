"""ETL simples baseado em pandas: discover, extract, transform e validate.

Fala com qualquer fonte SQL (Redshift, DuckDB, Postgres, ...) através do
contrato `QueryFn` — ver `i3_shift_lake.query`.
"""

from i3_shift_lake.checkpoint import clear_checkpoint, load_checkpoint
from i3_shift_lake.discover import TableModel, discover, load_model, save_model
from i3_shift_lake.extract import extract_all, extract_table, load_table_query, save_table_query
from i3_shift_lake.query import QueryFn, ping
from i3_shift_lake.transform import TableLoader
from i3_shift_lake.validate import (
    CheckResult,
    check_duplicate_ids,
    check_duplicates_all,
    check_foreign_key,
    check_row_counts,
    check_unique,
    count_loaded_rows,
    run_checks,
)

__all__ = [
    "QueryFn",
    "ping",
    "TableModel",
    "discover",
    "save_model",
    "load_model",
    "extract_all",
    "extract_table",
    "save_table_query",
    "load_table_query",
    "load_checkpoint",
    "clear_checkpoint",
    "TableLoader",
    "count_loaded_rows",
    "check_row_counts",
    "check_duplicate_ids",
    "check_duplicates_all",
    "CheckResult",
    "check_unique",
    "check_foreign_key",
    "run_checks",
]
