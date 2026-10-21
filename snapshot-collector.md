# system-health-agent — : read-only system snapshot collector

A one-shot, strictly read-only collector for **Windows 11 x64** (Python 3.12+,
ordinary user privileges). It reports what it measured and what it could not
measure. It does **not** diagnose, interpret, repair, or suggest anything.

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

**Not in this milestone:** database, LLM, GUI, scheduler/service, battery,
thermals, process monitoring, diagnostic rules, repairs.

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
tests/test_collector.py pytest suite (mocked psutil/platform)
tests/test_validator.py hermetic validator tests (synthetic snapshots, no psutil)
tests/test_validator_collector_contract.py
                        real collector output must pass the validator
requirements.txt        runtime: psutil
requirements-dev.txt    runtime + pytest
pytest.ini              puts the project root on the import path
```
