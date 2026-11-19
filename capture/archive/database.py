import sqlite3
from os import PathLike

from capture.stations.catalog import StationCatalog
from capture.tasks.registry import TaskRegistry

from .schema import create_archive_schema
from .transaction import write_transaction


def open_database(path: str | PathLike[str]) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    try:
        db.execute('PRAGMA foreign_keys = ON')
    except BaseException:
        db.close()
        raise
    return db


def initialize_database(db: sqlite3.Connection) -> None:
    with write_transaction(db):
        StationCatalog.create_schema(db)
        TaskRegistry.create_schema(db)
        create_archive_schema(db)
