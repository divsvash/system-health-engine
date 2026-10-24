# system-health-agent

A one-shot, strictly read-only collector for **Windows 11 x64** (Python 3.12+,
ordinary user privileges). It reports what it measured and what it could not
measure. The collector does **not** diagnose, interpret, repair, or suggest
anything; the separate validator and diagnostic engine (later sections) sit on
top of its output.

## Scope

| Group   | Collected |
|---------|-----------|
| system  | OS name, release, version string, machine architecture, logical CPU count |
| cpu     | Utilization % (sampled over a bounded interval), logical CPU count |
| memory  | Total bytes, available bytes, used % (as reported by psutil) |
| disk    | Per logical volume: total / used / free bytes and used % |
| network | Per interface: up/down flag and reported link speed (Mbps) |

Also: UTC observation timestamp and schema version.

**Deliberately omitted:** hostname (identifies the machine/user, not needed for
a single-machine local report), serial numbers, hardware IDs, account names.

**Not in the collector (or anywhere in the project yet):** LLM, GUI,
scheduler/service, battery, thermals, process monitoring, repairs. Diagnostic
rules live in `diagnostics.py`, not in the collector.

## Read-only design

Allowed APIs (nothing else is used):

- `psutil.cpu_percent`, `cpu_count`, `virtual_memory`, `disk_partitions`,
  `disk_usage`, `net_if_stats`
- `platform.system/release/version/machine`, `datetime.now`

Not used: `subprocess`, shells, PowerShell, WMI, registry, file writes,
network requests, process control, elevation. The collector accepts no
commands, paths or queries from callers; its only parameter is the CPU
sampling interval.

### Limits of the read-only guarantee

Read-only behavior is **not formally proven**. The test suite includes static
checks (import and attribute allowlists) and runtime tripwires (sockets,
subprocess, `os.system`, file writes), but these only cover this file's source
text and the paths they patch. They cannot see what `psutil` or the standard
library do internally, and a later edit could change code and allowlist
together. Tests run against fakes, so they show the collector's own logic is
side-effect-free, not that every real OS call is. Nothing here was verified on
Windows 11 itself. Python may also write `__pycache__` on import, and pytest
may write `.pytest_cache`; those are interpreter/tooling behavior, not the
collector (use `-B` and `-p no:cacheprovider` to suppress them).

## Setup, tests, demo (Windows PowerShell)

Activation-free commands (avoid PowerShell execution-policy issues):

```powershell
cd system-health-agent
py -3 --version                       # must be 3.12 or newer
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -B demo.py  # prints one snapshot as JSON to stdout
```

Runtime-only install (no test tools): `pip install -r requirements.txt`.
Package installation is a setup step, never part of the collector.

Inline alternative to `demo.py`:

```powershell
.\.venv\Scripts\python.exe -B -c "import json, collector; print(json.dumps(collector.collect_system_snapshot(), indent=2))"
```

## API

```python
from collector import collect_system_snapshot
snapshot = collect_system_snapshot()                       # 0.5 s CPU sample
snapshot = collect_system_snapshot(cpu_sample_interval=1)  # allowed: 0.1–5.0 s
```

Returns a plain, JSON-serializable `dict`. Does not print or write anything.
Raises `ValueError` only for an out-of-range interval (a caller bug).
Collection problems are reported inside the result.

### CPU sampling

CPU utilization needs elapsed time: `psutil.cpu_percent(interval=s)` compares
CPU time counters before and after sleeping for `s` seconds and returns the
average over that window. It is **not an instantaneous reading**, and the call
blocks for the interval. Default `0.5` s (`DEFAULT_CPU_SAMPLE_INTERVAL_S`),
bounded to 0.1–5.0 s; 0 is rejected because it would return a meaningless value.

### Consistency

`observed_at` is taken once at the start of the scan. Groups are read one
after another, so the scan is **not an atomic reading** of the system.

## Output contract (schema_version 1)

```text
{
  "schema_version": 1,
  "observed_at": "2026-10-18T15:49:07Z",         # UTC, ISO-8601, 'Z' suffix
  "collection_status": "complete|partial|failed",
  "system":  { os_name, os_release, os_version, machine, logical_cpu_count },
  "cpu":     { utilization_percent, logical_cpu_count },
  "memory":  { total_bytes, available_bytes, used_percent },
  "disk":    { "status": ..., ["reason": ...,] "drives": [ drive, ... ] },
  "network": { "status": ..., ["reason": ...,] "interfaces": [ interface, ... ] },
  "errors":  [ { "component", "error_type", "message" }, ... ]
}
```

### Measurement ("leaf") representation

Every individual value is `{"value", "unit", "status"}` plus `"reason"` when
status is not `ok`:

| status          | value | meaning |
|-----------------|-------|---------|
| `ok`            | the measurement | measured successfully (0 is a valid value) |
| `unavailable`   | `null` | API returned nothing / not reported (e.g. unknown link speed) |
| `error`         | `null` | the read failed; details in `errors` |
| `not_collected` | `null` | intentionally skipped by policy |

Units: `bytes` (integers), `percent` (float), `Mbps` (integer), `count`
(integer); `null` for text and booleans. An unavailable or failed reading is
**never** represented as zero.

### Drive entry

```text
{ "mountpoint": "C:\\", "fstype": "NTFS", "status": "ok|error|unavailable|not_collected",
  ["reason": "..."], "total_bytes": leaf, "used_bytes": leaf, "free_bytes": leaf, "used_percent": leaf }
```

### Interface entry

```text
{ "name": "Wi-Fi", "status": "ok|unavailable", "is_up": leaf(bool), "link_speed_mbps": leaf }
```

### Status rules

- **disk.status / network.status** (group roll-up of item statuses): no items →
  `unavailable`; all `ok` → `ok`; some `ok` → `partial`; none ok and any
  `error` → `error`; otherwise `unavailable`. Failure to enumerate at all →
  `error`. Drives skipped by policy (`not_collected`) are excluded from the
  roll-up. An interface with an unknown link speed is still `ok`.
- **collection_status**
  - `failed`: no `ok` measurement in cpu, memory, disk or network. OS metadata
    alone does not count as meaningful data.
  - `complete`: every system, cpu and memory measurement is `ok`, and both
    `disk.status` and `network.status` are `ok`.
  - `partial`: anything else.

## Snapshot validator (`validator.py`)

The validator is the trust boundary between the collector and anything that
will consume a snapshot later. It checks that a snapshot conforms to the schema
version 1 contract above. It is a pure function over an in-memory object: it
calls no system API, touches no file, network or database, imports neither the
collector nor `psutil`, never mutates its input, and never coerces, clamps,
recomputes or "repairs" a value.

```python
from validator import validate_snapshot

result = validate_snapshot(snapshot)   # never raises for malformed data
result.valid           # True if and only if there are no issues
result.schema_version  # 1 if the snapshot declared version 1, otherwise None
result.issues          # list of ValidationIssue(code, path, message, severity="error")
```

`schema_version` on the result is the version the validator *supports*, never
an echo of the untrusted value in the snapshot. `path` locates the bad value
(`$` is the root, e.g. `disk.drives[1].free_bytes.value`). Issues come out in
schema order, so equivalent inputs give equal results.

### What invalid data looks like

A CPU reading of 140 % and a byte count stored as a string:

```text
OUT_OF_RANGE  cpu.utilization_percent.value   must be between 0 and 100 inclusive
INVALID_TYPE  memory.total_bytes.value        must be an integer byte count
```

| Code | Meaning |
|------|---------|
| `INVALID_ROOT_TYPE` | the snapshot is not a dictionary |
| `MISSING_FIELD` / `UNEXPECTED_FIELD` | required field absent / field not in schema 1 |
| `INVALID_TYPE` / `INVALID_VALUE` | wrong Python type (exact; `bool` is not `int`) / right type, unusable value (for example an empty string) |
| `UNSUPPORTED_SCHEMA_VERSION` | integer version other than 1 |
| `INVALID_TIMESTAMP` | `observed_at` is not `YYYY-MM-DDTHH:MM:SSZ` or not a real date and time |
| `INVALID_STATUS` / `INVALID_COLLECTION_STATUS` | status outside the allowed set for that position / not `complete`, `partial` or `failed` |
| `INVALID_UNIT` | unit differs from the contract (`bytes`, `percent`, `count`, `Mbps`, or `null`) |
| `NULL_VALUE_FOR_OK_STATUS` / `VALUE_FOR_NON_OK_STATUS` | `ok` with a null value / `unavailable`, `error` or `not_collected` with a value |
| `NON_FINITE_NUMBER` | NaN or infinity where a finite percentage is required |
| `OUT_OF_RANGE` | outside the allowed range |
| `INCONSISTENT_VALUES` | fields contradict each other (see below) |

### Rules and policies

- **Closed schema.** Unknown fields are rejected, at every level, and are never
  interpreted as measurements. A new field needs a new schema version.
- **Version first.** A missing, non-integer (including `True`) or unsupported
  `schema_version` yields exactly that one issue: schema 1 rules cannot be
  applied to an unknown contract. Otherwise all independent problems are
  reported in one run.
- **Exact types, no coercion.** Byte counts, CPU counts and Mbps must be `int`,
  percentages must be `float`, `is_up` must be `bool`. Numeric strings, `bool`
  as a number, and NaN/infinity are rejected, never converted.
- **Ranges.** Percent 0-100 inclusive; bytes at least 0; logical CPU count at
  least 1; link speed at least 1 Mbps (the collector reports an unknown speed
  as `unavailable`, never 0).
- **Status and value.** `ok` needs a non-null value and no `reason`; every
  other status needs a null value and a non-empty `reason` (the drive-level
  `reason` is optional for `unavailable`, as the collector omits it there).
- **Cross-field checks.** `available_bytes` at most `total_bytes`; drive
  `used_bytes` and `free_bytes` each at most `total_bytes`; drive and group
  status must match their items under the same roll-up as the collector;
  interface `status` must match whether `is_up` was measured; interface names
  are unique; any `error` status requires at least one entry in `errors`;
  `collection_status` must equal what the status rules above give.
  These checks run only when the structure is otherwise clean, because they
  read the values they compare.
- **Deliberately not checked**, because the collector and source APIs do not
  guarantee it: `used + free == total`, that percentages match the byte counts
  (psutil's own figures are kept as reported), that the two CPU counts agree,
  how many `errors` entries a failure produces, error-message length, how
  recent `observed_at` is, or any plausibility cap (a CPU count of 10^21 is
  well-formed, so it passes).

### Invalid data is not an unhealthy computer

A snapshot that fails validation is a *data* problem: the collector, a transport
step or a later edit produced something that breaks the contract, and the
snapshot should not be trusted for analysis. A snapshot that passes is
well-formed and internally consistent; it says nothing about whether the
readings are true, and nothing about whether the computer is healthy. A
perfectly valid snapshot can show a full disk or a failed CPU reading. Reporting
that is the job of later stages, not the validator.

### Running the validator tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_validator.py                      # hermetic: no psutil needed
.\.venv\Scripts\python.exe -m pytest tests\test_validator_collector_contract.py   # collector output passes the validator
.\.venv\Scripts\python.exe -m pytest                                              # everything
```

`test_validator.py` builds synthetic snapshots and imports neither the collector
nor `psutil`. The contract file feeds real `collect_system_snapshot()` output
(with `psutil` faked, plus one run against the host's real `psutil`) through the
validator so the two modules cannot drift apart. The tests prove the validator
behaves as specified on these inputs; they do not prove the validator is
correct for every input, and the static no-I/O checks only read
`validator.py`'s own source.

## Diagnostic engine (`diagnostics.py`)

A deterministic, hardware-aware first diagnostic layer. It validates a snapshot
with the validator above and, only if it is valid, applies four fixed
threshold rules. Like the validator it is a pure function over in-memory data:
no operating-system queries, no file, network, subprocess or AI access, no
clock, no randomness, and the snapshot is never modified. Identical snapshots
and policies give equal reports.

```python
from diagnostics import diagnose_snapshot, DEFAULT_POLICY
import dataclasses

report = diagnose_snapshot(snapshot)                        # default policy
strict = dataclasses.replace(DEFAULT_POLICY, version="strict-1",
                             disk_warning_used_percent=80.0)
report = diagnose_snapshot(snapshot, strict)                # explicit override
```

Malformed snapshots never raise. A `policy` that is not a `DiagnosticPolicy`
raises `TypeError`, and an invalid threshold raises at policy construction
(both are caller bugs).

### Rules

| Rule id | Fires when | Default |
|---------|-----------|---------|
| `memory.available_low.warning` | available memory is at or **below** a share of total | 10 % |
| `memory.available_low.critical` | same, stricter | 5 % |
| `disk.used_high.warning` | a drive's used space is at or **above** a share of its total | 90 % |
| `disk.used_high.critical` | same, stricter | 95 % |

These are **heuristic occupancy / headroom indicators, not proof of
underperformance**. Crossing none of them is not proof of health: an empty
`findings` tuple means only "no rule condition was met by the evidence that
was available". Critical beats warning for the same resource: only the
critical finding is reported and it lists the warning in
`superseded_rule_ids`. Each drive is judged independently. There is
deliberately **no CPU rule**: one CPU utilization sample cannot establish
sustained pressure.

Percentages are derived from integer byte counts and compared with exact
`fractions.Fraction` arithmetic, so a value exactly on a threshold is never
misjudged by floating-point rounding (for example 7 of 100 bytes is exactly
7 %, although `7 / 100 * 100` is `7.000000000000001`), and a decimal threshold
such as `0.3` means 3/10, not its binary approximation. For drives the
collector's `used_percent` is **not** used: psutil rounds it to one decimal and
defines it per platform, and a drive whose `used_percent` is unavailable but
whose byte counts are fine is still evaluated.

### Report contract (`DiagnosticReport`, schema_version 1)

| Field | Meaning |
|-------|---------|
| `schema_version` | report schema version (1) |
| `snapshot_observed_at` | the snapshot's `observed_at`; `None` when rejected (nothing in an invalid snapshot is trusted) |
| `status` | `complete`, `partial` or `rejected` (below) |
| `policy_version`, `policy` | version label and the full thresholds used |
| `profile` | `HardwareProfile`, or `None` when rejected |
| `findings` | tuple of `DiagnosticFinding`: memory first, then drives in snapshot order |
| `skipped_rules` | tuple of `SkippedRule` with reasons |
| `rules_evaluated` | number of rule instances actually tested |
| `validation_issues` | the validator's issues when rejected, otherwise `()` |
| `rules_skipped`, `rules_applicable` | properties: `len(skipped_rules)` and `rules_evaluated + rules_skipped` |

`DiagnosticFinding`: `rule_id`, `severity` (`warning`/`critical`),
`resource_type` (`memory`/`disk`), `resource_id` (`"memory"` or the drive's
mountpoint), `resource_path` (for example `disk.drives[1]`), `message`,
`evidence`, `superseded_rule_ids`. Messages are fixed, hedged text and never
repeat strings from the snapshot. `DiagnosticEvidence`: `metric_path`,
`observed_value`, `unit`, `threshold`, `comparison` (`<=`/`>=`), `inputs`. The
first evidence item is the derived percentage that was compared (with its
threshold); the raw byte counts follow, each with its snapshot path.

All result types are frozen dataclasses holding only tuples, strings, numbers
and `None`.

### Exact semantics of status and counts

A **rule instance** is one rule applied to one resource: 2 memory instances,
plus 2 per reported drive (or 2 resource-level disk instances if no drive is
reported at all).

- **evaluated:** all required measurements were available and the condition was
  tested. A warning that was tested but whose finding was superseded by a
  critical finding still counts as evaluated.
- **skipped:** it could not be tested. Reason codes: `MEASUREMENT_UNAVAILABLE`,
  `MEASUREMENT_ERROR`, `NOT_COLLECTED` (a required measurement had that status;
  the code comes from the first missing input), `ZERO_CAPACITY` (a 0-byte total
  makes a percentage undefined) and `NO_DRIVES_REPORTED` (empty drive list, so
  missing disk evidence can never look like a clean disk). Unavailable data is
  never replaced by zero, and a skip never affects unrelated rules.
- **status `rejected`:** the snapshot failed validation. No rule ran:
  `rules_evaluated == 0`, no findings, no skips, no profile.
- **status `partial`:** the snapshot is valid but at least one instance was
  skipped, so no findings must not be read as an all-clear.
- **status `complete`:** valid and nothing skipped.

Status describes **rule coverage only**. It is independent of the snapshot's own
`collection_status` (a failed CPU reading makes the snapshot `partial` but skips
no rule) and of `profile.completeness`.

### Hardware profile (`HardwareProfile`)

Derived only from the validated snapshot: `logical_cpu_count` (from
`cpu.logical_cpu_count`, falling back to `system.logical_cpu_count`;
`logical_cpu_count_source` names which), `total_memory_bytes`, and per drive a
`DriveProfile(path, mountpoint, total_bytes, free_bytes)`. Unknown values stay
`None` (never 0; a genuine 0-byte volume is `0`). `completeness` is `complete`
or `partial`, and `missing` lists the snapshot path of every unknown item
(including `disk.drives` when no drive was reported). The profile supplies the
capacity side of each percentage; it describes capacity and never decides by
itself whether the machine is healthy.

### Policy (`DiagnosticPolicy`)

Frozen, explicit and versioned: `version` (default `"1"`) plus
`memory_warning_available_percent` (10), `memory_critical_available_percent`
(5), `disk_warning_used_percent` (90), `disk_critical_used_percent` (95).
Thresholds must be finite numbers from 0 to 100 (not `bool`), and each critical
threshold must be at least as severe as its warning. Nothing is learned or
tuned at run time. The report embeds the whole policy, so an override that
reuses the default label cannot hide changed thresholds.

### Assumptions and limitations

- **Skipped volumes make a report partial.** A drive the collector deliberately
  did not measure (optical drive, network share) is a skipped rule, so machines
  with such volumes are always `partial`. This favors honesty over a tidy
  `complete`.
- **Thresholds are generic heuristics.** A 90 %-full drive or 10 % free memory
  is not shown to hurt performance, and "available memory" is Windows' own
  estimate. Disk figures are logical capacity, not drive health, SMART state,
  SSD endurance or temperature.
- **Read-only or image-backed volumes look "full".** A volume that is
  100 % used by design (a mounted read-only image, for instance) raises a
  critical finding. The engine does not know the volume is read-only: the
  snapshot carries no such field. Observed on a real run (Linux sandbox): three
  read-only image mounts at exactly 100 %.
- **Single snapshot only.** There is no trend, no history and no sustained-
  pressure detection; the scan itself is not atomic.
- **Not validated on Windows 11.** The tests ran on Linux with Windows-style
  fixtures and faked `psutil` values; the one real-`psutil` run asserts
  structure only.
- **Resource ids are snapshot data.** A drive's mountpoint is carried as data in
  `resource_id` and is not sanitized beyond what the validator requires
  (non-empty string); a presenter must escape it when displaying.

### Running the diagnostics tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_diagnostics.py   # the engine
.\.venv\Scripts\python.exe -m pytest                              # everything
```

`test_diagnostics.py` builds collector-shaped snapshots with the validator test
helpers (no `psutil` needed); its last section feeds real collector output
(faked `psutil`, plus one real run) through the engine and is skipped if
`psutil` is not installed. The tests do not prove that the thresholds are
meaningful, and the static and runtime no-side-effect checks cover only
`diagnostics.py`'s own source and the call paths they patch.

## Snapshot repository (`storage.py`)

A small local SQLite store for **validated** snapshots (stdlib `sqlite3` and `json`
only). It is the first component that writes to disk, and it writes only to the
database path you give it. It does not collect, diagnose, analyse trends, sync,
prune or delete anything, and it is not wired into `demo.py`.

```python
from storage import SnapshotRepository
repo = SnapshotRepository(r"C:\Users\me\health\snapshots.sqlite3")  # no disk access yet
repo.initialize()                      # explicit and idempotent; creates the file
record_id = repo.save_snapshot(snapshot)   # validates first; returns the new id
records = repo.get_snapshots(limit=100)    # tuple of StoredSnapshot
```

### Contract

- **Explicit setup.** Importing or constructing does nothing. `save_snapshot()` and
  `get_snapshots()` never create or initialize a database: before `initialize()` they
  raise `RepositoryNotInitializedError`. `initialize()` does not create missing
  directories, refuses an incompatible `snapshots` table or a foreign
  `PRAGMA user_version`, and never alters existing rows.
- **Schema.** One table `snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at TEXT,
  schema_version INTEGER, payload_json TEXT, stored_at TEXT)`, all `NOT NULL` with
  `typeof()` checks, an index on `(observed_at, id)`, and `user_version = 1`.
  `payload_json` is the whole snapshot as deterministic strict JSON (sorted keys,
  compact, ASCII-escaped, no NaN/Infinity). No findings or other derived data are stored.
  `stored_at` is the repository's UTC time (`YYYY-MM-DDTHH:MM:SSZ`), distinct from
  the snapshot's `observed_at`.
- **Saving.** The snapshot is validated with `validator.validate_snapshot` first; an
  invalid one raises `InvalidSnapshotError` (with `.issues`), nothing is repaired,
  nothing is inserted, and the input is never modified. The insert runs in one
  `BEGIN IMMEDIATE` transaction that is committed or rolled back. The same snapshot saved
  twice creates two records (no de-duplication).
- **Retrieving.** `get_snapshots(limit=100)` returns the **most recent** `limit` records
  (by `observed_at`, then `id`) **ordered oldest first**; ties on `observed_at` are
  ordered by insertion `id`. `limit` must be a real `int` (not bool/float) from 1 to
  `MAX_LIMIT` (1000), otherwise `InvalidQueryError` (a `ValueError`). There is no
  pagination, so older records are not reachable through this API. Reads use a
  `query_only` connection and cannot modify the database. Records are frozen
  `StoredSnapshot(id, observed_at, schema_version, stored_at, snapshot)`; every call
  returns a fresh private copy of the payload dict.
- **Corruption.** Each returned row is checked (strict JSON, an object, `observed_at` and
  `schema_version` agreeing with their columns). A bad row raises `CorruptRecordError`
  (with `.record_id`; the message never echoes stored content) instead of being skipped.
  Rows are *not* re-validated on read, so history stays readable if the validator
  later changes.
- **Errors.** All derive from `StorageError`: `InvalidSnapshotError`,
  `RepositoryNotInitializedError`, `StorageDatabaseError` (SQLite failures such as
  locked, unreadable, not a database, incompatible schema; chained to the original),
  `CorruptRecordError`, `InvalidQueryError`. Programming errors (wrong argument
  types to the constructor, a bad clock, `sqlite3.ProgrammingError`, and so on) are
  not caught.

### Privacy

The database holds hostnames, drive letters and other machine details. Treat it as
**sensitive local data**: it never leaves the machine through this code, and it must
not be committed or shared. The project `.gitignore` is expected to exclude common
SQLite extensions (`*.sqlite`, `*.sqlite3`, `*.db`, and journal files); this code does
not change it. Verify with `git check-ignore -v <your-database-file>` before
committing, and keep the database outside the repository if possible.

### Limitations

- Per-operation connections; no connection pooling and no long-lived handles.
- Concurrency: SQLite locking only. A busy database waits `busy_timeout_s` (default 5 s)
  and then raises `StorageDatabaseError`; this is not tested under heavy contention.
- Records stay in the database indefinitely (no pruning or deletion API).
- A valid snapshot is stored as reported; storage does not make readings true.
- The database file is not encrypted. Atomicity relies on SQLite's guarantees.
- Not tested on Windows by the author of this change (see below).

### Running the storage tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_storage.py   # storage only
.\.venv\Scripts\python.exe -m pytest                          # everything
```

Each test uses its own `tmp_path` database. Only the section on collector-produced
snapshots needs `psutil` (skipped without it).

## Notes and limitations

- **Partition policy:** `psutil.disk_partitions(all=False)`. Volumes flagged
  `cdrom`, `remote` (network share) or `unmounted`, or with no filesystem, are
  not queried and are reported `not_collected`, to avoid blocking calls. Other
  failures (permissions, device not ready) are caught per drive. A volume that
  looks normal but hangs can still block `disk_usage`; there is no timeout.
- **Logical capacity only.** Disk figures say nothing about physical-device
  health, SMART, SSD endurance or temperature.
- **Link up ≠ internet.** No network probes are made. Reported speed may be
  missing (psutil returns 0 when unknown, mapped to `unavailable`) and, for
  virtual adapters, may not reflect real throughput.
- **Memory:** `available_bytes` is memory usable without swapping, not "free".
  `used_percent` is psutil's own figure, `(total - available) / total`.
- **OS version:** `platform.release()` can report `"10"` on some Windows 11
  builds; `os_version` carries the build (`10.0.<build>`). The collector does
  not interpret it.
- **Error messages** are single-line, truncated to 200 characters, with no
  stack traces. Unexpected (non-OS) exceptions are recorded with the prefix
  `Unexpected internal error:` and turn the affected group into `error`.

## Layout

```text
collector.py            collect_system_snapshot() and helpers (no I/O, no printing)
demo.py                 presentation only: prints the snapshot as JSON to stdout
validator.py            validate_snapshot(): pure, strict contract validator
storage.py              SnapshotRepository: local SQLite store for validated snapshots
diagnostics.py          diagnose_snapshot(): pure, deterministic rules + hardware profile
tests/test_collector.py pytest suite (mocked psutil/platform)
tests/test_diagnostics.py
                        engine tests (collector-shaped fixtures; real collector output)
tests/test_storage.py   repository tests (temp databases, injected failures)
tests/test_validator.py hermetic validator tests (synthetic snapshots, no psutil)
tests/test_validator_collector_contract.py
                        real collector output must pass the validator
requirements.txt        runtime: psutil
requirements-dev.txt    runtime + pytest
pytest.ini              puts the project root on the import path
```
