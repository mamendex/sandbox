"""ETL simples para Redshift baseado em pandas: discover, extract e transform."""

from redshift_etl.discover import TableModel, discover
from redshift_etl.extract import extract_all, extract_table
from redshift_etl.transform import TableLoader

__all__ = [
    "TableModel",
    "discover",
    "extract_all",
    "extract_table",
    "TableLoader",
]
