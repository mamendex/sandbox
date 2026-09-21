"""ETL simples para Redshift baseado em pandas: discover, extract, transform e validate."""

from redshift_etl.discover import TableModel, discover, load_model, save_model
from redshift_etl.extract import extract_all, extract_table, load_table_query, save_table_query
from redshift_etl.transform import TableLoader
from redshift_etl.validate import (
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
    "TableModel",
    "discover",
    "save_model",
    "load_model",
    "extract_all",
    "extract_table",
    "save_table_query",
    "load_table_query",
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
