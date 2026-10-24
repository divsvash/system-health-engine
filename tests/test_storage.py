"""Tests for storage.py (SQLite snapshot repository).

Every test uses its own temporary database (pytest ``tmp_path``). Fixtures reuse the
collector-shaped builders from test_validator.py (no psutil needed); the section on
collector-produced snapshots needs psutil and is skipped cleanly without it.

Written to be Windows-safe: every raw SQLite connection is explicitly closed (a
``with sqlite3.connect()`` block does NOT close it, and an open handle blocks deleting
the temp file on Windows), and file names avoid characters Windows forbids. Nothing
here needs admin rights, internet access, or any change outside the temp directory.
"""

from __future__ import annotations

import ast
import builtins
import copy
import dataclasses
import hashlib
import json
import os
import runpy
import socket
import sqlite3
import subprocess
import sys
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import storage
import test_validator as tv
from storage import (
    DEFAULT_LIMIT, MAX_LIMIT, CorruptRecordError, InvalidQueryError, InvalidSnapshotError,
    RepositoryNotInitializedError, SnapshotRepository, StorageDatabaseError, StorageError,
    StoredSnapshot,
)
from validator import validate_snapshot

FIXED_NOW = datetime(2026, 10, 24, 12, 30, 45, tzinfo=timezone.utc)
FIXED_STORED_AT = "2026-10-24T12:30:45Z"
T1, T2, T3 = "2026-10-18T10:00:00Z", "2026-10-18T11:00:00Z", "2026-10-18T12:00:00Z"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

@contextmanager
def raw(path):
    """A raw, autocommit SQLite connection that is always closed."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        yield conn
    finally:
        conn.close()


def query(path, sql, params=()):
    with raw(path) as conn:
        return conn.execute(sql, params).fetchall()


def count(path):
    return query(path, "SELECT COUNT(*) FROM snapshots")[0][0]


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def snap_at(timestamp, label=None):
    snapshot = tv.build_snapshot()
    snapshot["observed_at"] = timestamp
    if label is not None:
        snapshot["system"]["os_name"]["value"] = label     # differs between otherwise equal snapshots
    return snapshot


def minute(i):
    return f"2026-01-01T{i // 60:02d}:{i % 60:02d}:00Z"


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "snapshots.sqlite3"


@pytest.fixture
def repo(db_path):
    repository = SnapshotRepository(db_path, clock=lambda: FIXED_NOW)
    repository.initialize()
    return repository


def listing(path):
    return sorted(p.name for p in Path(path).iterdir())


# --------------------------------------------------------------------------
# Import and construction have no side effects
# --------------------------------------------------------------------------

def test_importing_the_module_touches_nothing(tmp_path, monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("module import touched the database or the filesystem")

    monkeypatch.chdir(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", blocked)
        patch.setattr(builtins, "open", blocked)
        patch.setattr(os, "makedirs", blocked)
        runpy.run_path(storage.__file__, run_name="storage_import_probe")   # executes the module body afresh
    assert listing(tmp_path) == []


def test_module_level_code_only_defines_things():
    tree = ast.parse(Path(storage.__file__).read_text(encoding="utf-8"))
    allowed = (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign)
    docstring = tree.body[0]
    assert isinstance(docstring, ast.Expr) and isinstance(docstring.value, ast.Constant)
    for node in tree.body[1:]:
        assert isinstance(node, allowed), ast.dump(node)[:80]
        if isinstance(node, ast.Assign):
            assert not any(isinstance(sub, ast.Call) and "connect" in ast.unparse(sub.func)
                           for sub in ast.walk(node.value))


def test_constructing_a_repository_does_not_touch_the_disk(tmp_path, monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("constructor touched the database")

    monkeypatch.setattr(sqlite3, "connect", blocked)
    repository = SnapshotRepository(tmp_path / "never_created.sqlite3")
    assert listing(tmp_path) == [] and repository.path == str(tmp_path / "never_created.sqlite3")


@pytest.mark.parametrize("bad,error", [
    (None, TypeError), (5, TypeError), (b"bytes.sqlite3", TypeError), (["x"], TypeError),
    ("", ValueError), (":memory:", ValueError), ("bad\x00name", ValueError),
])
def test_invalid_database_path_is_rejected(bad, error):
    with pytest.raises(error):
        SnapshotRepository(bad)


@pytest.mark.parametrize("kwargs,error", [
    ({"clock": 5}, TypeError), ({"busy_timeout_s": -1}, ValueError), ({"busy_timeout_s": 61}, ValueError),
    ({"busy_timeout_s": float("nan")}, ValueError), ({"busy_timeout_s": True}, ValueError),
    ({"busy_timeout_s": "5"}, ValueError),
])
def test_invalid_constructor_options_are_rejected(tmp_path, kwargs, error):
    with pytest.raises(error):
        SnapshotRepository(tmp_path / "x.sqlite3", **kwargs)


def test_relative_paths_are_fixed_at_construction(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    monkeypatch.chdir(tmp_path / "a")
    repository = SnapshotRepository("rel.sqlite3")
    repository.initialize()
    monkeypatch.chdir(tmp_path / "b")                    # a later chdir must not redirect the repository
    repository.save_snapshot(tv.build_snapshot())
    assert listing(tmp_path / "a") == ["rel.sqlite3"] and listing(tmp_path / "b") == []


def test_str_and_pathlike_paths_are_equivalent_and_unusual_names_work(tmp_path):
    name = "snap #1 é.sqlite3"                        # space, '#', non-ASCII (valid on Windows too)
    repository = SnapshotRepository(str(tmp_path / name))
    repository.initialize()
    repository.save_snapshot(tv.build_snapshot())
    assert SnapshotRepository(tmp_path / name).get_snapshots()[0].id == 1
    assert listing(tmp_path) == [name]


# --------------------------------------------------------------------------
# initialize(): explicit, idempotent, non-destructive
# --------------------------------------------------------------------------

def test_initialize_creates_the_documented_schema(db_path):
    SnapshotRepository(db_path).initialize()
    columns = [(r[1], r[2], r[3], r[5]) for r in query(db_path, "PRAGMA table_info(snapshots)")]
    assert columns == [("id", "INTEGER", 0, 1), ("observed_at", "TEXT", 1, 0),
                       ("schema_version", "INTEGER", 1, 0), ("payload_json", "TEXT", 1, 0),
                       ("stored_at", "TEXT", 1, 0)]
    assert query(db_path, "PRAGMA user_version") == [(1,)]
    index = query(db_path, "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
                           "AND tbl_name = 'snapshots' AND sql IS NOT NULL")
    assert [(name, " ".join(sql.split())) for name, sql in index] == [
        ("idx_snapshots_observed_at",
         "CREATE INDEX idx_snapshots_observed_at ON snapshots (observed_at, id)")]
    table_sql = query(db_path, "SELECT sql FROM sqlite_master WHERE name = 'snapshots'")[0][0]
    assert "AUTOINCREMENT" in table_sql
    assert {row[0] for row in query(db_path, "SELECT name FROM sqlite_master WHERE type = 'table'")} == {
        "snapshots", "sqlite_sequence"}


def test_repeated_initialization_is_safe_and_keeps_data(repo, db_path):
    repo.save_snapshot(tv.build_snapshot())
    schema = query(db_path, "SELECT type, name, sql FROM sqlite_master ORDER BY name")
    for _ in range(3):
        repo.initialize()
        SnapshotRepository(db_path).initialize()
    assert query(db_path, "SELECT type, name, sql FROM sqlite_master ORDER BY name") == schema
    assert count(db_path) == 1
    assert [r.id for r in repo.get_snapshots()] == [1]


def test_initialize_recreates_a_dropped_index_without_touching_rows(repo, db_path):
    repo.save_snapshot(tv.build_snapshot())
    query(db_path, "DROP INDEX idx_snapshots_observed_at")
    repo.initialize()
    assert query(db_path, "SELECT name FROM sqlite_master WHERE name = 'idx_snapshots_observed_at'")
    assert count(db_path) == 1


def test_initialize_adds_its_table_to_a_database_that_has_other_tables(db_path):
    query(db_path, "CREATE TABLE other (x TEXT)")
    query(db_path, "INSERT INTO other VALUES ('keep me')")
    SnapshotRepository(db_path).initialize()
    assert query(db_path, "SELECT x FROM other") == [("keep me",)]
    assert count(db_path) == 0


def test_initialize_refuses_an_incompatible_snapshots_table_and_leaves_it_alone(db_path):
    query(db_path, "CREATE TABLE snapshots (id INTEGER, foo TEXT)")
    query(db_path, "INSERT INTO snapshots VALUES (1, 'precious')")
    before = sha256(db_path)
    repository = SnapshotRepository(db_path)
    for operation in (repository.initialize, lambda: repository.save_snapshot(tv.build_snapshot()),
                      repository.get_snapshots):
        with pytest.raises(StorageDatabaseError, match="incompatible"):
            operation()
    assert sha256(db_path) == before


def test_initialize_refuses_a_foreign_database_with_another_schema_marker(db_path):
    query(db_path, "CREATE TABLE other (x TEXT)")
    query(db_path, "PRAGMA user_version = 7")
    before = sha256(db_path)
    with pytest.raises(StorageDatabaseError, match="schema marker"):
        SnapshotRepository(db_path).initialize()
    assert sha256(db_path) == before
    assert query(db_path, "SELECT name FROM sqlite_master WHERE name = 'snapshots'") == []


def test_initialize_does_not_create_missing_directories(tmp_path):
    repository = SnapshotRepository(tmp_path / "missing_dir" / "db.sqlite3")
    with pytest.raises(StorageDatabaseError):
        repository.initialize()
    assert listing(tmp_path) == []


def test_initialize_on_a_non_sqlite_file_fails_without_modifying_it(db_path):
    db_path.write_bytes(b"this is definitely not a sqlite database " * 20)
    before = sha256(db_path)
    with pytest.raises(StorageDatabaseError):
        SnapshotRepository(db_path).initialize()
    assert sha256(db_path) == before


def test_table_constraints_reject_wrongly_typed_rows(repo, db_path):
    good = ("2026-10-18T10:00:00Z", 1, "{}", FIXED_STORED_AT)
    sql = "INSERT INTO snapshots (observed_at, schema_version, payload_json, stored_at) VALUES (?, ?, ?, ?)"
    # Note: an integer in a TEXT column is converted to text by SQLite's column affinity
    # (so it passes the typeof check); a BLOB is not converted and is rejected.
    for index, bad in [(0, None), (0, b"\x00"), (1, "x"), (1, None), (2, None), (2, b"\x00"),
                       (3, None), (3, b"\x00")]:
        row = list(good)
        row[index] = bad
        with pytest.raises(sqlite3.IntegrityError):
            query(db_path, sql, tuple(row))
    assert count(db_path) == 0


# --------------------------------------------------------------------------
# Saving before initialization fails clearly and creates nothing
# --------------------------------------------------------------------------

def test_save_and_get_before_initialization_fail_clearly_and_create_no_file(db_path, tmp_path):
    repository = SnapshotRepository(db_path)
    with pytest.raises(RepositoryNotInitializedError, match="initialize"):
        repository.save_snapshot(tv.build_snapshot())
    with pytest.raises(RepositoryNotInitializedError, match="initialize"):
        repository.get_snapshots()
    assert listing(tmp_path) == []                      # neither call created or initialized the file


def test_an_existing_database_without_the_table_is_not_silently_initialized(db_path):
    query(db_path, "CREATE TABLE other (x TEXT)")
    before = sha256(db_path)
    repository = SnapshotRepository(db_path)
    with pytest.raises(RepositoryNotInitializedError):
        repository.save_snapshot(tv.build_snapshot())
    with pytest.raises(RepositoryNotInitializedError):
        repository.get_snapshots()
    assert sha256(db_path) == before
    assert query(db_path, "SELECT name FROM sqlite_master WHERE type = 'table'") == [("other",)]


def test_a_directory_in_place_of_the_database_file_fails_clearly(tmp_path):
    (tmp_path / "dir.sqlite3").mkdir()
    repository = SnapshotRepository(tmp_path / "dir.sqlite3")
    with pytest.raises(StorageDatabaseError):
        repository.initialize()
    with pytest.raises(RepositoryNotInitializedError):
        repository.save_snapshot(tv.build_snapshot())


def test_a_non_sqlite_file_is_a_database_error_for_save_and_get(db_path):
    db_path.write_bytes(b"not a database " * 50)
    before = sha256(db_path)
    repository = SnapshotRepository(db_path)
    with pytest.raises(StorageDatabaseError):
        repository.save_snapshot(tv.build_snapshot())
    with pytest.raises(StorageDatabaseError):
        repository.get_snapshots()
    assert sha256(db_path) == before


# --------------------------------------------------------------------------
# Save and retrieve: round trips
# --------------------------------------------------------------------------

def test_save_returns_the_id_and_the_record_has_the_documented_fields(repo):
    snapshot = tv.build_snapshot()
    assert repo.save_snapshot(snapshot) == 1
    assert repo.save_snapshot(snapshot) == 2            # same snapshot twice = two records (no dedupe)
    (first, second) = repo.get_snapshots()
    assert isinstance(first, StoredSnapshot)
    assert (first.id, first.observed_at, first.schema_version, first.stored_at) == (
        1, snapshot["observed_at"], 1, FIXED_STORED_AT)
    assert second.id == 2


@pytest.mark.parametrize("name", tv.VALID_VARIANTS)
def test_every_valid_variant_round_trips(repo, name):
    original = tv.VALID_VARIANTS[name]()
    snapshot = copy.deepcopy(original)
    repo.save_snapshot(snapshot)
    (record,) = repo.get_snapshots()
    assert record.snapshot == original
    assert json.loads(json.dumps(original)) == record.snapshot      # JSON-compatible content preserved
    assert validate_snapshot(record.snapshot).valid
    assert record.snapshot is not snapshot                           # content, not object identity


def test_all_variants_saved_together_come_back_in_insertion_order(repo):
    originals = [make() for make in tv.VALID_VARIANTS.values()]      # all share one observed_at
    for original in originals:
        repo.save_snapshot(original)
    records = repo.get_snapshots()
    assert [r.id for r in records] == list(range(1, len(originals) + 1))
    assert [r.snapshot for r in records] == originals


def test_floats_integers_unicode_and_sql_metacharacters_survive_exactly(repo, db_path):
    snapshot = tv.build_snapshot()
    snapshot["cpu"]["utilization_percent"]["value"] = 0.1 + 0.2                  # 0.30000000000000004
    snapshot["memory"]["total_bytes"]["value"] = 2 ** 63 + 12345                  # beyond SQLite's int64
    snapshot["memory"]["available_bytes"]["value"] = 2 ** 62
    evil = "'; DROP TABLE snapshots; -- Ω:\\ \"quoted\" ☃ \U0001F600"
    snapshot["disk"]["drives"][0]["mountpoint"] = evil
    repo.save_snapshot(snapshot)
    (record,) = repo.get_snapshots()
    assert record.snapshot == snapshot
    assert record.snapshot["cpu"]["utilization_percent"]["value"] == 0.30000000000000004
    assert record.snapshot["memory"]["total_bytes"]["value"] == 2 ** 63 + 12345
    assert record.snapshot["disk"]["drives"][0]["mountpoint"] == evil
    assert count(db_path) == 1                                                    # the table survived


def test_payload_is_deterministic_strict_json(repo, db_path):
    snapshot = tv.build_snapshot()
    snapshot["disk"]["drives"][0]["mountpoint"] = "D:\\Caf\u00e9 \u2603"        # non-ASCII must be escaped
    repo.save_snapshot(snapshot)
    repo.save_snapshot(tv.reordered(snapshot))                                    # same content, other key order
    first, second = [row[0] for row in query(db_path, "SELECT payload_json FROM snapshots ORDER BY id")]
    assert first == second
    assert first == json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    assert first.isascii() and "NaN" not in first and "Infinity" not in first
    assert json.loads(first) == snapshot


def test_stored_columns_mirror_the_snapshot(repo, db_path):
    repo.save_snapshot(snap_at(T2))
    assert query(db_path, "SELECT id, observed_at, schema_version, stored_at FROM snapshots") == [
        (1, T2, 1, FIXED_STORED_AT)]
    assert query(db_path, "SELECT typeof(observed_at), typeof(schema_version), typeof(payload_json), "
                          "typeof(stored_at) FROM snapshots") == [("text", "integer", "text", "text")]


def test_no_derived_data_is_stored(repo, db_path):
    repo.save_snapshot(tv.build_snapshot(memory=None) if False else tv.build_snapshot())
    assert [r[1] for r in query(db_path, "PRAGMA table_info(snapshots)")] == [
        "id", "observed_at", "schema_version", "payload_json", "stored_at"]
    assert "finding" not in query(db_path, "SELECT payload_json FROM snapshots")[0][0].lower()


def test_no_leftover_journal_files_and_the_file_can_be_deleted_afterwards(repo, db_path, tmp_path):
    repo.save_snapshot(tv.build_snapshot())
    repo.get_snapshots()
    SnapshotRepository(db_path).initialize()
    assert listing(tmp_path) == [db_path.name]
    os.remove(db_path)                                  # fails on Windows if any handle were left open
    assert listing(tmp_path) == []


# --------------------------------------------------------------------------
# Rejection of invalid snapshots
# --------------------------------------------------------------------------

INVALID = {
    "none": lambda: None, "list": lambda: [], "string": lambda: "snapshot",
    "missing_field": lambda: tv.without("memory"),
    "unsupported_version": lambda: tv.mutated("schema_version", 2),
    "bad_timestamp": lambda: tv.mutated("observed_at", "yesterday"),
    "naive_timestamp": lambda: tv.mutated("observed_at", "2026-10-18T10:00:00"),
    "cpu_out_of_range": lambda: tv.mutated("cpu.utilization_percent.value", 140.0),
    "numeric_string": lambda: tv.mutated("memory.total_bytes.value", "1000"),
    "bool_as_int": lambda: tv.mutated("memory.total_bytes.value", True),
    "nan": lambda: tv.mutated("cpu.utilization_percent.value", float("nan")),
    "unknown_field": lambda: {**tv.build_snapshot(), "extra": 1},
    "contradictory_status": lambda: {**tv.build_snapshot(), "collection_status": "failed"},
}


@pytest.mark.parametrize("name", INVALID)
def test_invalid_snapshots_are_rejected_without_creating_a_record(repo, db_path, name):
    snapshot = INVALID[name]()
    expected = validate_snapshot(snapshot)
    before = repr(snapshot)
    with pytest.raises(InvalidSnapshotError) as caught:
        repo.save_snapshot(snapshot)
    assert caught.value.issues == tuple(expected.issues) and caught.value.issues
    assert isinstance(caught.value, StorageError)
    assert repr(snapshot) == before                      # not repaired, not modified
    assert count(db_path) == 0


def test_invalid_snapshot_is_rejected_even_when_the_repository_is_uninitialized(db_path, tmp_path):
    repository = SnapshotRepository(db_path)
    with pytest.raises(InvalidSnapshotError):
        repository.save_snapshot(INVALID["cpu_out_of_range"]())
    assert listing(tmp_path) == []                       # validation happens before any database access


def test_a_rejected_snapshot_does_not_disturb_stored_records(repo):
    repo.save_snapshot(snap_at(T1, "first"))
    with pytest.raises(InvalidSnapshotError):
        repo.save_snapshot(INVALID["nan"]())
    repo.save_snapshot(snap_at(T2, "second"))
    assert [r.snapshot["system"]["os_name"]["value"] for r in repo.get_snapshots()] == ["first", "second"]


def test_error_message_does_not_echo_untrusted_values(repo):
    snapshot = tv.mutated("memory.total_bytes.value", "SECRET-VALUE")
    with pytest.raises(InvalidSnapshotError) as caught:
        repo.save_snapshot(snapshot)
    assert "SECRET-VALUE" not in str(caught.value)


@pytest.mark.skipif(sys.get_int_max_str_digits() == 0, reason="int string-conversion limit disabled")
def test_a_valid_snapshot_that_cannot_be_serialized_is_rejected_cleanly(repo, db_path):
    snapshot = tv.build_snapshot()
    snapshot["memory"]["total_bytes"]["value"] = 10 ** 5000          # valid per the validator
    assert validate_snapshot(snapshot).valid
    with pytest.raises(InvalidSnapshotError, match="serialized") as caught:
        repo.save_snapshot(snapshot)
    assert caught.value.issues == () and count(db_path) == 0


# --------------------------------------------------------------------------
# Input is never modified
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", tv.VALID_VARIANTS)
def test_input_is_unchanged_by_a_successful_save(repo, name):
    snapshot = tv.VALID_VARIANTS[name]()
    original, before = copy.deepcopy(snapshot), repr(snapshot)
    repo.save_snapshot(snapshot)
    assert snapshot == original and repr(snapshot) == before
    repo.save_snapshot(tv.freeze(tv.VALID_VARIANTS[name]()))         # any mutation attempt would raise


def test_input_is_unchanged_by_a_failed_save(repo):
    snapshot = INVALID["cpu_out_of_range"]()
    original = copy.deepcopy(snapshot)
    with pytest.raises(InvalidSnapshotError):
        repo.save_snapshot(snapshot)
    assert snapshot == original and snapshot["cpu"]["utilization_percent"]["value"] == 140.0


# --------------------------------------------------------------------------
# Ordering, ties and the limit window
# --------------------------------------------------------------------------

def test_snapshots_come_back_oldest_first_whatever_the_insertion_order(repo):
    for stamp in (T3, T1, T2):
        repo.save_snapshot(snap_at(stamp))
    assert [r.observed_at for r in repo.get_snapshots()] == [T1, T2, T3]
    assert [r.id for r in repo.get_snapshots()] == [2, 3, 1]


def test_equal_timestamps_are_ordered_by_insertion_id(repo):
    labels = ["A", "B", "C", "D"]
    for label in labels:
        repo.save_snapshot(snap_at(T2, label))
    repo.save_snapshot(snap_at(T1, "early"))
    repo.save_snapshot(snap_at(T3, "late"))
    records = repo.get_snapshots()
    assert [r.snapshot["system"]["os_name"]["value"] for r in records] == ["early", *labels, "late"]
    assert [r.id for r in records if r.observed_at == T2] == [1, 2, 3, 4]
    assert repo.get_snapshots() == records                           # repeatable


@pytest.mark.parametrize("with_index", [True, False])
def test_ties_are_stable_inside_a_limited_window(repo, db_path, with_index):
    if not with_index:
        query(db_path, "DROP INDEX idx_snapshots_observed_at")      # ordering must not depend on the index
    for label in "ABCDEF":
        repo.save_snapshot(snap_at(T2, label))
    window = repo.get_snapshots(limit=3)
    assert [r.snapshot["system"]["os_name"]["value"] for r in window] == ["D", "E", "F"]


def test_ordering_uses_observed_at_not_the_time_of_storage(db_path):
    clock_values = iter([FIXED_NOW + timedelta(hours=h) for h in (3, 2, 1)])    # stored_at goes backwards
    repository = SnapshotRepository(db_path, clock=lambda: next(clock_values))
    repository.initialize()
    for stamp in (T1, T2, T3):
        repository.save_snapshot(snap_at(stamp))
    records = repository.get_snapshots()
    assert [r.observed_at for r in records] == [T1, T2, T3]
    assert [r.stored_at for r in records] == sorted([r.stored_at for r in records], reverse=True)


def test_limit_returns_the_most_recent_records_oldest_first(repo):
    for i in range(5):
        repo.save_snapshot(snap_at(minute(i), f"s{i}"))
    window = repo.get_snapshots(limit=2)
    assert [r.snapshot["system"]["os_name"]["value"] for r in window] == ["s3", "s4"]
    assert [r.snapshot["system"]["os_name"]["value"] for r in repo.get_snapshots(limit=5)] == [
        f"s{i}" for i in range(5)]
    assert len(repo.get_snapshots(limit=50)) == 5                    # a limit above the count is fine


def test_default_limit_is_100_and_bounded(repo):
    assert DEFAULT_LIMIT == 100
    for i in reversed(range(105)):                                   # inserted newest-first on purpose
        repo.save_snapshot(snap_at(minute(i)))
    records = repo.get_snapshots()
    assert len(records) == 100
    assert [r.observed_at for r in records] == [minute(i) for i in range(5, 105)]


def test_empty_repository_returns_an_empty_tuple(repo):
    assert repo.get_snapshots() == ()


def test_the_maximum_limit_is_accepted(repo):
    assert MAX_LIMIT == 1000
    repo.save_snapshot(tv.build_snapshot())
    assert len(repo.get_snapshots(limit=1)) == 1 and len(repo.get_snapshots(limit=MAX_LIMIT)) == 1


class _MyInt(int):
    pass


@pytest.mark.parametrize("limit", [
    True, False, 1.0, 2.5, float("nan"), float("inf"), "5", None, [], (1,), _MyInt(5),
    0, -1, -100, MAX_LIMIT + 1, 10 ** 9, 2 ** 70])
def test_invalid_limits_are_rejected(repo, limit):
    with pytest.raises(InvalidQueryError):
        repo.get_snapshots(limit=limit)


def test_invalid_limit_is_reported_before_the_database_is_touched(db_path, tmp_path):
    with pytest.raises(InvalidQueryError):
        SnapshotRepository(db_path).get_snapshots(limit=0)           # not RepositoryNotInitializedError
    assert listing(tmp_path) == []


def test_exception_hierarchy():
    for error in (InvalidSnapshotError, RepositoryNotInitializedError, StorageDatabaseError,
                  CorruptRecordError, InvalidQueryError):
        assert issubclass(error, StorageError)
    assert issubclass(InvalidQueryError, ValueError)
    assert not issubclass(StorageError, (ValueError, TypeError))


# --------------------------------------------------------------------------
# Returned records
# --------------------------------------------------------------------------

def test_records_are_frozen_and_not_hashable_and_results_are_a_tuple(repo):
    repo.save_snapshot(tv.build_snapshot())
    records = repo.get_snapshots()
    assert isinstance(records, tuple) and dataclasses.is_dataclass(records[0])
    with pytest.raises(dataclasses.FrozenInstanceError):
        records[0].id = 99
    with pytest.raises(dataclasses.FrozenInstanceError):
        records[0].snapshot = {}
    with pytest.raises(TypeError):
        hash(records[0])                                             # documented: holds a dict


def test_each_retrieval_returns_a_private_copy(repo, db_path):
    repo.save_snapshot(tv.build_snapshot())
    (first,) = repo.get_snapshots()
    first.snapshot["cpu"]["utilization_percent"]["value"] = 999.0    # the caller scribbles on its copy
    first.snapshot.clear()
    (second,) = repo.get_snapshots()
    assert second.snapshot == tv.build_snapshot()
    assert second.snapshot is not first.snapshot
    assert json.loads(query(db_path, "SELECT payload_json FROM snapshots")[0][0]) == tv.build_snapshot()


# --------------------------------------------------------------------------
# Retrieval cannot modify the database
# --------------------------------------------------------------------------

def test_retrieval_leaves_the_database_file_byte_identical(repo, db_path):
    for i in range(4):
        repo.save_snapshot(snap_at(minute(i), f"s{i}"))
    before = (sha256(db_path), db_path.stat().st_mtime_ns, db_path.stat().st_size)
    for limit in (1, 2, 50, DEFAULT_LIMIT):
        repo.get_snapshots(limit=limit)
    SnapshotRepository(db_path).get_snapshots()
    assert (sha256(db_path), db_path.stat().st_mtime_ns, db_path.stat().st_size) == before
    assert listing(db_path.parent) == [db_path.name]


def test_retrieval_only_issues_read_statements_on_a_query_only_connection(repo, monkeypatch):
    repo.save_snapshot(tv.build_snapshot())
    statements = []

    class Recording(sqlite3.Connection):
        def execute(self, sql, *args):
            statements.append(" ".join(sql.split()))
            return super().execute(sql, *args)

    real_connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: real_connect(*a, factory=Recording, **k))
    repo.get_snapshots()
    assert statements[0] == "PRAGMA query_only = ON"
    assert {s.split()[0] for s in statements} <= {"PRAGMA", "BEGIN", "SELECT"}


# --------------------------------------------------------------------------
# Persistence across repository objects
# --------------------------------------------------------------------------

def test_reopening_the_repository_preserves_records_and_continues_ids(db_path):
    first = SnapshotRepository(db_path, clock=lambda: FIXED_NOW)
    first.initialize()
    first.save_snapshot(snap_at(T1, "one"))
    first.save_snapshot(snap_at(T2, "two"))
    del first

    second = SnapshotRepository(db_path, clock=lambda: FIXED_NOW + timedelta(days=1))
    records = second.get_snapshots()
    assert [(r.id, r.stored_at) for r in records] == [(1, FIXED_STORED_AT), (2, FIXED_STORED_AT)]
    assert second.save_snapshot(snap_at(T3, "three")) == 3
    assert [r.snapshot["system"]["os_name"]["value"] for r in second.get_snapshots()] == [
        "one", "two", "three"]
    assert second.get_snapshots()[2].stored_at == "2026-10-25T12:30:45Z"
    second.initialize()                                              # re-initializing a populated database
    assert count(db_path) == 3


def test_ids_are_never_reused(repo, db_path):
    repo.save_snapshot(tv.build_snapshot())
    repo.save_snapshot(tv.build_snapshot())
    query(db_path, "DELETE FROM snapshots WHERE id = 2")             # external deletion (no API for it)
    assert repo.save_snapshot(tv.build_snapshot()) == 3


# --------------------------------------------------------------------------
# stored_at and the clock
# --------------------------------------------------------------------------

def test_default_clock_stamps_utc_time_of_writing(db_path):
    repository = SnapshotRepository(db_path)
    repository.initialize()
    before = datetime.now(timezone.utc).replace(microsecond=0)
    repository.save_snapshot(tv.build_snapshot())
    after = datetime.now(timezone.utc)
    stamp = repository.get_snapshots()[0].stored_at
    assert stamp.endswith("Z")
    assert before <= datetime.fromisoformat(stamp) <= after


@pytest.mark.parametrize("value,error", [
    (datetime(2026, 10, 24, 12, 0, 0), ValueError),                              # naive
    (datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))), ValueError),
    ("2026-10-24T12:00:00Z", TypeError), (None, TypeError), (1.0, TypeError),
])
def test_a_clock_that_is_not_aware_utc_is_a_caller_bug_and_stores_nothing(db_path, value, error):
    repository = SnapshotRepository(db_path, clock=lambda: value)
    repository.initialize()
    with pytest.raises(error):
        repository.save_snapshot(tv.build_snapshot())
    assert count(db_path) == 0


def test_a_failing_clock_propagates_and_stores_nothing(db_path):
    def broken():
        raise RuntimeError("clock bug")

    repository = SnapshotRepository(db_path, clock=broken)
    repository.initialize()
    with pytest.raises(RuntimeError, match="clock bug"):
        repository.save_snapshot(tv.build_snapshot())
    assert count(db_path) == 0


# --------------------------------------------------------------------------
# Failures leave no partial records
# --------------------------------------------------------------------------

def test_a_statement_failing_after_the_insert_leaves_no_record(repo, db_path):
    query(db_path, "CREATE TRIGGER boom AFTER INSERT ON snapshots "
                   "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    with pytest.raises(StorageDatabaseError, match="injected failure") as caught:
        repo.save_snapshot(tv.build_snapshot())
    assert isinstance(caught.value.__cause__, sqlite3.Error)
    assert count(db_path) == 0
    query(db_path, "DROP TRIGGER boom")
    assert repo.save_snapshot(tv.build_snapshot()) == 1              # the repository is still usable
    assert count(db_path) == 1


def test_a_failing_commit_is_rolled_back(repo, db_path, monkeypatch):
    class CommitFails(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql == "COMMIT":
                raise sqlite3.OperationalError("disk I/O error")
            return super().execute(sql, *args)

    with monkeypatch.context() as patch:
        real_connect = sqlite3.connect
        patch.setattr(sqlite3, "connect", lambda *a, **k: real_connect(*a, factory=CommitFails, **k))
        with pytest.raises(StorageDatabaseError, match="disk I/O error"):
            repo.save_snapshot(tv.build_snapshot())
    assert count(db_path) == 0
    assert repo.save_snapshot(tv.build_snapshot()) == 1


def test_a_locked_database_fails_cleanly_and_stores_nothing(db_path):
    repository = SnapshotRepository(db_path, busy_timeout_s=0.05)
    repository.initialize()
    with raw(db_path) as blocker:
        blocker.execute("BEGIN IMMEDIATE")                           # another writer holds the lock
        with pytest.raises(StorageDatabaseError, match="locked"):
            repository.save_snapshot(tv.build_snapshot())
        blocker.execute("ROLLBACK")
    assert count(db_path) == 0
    assert repository.save_snapshot(tv.build_snapshot()) == 1


def test_sqlite_programming_errors_are_not_disguised(repo, db_path, monkeypatch):
    class Buggy(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.startswith("INSERT"):
                raise sqlite3.ProgrammingError("bug in the caller of sqlite3")
            return super().execute(sql, *args)

    with monkeypatch.context() as patch:
        real_connect = sqlite3.connect
        patch.setattr(sqlite3, "connect", lambda *a, **k: real_connect(*a, factory=Buggy, **k))
        with pytest.raises(sqlite3.ProgrammingError):
            repo.save_snapshot(tv.build_snapshot())
    assert count(db_path) == 0


@pytest.mark.parametrize("error", [TypeError("bug"), KeyError("bug"), AttributeError("bug")])
def test_unexpected_errors_propagate_and_leave_no_record(repo, db_path, monkeypatch, error):
    def broken(*args, **kwargs):
        raise error
    monkeypatch.setattr(storage, "_schema_present", broken)
    with pytest.raises(type(error)):
        repo.save_snapshot(tv.build_snapshot())
    with pytest.raises(type(error)):
        repo.get_snapshots()
    monkeypatch.undo()
    assert count(db_path) == 0


# --------------------------------------------------------------------------
# Corrupted stored data is detected, never skipped
# --------------------------------------------------------------------------

def corrupt(db_path, record_id, column, value):
    with raw(db_path) as conn:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(f"UPDATE snapshots SET {column} = ? WHERE id = ?", (value, record_id))


@pytest.fixture
def three(repo):
    for stamp, label in ((T1, "a"), (T2, "b"), (T3, "c")):
        repo.save_snapshot(snap_at(stamp, label))
    return repo


@pytest.mark.parametrize("column,value,fragment", [
    ("payload_json", "{not json", "not valid strict JSON"),
    ("payload_json", "", "not valid strict JSON"),
    ("payload_json", "[1, 2]", "not a JSON object"),
    ("payload_json", "5", "not a JSON object"),
    ("payload_json", "null", "not a JSON object"),
    ("payload_json", '{"observed_at": "2026-10-18T11:00:00Z", "schema_version": 1, "x": NaN}',
     "not valid strict JSON"),
    ("payload_json", '{"observed_at": "2026-10-18T11:00:00Z", "schema_version": 1, "x": Infinity}',
     "not valid strict JSON"),
    ("payload_json", "[" * 100000, "not valid strict JSON"),                      # nesting beyond the parser
    ("payload_json", b"{}", "wrong type"),                                          # a BLOB, not text
    ("observed_at", "2026-10-18T23:59:59Z", "observed_at differs"),
    ("schema_version", 2, "schema_version differs"),
    ("stored_at", b"\x00", "wrong type"),
])
def test_corrupted_rows_raise_a_clear_error_naming_the_record(three, db_path, column, value, fragment):
    corrupt(db_path, 2, column, value)
    with pytest.raises(CorruptRecordError, match=fragment) as caught:
        three.get_snapshots()
    assert caught.value.record_id == 2 and "record 2" in str(caught.value)
    assert isinstance(caught.value, StorageError)


def test_a_payload_without_the_snapshot_keys_is_corrupt(three, db_path):
    corrupt(db_path, 2, "payload_json", "{}")
    with pytest.raises(CorruptRecordError, match="observed_at differs"):
        three.get_snapshots()


def test_a_corrupt_record_is_never_skipped_but_records_outside_the_window_are_not_read(three, db_path):
    corrupt(db_path, 1, "payload_json", "garbage")                                  # the oldest record
    with pytest.raises(CorruptRecordError) as caught:
        three.get_snapshots()                                                       # window includes it
    assert caught.value.record_id == 1
    assert [r.id for r in three.get_snapshots(limit=2)] == [2, 3]                   # window excludes it


def test_the_corruption_error_never_echoes_stored_content(three, db_path):
    corrupt(db_path, 2, "payload_json", "SECRET-CONTENT {not json")
    with pytest.raises(CorruptRecordError) as caught:
        three.get_snapshots()
    assert "SECRET" not in str(caught.value)


def test_retrieval_does_not_revalidate_so_old_schema_history_stays_readable(three, db_path):
    valid_json_that_the_validator_would_reject = json.dumps(
        {"observed_at": T2, "schema_version": 1, "legacy": True})
    corrupt(db_path, 2, "payload_json", valid_json_that_the_validator_would_reject)
    records = three.get_snapshots()
    assert records[1].snapshot["legacy"] is True and not validate_snapshot(records[1].snapshot).valid


# --------------------------------------------------------------------------
# Safety: no collection, no network, no subprocess; SQL only via parameters
# --------------------------------------------------------------------------

def test_operations_perform_no_network_subprocess_or_file_opens(db_path, monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("forbidden side effect")

    repository = SnapshotRepository(db_path)
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", blocked)
        patch.setattr(socket.socket, "connect", blocked)
        patch.setattr(socket, "create_connection", blocked)
        patch.setattr(socket, "getaddrinfo", blocked)
        patch.setattr(subprocess, "Popen", blocked)
        patch.setattr(os, "system", blocked)
        repository.initialize()
        repository.save_snapshot(tv.build_snapshot())
        assert len(repository.get_snapshots()) == 1


SOURCE = Path(storage.__file__).read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
ALLOWED_IMPORTS = {"__future__", "contextlib", "dataclasses", "datetime", "json", "os", "sqlite3",
                   "typing", "validator"}
FORBIDDEN_CALLS = {"open", "eval", "exec", "compile", "__import__", "input", "print", "getattr",
                   "setattr", "delattr", "globals", "locals", "vars"}


def test_static_imports_are_allowlisted_and_exclude_collection_and_network():
    imported = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= ALLOWED_IMPORTS, imported - ALLOWED_IMPORTS
    assert not imported & {"collector", "diagnostics", "psutil", "platform", "subprocess", "socket",
                           "urllib", "http", "shutil", "pathlib", "random", "threading", "sched"}


def test_static_only_allowlisted_os_and_sqlite3_attributes_are_used():
    used = {}
    for node in ast.walk(TREE):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id in {"os", "sqlite3"}):
            used.setdefault(node.value.id, set()).add(node.attr)
    assert used["os"] <= {"fspath", "path", "PathLike"}
    assert used["sqlite3"] <= {"connect", "Connection", "Error", "ProgrammingError", "InterfaceError",
                               "NotSupportedError"}
    os_path_calls = {node.attr for node in ast.walk(TREE) if isinstance(node, ast.Attribute)
                     and isinstance(node.value, ast.Attribute) and ast.unparse(node.value) == "os.path"}
    assert os_path_calls <= {"abspath", "isfile"}                    # read-only path queries only


def test_static_every_sql_statement_is_a_constant_never_built_from_data():
    executes = [node for node in ast.walk(TREE) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == "execute"]
    assert len(executes) >= 10
    for call in executes:
        assert isinstance(call.args[0], (ast.Constant, ast.Name)), ast.unparse(call)
        if isinstance(call.args[0], ast.Name):
            assert call.args[0].id.isupper() or call.args[0].id.startswith("_"), ast.unparse(call)
    assert all(not isinstance(a, ast.JoinedStr) for call in executes for a in call.args)


def test_static_no_dynamic_execution_and_no_broad_exception_handlers():
    called = {node.func.id for node in ast.walk(TREE)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert not called & FORBIDDEN_CALLS, called & FORBIDDEN_CALLS
    for handler in (n for n in ast.walk(TREE) if isinstance(n, ast.ExceptHandler)):
        assert handler.type is not None, "bare except"
        name = ast.unparse(handler.type)
        assert name != "Exception", "catches every error"
        if name == "BaseException":                                  # only the rollback-and-re-raise blocks
            assert any(isinstance(n, ast.Raise) for n in ast.walk(handler))
    assert {ast.unparse(h.type) for h in ast.walk(TREE) if isinstance(h, ast.ExceptHandler)} == {
        "_BUG_ERRORS", "sqlite3.Error", "BaseException", "(ValueError, RecursionError)", "ValueError"}


def test_static_no_module_level_mutable_state():
    mutable = [name for name, value in vars(storage).items()
               if not name.startswith("__") and isinstance(value, (list, dict, set, bytearray))]
    assert mutable == []


# --------------------------------------------------------------------------
# Snapshots produced by the real collector (psutil faked, plus one real run)
# --------------------------------------------------------------------------

@pytest.fixture
def tc():
    return pytest.importorskip("test_collector")                     # needs psutil, a runtime requirement


def test_collector_produced_snapshots_round_trip(tc, monkeypatch, repo):
    scenarios = {"complete": {}, "all_fail": dict(
        cpu_percent=tc.boom(), cpu_count=tc.boom(), virtual_memory=tc.boom(),
        disk_partitions=tc.boom(), net_if_stats=tc.boom()), **tc.PARTIAL_CASES}
    originals = []
    for overrides in scenarios.values():
        snapshot = tc.scan(monkeypatch, **overrides)
        originals.append(copy.deepcopy(snapshot))
        repo.save_snapshot(snapshot)
        assert snapshot == originals[-1]                             # input untouched
    records = repo.get_snapshots(limit=MAX_LIMIT)
    assert [r.snapshot for r in records] == originals
    assert all(validate_snapshot(r.snapshot).valid for r in records)
    assert {r.schema_version for r in records} == {1}


def test_a_real_snapshot_from_this_host_round_trips(tc, repo):
    import collector
    snapshot = collector.collect_system_snapshot(cpu_sample_interval=0.1)
    repo.save_snapshot(snapshot)
    (record,) = repo.get_snapshots()
    assert record.snapshot == json.loads(json.dumps(snapshot))
    assert record.observed_at == snapshot["observed_at"]


# What these tests do NOT prove:
# * They ran on whatever platform executes them; SQLite file-locking, antivirus and
#   handle-release behaviour on Windows 11 are only exercised if the suite is run there.
# * Atomicity under a real crash or power loss is SQLite's guarantee, not something these
#   tests demonstrate; they inject failures at the SQL and connection layer only.
# * Concurrent writers from several processes are checked only for the "database is locked"
#   case, not under sustained contention.
# * Passing validation and storage says nothing about whether the readings are true.
