# system-health-agent — Milestone 1: read-only system snapshot collector

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
tests/test_collector.py pytest suite (mocked psutil/platform)
requirements.txt        runtime: psutil
requirements-dev.txt    runtime + pytest
pytest.ini              puts the project root on the import path
```
