"""Small, local SQLite repository for validated system snapshots (storage schema 1).

``SnapshotRepository(db_path)`` stores whole snapshots, exactly as produced by
``collector.collect_system_snapshot`` and accepted by ``validator.validate_snapshot``,
and reads them back. It holds no derived data (no findings, no history analysis).

Contract
--------
* **Explicit and side-effect free.** Importing this module, or constructing a
  repository, touches nothing on disk. The database file is created only by
  ``initialize()``; ``save_snapshot`` and ``get_snapshots`` never create or
  initialize it (they raise ``RepositoryNotInitializedError`` instead). Missing
  parent directories are not created.
* **Resource management.** The repository owns no connection. Each public call
  opens its own connection, uses explicit ``BEGIN`` / ``COMMIT`` / ``ROLLBACK``,
  and always closes it. Nothing stays open between calls (so a database file can
  be deleted or moved right after a call, including on Windows), and one
  repository object is safe to share between threads (each call has its own
  connection; SQLite's file lock serializes writers).
* **save_snapshot(snapshot) -> int.** Validates with the existing validator
  *first*, then serializes, then writes one row in a single ``BEGIN IMMEDIATE``
  transaction. An invalid snapshot raises ``InvalidSnapshotError`` (carrying the
  validator's issues) and no database access happens. Nothing is repaired,
  coerced or modified, and the input object is never changed. Returns the new
  row id. Saving the same snapshot twice stores two rows (no deduplication).
* **get_snapshots(limit=100) -> tuple[StoredSnapshot, ...].** Returns the *most
  recent* ``limit`` records, ordered oldest first by ``(observed_at, id)``:
  ``observed_at`` is the snapshot's own UTC timestamp (a fixed-width string, so
  text order is chronological) and ``id`` (insertion order) breaks ties
  deterministically. ``limit`` must be an ``int`` (not ``bool``) from 1 to
  ``MAX_LIMIT`` (1000). There is no pagination, so records older than the most
  recent ``limit`` are not reachable through this API yet. The connection is
  opened ``PRAGMA query_only``: retrieval cannot modify the database.
* **Stored form.** One table, ``snapshots(id, observed_at, schema_version,
  payload_json, stored_at)``. ``payload_json`` is the whole snapshot as strict,
  deterministic JSON (sorted keys, compact separators, ASCII, no NaN/Infinity).
  JSON-compatible content round-trips; Python object identity and dict key order
  do not. ``stored_at`` is the repository's own UTC time of writing
  (``YYYY-MM-DDTHH:MM:SSZ``), taken from an injectable clock.
* **Integrity on read.** A stored row is rejected with ``CorruptRecordError``
  (never skipped) if a column has the wrong type, the payload is not valid strict
  JSON or not a JSON object, or its ``observed_at`` / ``schema_version`` differ
  from the row's columns. Only the rows actually returned are checked. Rows are
  *not* re-validated with the validator on read: a future validator that drops
  schema 1 must not make old history unreadable. Call ``validate_snapshot`` on
  ``record.snapshot`` if you need that.
* **Schema safety.** ``initialize()`` is idempotent and never deletes or alters
  data. If a ``snapshots`` table already exists it is checked (columns, and
  ``PRAGMA user_version == 1``); an incompatible table, or a foreign database
  that already uses a different ``user_version``, is refused with
  ``StorageDatabaseError`` and left unchanged. There are no migrations yet.

Exceptions (all derive from ``StorageError``)
---------------------------------------------
``InvalidSnapshotError`` (``.issues``), ``RepositoryNotInitializedError``,
``StorageDatabaseError`` (a ``sqlite3`` failure such as a locked, unreadable or
non-SQLite file, chained as ``__cause__``), ``CorruptRecordError``
(``.record_id``), ``InvalidQueryError`` (also a ``ValueError``). Errors that
indicate a bug (``sqlite3.ProgrammingError``, ``TypeError``, an invalid
constructor argument, a clock that returns a non-UTC value, ...) are not
converted; they propagate.

Privacy and safety
------------------
The database holds system information (OS version, drive letters, capacities,
network interface names) and should be treated as sensitive. Keep it out of
version control. The module performs no network access, runs no commands,
collects no metrics, deletes nothing and runs no background work. The only clock
reading is ``stored_at``.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
from typing import Any, Callable, Iterator

from validator import ValidationIssue, validate_snapshot

__all__ = [
    "DEFAULT_LIMIT", "MAX_LIMIT", "DEFAULT_BUSY_TIMEOUT_S", "SCHEMA_MARKER",
    "StorageError", "InvalidSnapshotError", "RepositoryNotInitializedError",
    "StorageDatabaseError", "CorruptRecordError", "InvalidQueryError",
    "StoredSnapshot", "SnapshotRepository",
]

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
DEFAULT_BUSY_TIMEOUT_S = 5.0
_MAX_BUSY_TIMEOUT_S = 60.0
SCHEMA_MARKER = 1          # stored in PRAGMA user_version

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at    TEXT    NOT NULL CHECK (typeof(observed_at) = 'text'),
    schema_version INTEGER NOT NULL CHECK (typeof(schema_version) = 'integer'),
    payload_json   TEXT    NOT NULL CHECK (typeof(payload_json) = 'text'),
    stored_at      TEXT    NOT NULL CHECK (typeof(stored_at) = 'text')
)"""
_CREATE_INDEX = ("CREATE INDEX IF NOT EXISTS idx_snapshots_observed_at "
                 "ON snapshots (observed_at, id)")
_SET_MARKER = f"PRAGMA user_version = {SCHEMA_MARKER}"      # constant, no caller data
_EXPECTED_COLUMNS = (          # (name, declared type, notnull, primary key)
    ("id", "INTEGER", 0, 1),
    ("observed_at", "TEXT", 1, 0),
    ("schema_version", "INTEGER", 1, 0),
    ("payload_json", "TEXT", 1, 0),
    ("stored_at", "TEXT", 1, 0),
)
_INSERT = ("INSERT INTO snapshots (observed_at, schema_version, payload_json, stored_at) "
           "VALUES (?, ?, ?, ?)")
_SELECT_RECENT = """
SELECT id, observed_at, schema_version, payload_json, stored_at FROM (
    SELECT id, observed_at, schema_version, payload_json, stored_at FROM snapshots
    ORDER BY observed_at DESC, id DESC LIMIT ?
) ORDER BY observed_at ASC, id ASC"""

# sqlite3 exceptions that signal a bug in this module rather than a database failure.
_BUG_ERRORS = (sqlite3.ProgrammingError, sqlite3.InterfaceError, sqlite3.NotSupportedError)


# --------------------------------------------------------------------------
# Exceptions and records
# --------------------------------------------------------------------------

class StorageError(Exception):
    """Base class for every error this module raises on purpose."""


class InvalidSnapshotError(StorageError):
    """The snapshot failed validation (or cannot be serialized); nothing was stored.

    ``issues`` holds the validator's ``ValidationIssue`` objects (empty when the
    snapshot was valid but could not be serialized as strict JSON).
    """

    def __init__(self, issues: tuple[ValidationIssue, ...], message: str | None = None) -> None:
        if message is None:
            first = issues[0]
            message = (f"snapshot failed validation with {len(issues)} issue(s); "
                       f"first: {first.code} at {first.path}")
        super().__init__(message)
        self.issues = issues


class RepositoryNotInitializedError(StorageError):
    """The database file or the snapshots table does not exist; call ``initialize()``."""


class StorageDatabaseError(StorageError):
    """SQLite failed (locked, unreadable, not a database, incompatible schema, ...)."""


class CorruptRecordError(StorageError):
    """A stored row failed the integrity checks on read. ``record_id`` is its id (or None)."""

    def __init__(self, record_id: object, reason: str) -> None:
        shown = record_id if type(record_id) is int else "?"
        super().__init__(f"stored record {shown} is corrupt: {reason}")
        self.record_id = record_id if type(record_id) is int else None


class InvalidQueryError(StorageError, ValueError):
    """An argument to a query method (the ``limit``) is invalid."""


@dataclass(frozen=True)
class StoredSnapshot:
    """One stored record. The dataclass is frozen; ``snapshot`` is a plain ``dict``
    that is a fresh private copy for every retrieval (editing it cannot affect the
    database or other callers). Because it holds a dict, instances are not hashable.
    """

    id: int
    observed_at: str
    schema_version: int
    stored_at: str
    snapshot: dict[str, Any]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_utc(moment: object) -> str:
    if not isinstance(moment, datetime):
        raise TypeError("clock must return a datetime")
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
        raise ValueError("clock must return a timezone-aware UTC datetime")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_limit(limit: object) -> None:
    if type(limit) is not int:               # also rejects bool, float, str, None
        raise InvalidQueryError("limit must be an int (bool and float are not accepted)")
    if not 1 <= limit <= MAX_LIMIT:
        raise InvalidQueryError(f"limit must be between 1 and {MAX_LIMIT}")


@contextmanager
def _translate_db_errors() -> Iterator[None]:
    """Turn expected SQLite failures into ``StorageDatabaseError``; let bugs through."""
    try:
        yield
    except _BUG_ERRORS:
        raise
    except sqlite3.Error as exc:
        raise StorageDatabaseError(f"SQLite operation failed: {exc}") from exc


def _rollback(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass        # the original error is being re-raised; closing the connection rolls back too


def _schema_present(conn: sqlite3.Connection) -> bool:
    """True if a compatible ``snapshots`` table exists, False if there is none.

    Raises ``StorageDatabaseError`` if a table of that name exists but is not ours.
    """
    found = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'snapshots'").fetchone()
    if found is None:
        return False
    columns = tuple((row[1], row[2], row[3], row[5])
                    for row in conn.execute("PRAGMA table_info(snapshots)"))
    marker = conn.execute("PRAGMA user_version").fetchone()[0]
    if columns != _EXPECTED_COLUMNS or marker != SCHEMA_MARKER:
        raise StorageDatabaseError(
            "the database has an incompatible 'snapshots' table; it was left unchanged")
    return True


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-standard JSON constant {name}")


def _decode_row(row: tuple) -> StoredSnapshot:
    record_id, observed_at, schema_version, payload, stored_at = row
    if type(record_id) is not int:
        raise CorruptRecordError(record_id, "id is not an integer")
    if (type(observed_at) is not str or type(stored_at) is not str
            or type(payload) is not str or type(schema_version) is not int):
        raise CorruptRecordError(record_id, "a column has the wrong type")
    try:
        snapshot = json.loads(payload, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise CorruptRecordError(record_id, "payload is not valid strict JSON") from None
    if not isinstance(snapshot, dict):
        raise CorruptRecordError(record_id, "payload is not a JSON object")
    if snapshot.get("observed_at") != observed_at:
        raise CorruptRecordError(record_id, "payload observed_at differs from the stored column")
    payload_version = snapshot.get("schema_version")
    if type(payload_version) is not int or payload_version != schema_version:
        raise CorruptRecordError(record_id, "payload schema_version differs from the stored column")
    return StoredSnapshot(record_id, observed_at, schema_version, stored_at, snapshot)


# --------------------------------------------------------------------------
# Repository
# --------------------------------------------------------------------------

class SnapshotRepository:
    """Explicit-path SQLite store for validated snapshots. See the module docstring.

    ``db_path`` is a ``str`` or path-like (relative paths are made absolute now, so a
    later ``chdir`` cannot redirect the repository); ``":memory:"`` and empty paths
    are rejected because each call opens its own connection. ``clock`` (returns an
    aware UTC ``datetime``) and ``busy_timeout_s`` (seconds to wait for a locked
    database, 0-60) exist mainly so tests are deterministic. Invalid constructor
    arguments raise ``TypeError`` / ``ValueError`` (caller bugs).
    """

    def __init__(self, db_path: str | os.PathLike[str], *,
                 clock: Callable[[], datetime] | None = None,
                 busy_timeout_s: float = DEFAULT_BUSY_TIMEOUT_S) -> None:
        if not isinstance(db_path, (str, os.PathLike)):
            raise TypeError("db_path must be a str or path-like object")
        path = os.fspath(db_path)
        if type(path) is not str:
            raise TypeError("db_path must be a text path")
        if path == "" or path == ":memory:":
            raise ValueError('db_path must name a file ("" and ":memory:" are not supported)')
        if "\x00" in path:
            raise ValueError("db_path must not contain a null character")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable or None")
        if (type(busy_timeout_s) not in (int, float)
                or not 0 <= busy_timeout_s <= _MAX_BUSY_TIMEOUT_S):
            raise ValueError(f"busy_timeout_s must be a number from 0 to {_MAX_BUSY_TIMEOUT_S:g}")
        self._path = os.path.abspath(path)
        self._clock = _utc_now if clock is None else clock
        self._busy_timeout_s = float(busy_timeout_s)

    @property
    def path(self) -> str:
        """Absolute path of the database file."""
        return self._path

    def __repr__(self) -> str:
        return f"SnapshotRepository({self._path!r})"

    def _connect(self, *, must_exist: bool, read_only: bool) -> sqlite3.Connection:
        """Open a fresh connection (caller closes it). Only ``initialize`` may create the file."""
        if must_exist and not os.path.isfile(self._path):
            raise RepositoryNotInitializedError(
                f"database file {self._path!r} does not exist; call initialize() first")
        conn = sqlite3.connect(self._path, timeout=self._busy_timeout_s, isolation_level=None)
        try:
            if read_only:
                conn.execute("PRAGMA query_only = ON")
        except BaseException:
            conn.close()
            raise
        return conn

    def initialize(self) -> None:
        """Create the schema if it is missing. Idempotent; never deletes or alters data.

        Creates the database file if needed (but not its parent directory).
        """
        with _translate_db_errors():
            with closing(self._connect(must_exist=False, read_only=False)) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    if not _schema_present(conn):
                        if conn.execute("PRAGMA user_version").fetchone()[0] != 0:
                            raise StorageDatabaseError(
                                "the database already uses a different schema marker; "
                                "it was left unchanged")
                        conn.execute(_CREATE_TABLE)
                        conn.execute(_SET_MARKER)
                    conn.execute(_CREATE_INDEX)
                    conn.execute("COMMIT")
                except BaseException:
                    _rollback(conn)
                    raise

    def save_snapshot(self, snapshot: Any) -> int:
        """Validate and store one snapshot; return its row id.

        Raises ``InvalidSnapshotError`` (no database access), ``RepositoryNotInitializedError``,
        or ``StorageDatabaseError``. On any failure no record is left behind.
        """
        result = validate_snapshot(snapshot)
        if not result.valid:
            raise InvalidSnapshotError(tuple(result.issues))
        try:
            payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=True, allow_nan=False)
        except ValueError as exc:     # e.g. an integer beyond Python's string-conversion limit
            raise InvalidSnapshotError(
                (), "snapshot is valid but cannot be serialized as strict JSON") from exc
        stored_at = _format_utc(self._clock())

        with _translate_db_errors():
            with closing(self._connect(must_exist=True, read_only=False)) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    if not _schema_present(conn):
                        raise RepositoryNotInitializedError(
                            "the database has no snapshots table; call initialize() first")
                    cursor = conn.execute(
                        _INSERT, (snapshot["observed_at"], snapshot["schema_version"],
                                  payload, stored_at))
                    record_id = cursor.lastrowid
                    conn.execute("COMMIT")
                except BaseException:
                    _rollback(conn)
                    raise
        return record_id

    def get_snapshots(self, limit: int = DEFAULT_LIMIT) -> tuple[StoredSnapshot, ...]:
        """Return the most recent ``limit`` records, oldest first (ties by insertion order).

        Raises ``InvalidQueryError`` (checked before any database access),
        ``RepositoryNotInitializedError``, ``StorageDatabaseError`` or
        ``CorruptRecordError``. Read-only.
        """
        _check_limit(limit)
        with _translate_db_errors():
            with closing(self._connect(must_exist=True, read_only=True)) as conn:
                conn.execute("BEGIN")
                if not _schema_present(conn):
                    raise RepositoryNotInitializedError(
                        "the database has no snapshots table; call initialize() first")
                rows = conn.execute(_SELECT_RECENT, (limit,)).fetchall()
        return tuple(_decode_row(row) for row in rows)
