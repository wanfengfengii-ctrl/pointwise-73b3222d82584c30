from collections.abc import Iterable
import sqlite3

from capture.archive.transaction import write_transaction

from .definition import StationDefinition, build_station_definition


class UnknownStation(KeyError):
    """No station has the given code."""


class StationCatalog:
    def __init__(self, db: sqlite3.Connection):
        self._db = db

    @staticmethod
    def create_schema(db: sqlite3.Connection) -> None:
        db.execute('''
            CREATE TABLE IF NOT EXISTS stations (
                code TEXT PRIMARY KEY NOT NULL,
                active INTEGER NOT NULL CHECK (active IN (0, 1))
            )
        ''')
        db.execute('''
            CREATE TABLE IF NOT EXISTS station_channels (
                station_code TEXT NOT NULL REFERENCES stations(code),
                name TEXT NOT NULL,
                multiplier TEXT NOT NULL,
                ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
                PRIMARY KEY (station_code, name),
                UNIQUE (station_code, ordinal)
            )
        ''')

    def upsert(
        self, code: str, channels: Iterable[tuple[str, int]], active: bool = True
    ) -> StationDefinition:
        definition = build_station_definition(code, channels, active)
        with write_transaction(self._db):
            self._db.execute('''
                INSERT INTO stations (code, active) VALUES (?, ?)
                ON CONFLICT (code) DO UPDATE SET active = excluded.active
            ''', (definition.code, int(definition.active)))
            self._db.execute('DELETE FROM station_channels WHERE station_code = ?', (code,))
            self._db.executemany('''
                INSERT INTO station_channels (station_code, name, multiplier, ordinal)
                VALUES (?, ?, ?, ?)
            ''', (
                (definition.code, item.name, format(item.multiplier_um, 'x'), ordinal)
                for ordinal, item in enumerate(definition.channels)
            ))
        return definition

    def get(self, code: str) -> StationDefinition:
        rows = self._db.execute('''
            SELECT s.code, s.active, c.name, c.multiplier
              FROM stations AS s
              LEFT JOIN station_channels AS c ON c.station_code = s.code
             WHERE s.code = ?
             ORDER BY c.ordinal
        ''', (code,)).fetchall()
        if not rows:
            raise UnknownStation(code)
        stored_code, active, _, _ = rows[0]
        channels = ((name, int(scale, 16)) for _, _, name, scale in rows if name is not None)
        return build_station_definition(stored_code, channels, bool(active))
