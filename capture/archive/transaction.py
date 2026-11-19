from collections.abc import Iterator
from contextlib import contextmanager
import sqlite3


@contextmanager
def write_transaction(db: sqlite3.Connection) -> Iterator[None]:
    """Take the write lock before making any state-dependent decisions."""
    db.execute('BEGIN IMMEDIATE')
    try:
        yield
        db.commit()
    except BaseException:
        if db.in_transaction:
            db.rollback()
        raise
