"""Strictly read-only, one-shot system snapshot collector for Windows 11.

This module is a data-acquisition component only. It reports measurements and
collection failures; it never interprets them as problems and never suggests
fixes. See README.md for the full output contract.

Read-only boundary
------------------
The only external APIs used are a small allowlist of ``psutil`` observation
functions and a few standard-library calls (``platform.*``, ``datetime.now``).
There is no subprocess, shell, file, registry, network or environment access.

Timing and consistency
----------------------
* ``observed_at`` is taken once, at the start of the scan.
* CPU utilization is an average over a bounded sampling interval (it needs
  elapsed time, so it is not instantaneous) and the call blocks for that long.
* The scan is NOT an atomic reading of the whole system: groups are read
  one after another, so values may reflect slightly different moments.
"""

from __future__ import annotations

from datetime import datetime, timezone
import platform
from typing import Any, Callable

import psutil

SCHEMA_VERSION = 1

# CPU sampling interval in seconds. psutil.cpu_percent(interval=s) compares CPU
# times before and after sleeping for ``s``; interval=0/None would instead
# return a meaningless value (compared with an earlier call), so it is not
# allowed. The bounds keep a scan short and the sample not-too-noisy.
DEFAULT_CPU_SAMPLE_INTERVAL_S = 0.5
MIN_CPU_SAMPLE_INTERVAL_S = 0.1
MAX_CPU_SAMPLE_INTERVAL_S = 5.0

# Failures we expect from OS/permission/device access. Anything else (TypeError,
# ValueError, AttributeError, ...) is treated as a possible programming error:
# it is never swallowed per-measurement and is recorded by the outer boundary.
_EXPECTED_ERRORS = (OSError, psutil.Error, NotImplementedError)

_MAX_MESSAGE_CHARS = 200

# Partition-selection policy: volumes carrying these psutil option flags are
# not touched at all, because querying them can block (disconnected shares,
# empty optical drives). They are reported as "not_collected".
_SKIPPED_MOUNT_FLAGS = (
    ("cdrom", "optical drive"),
    ("remote", "network share"),
    ("unmounted", "volume not mounted"),
)

_SYSTEM_FIELDS = (
    ("os_name", None),
    ("os_release", None),
    ("os_version", None),
    ("machine", None),
    ("logical_cpu_count", "count"),
)
_CPU_FIELDS = (("utilization_percent", "percent"), ("logical_cpu_count", "count"))
_MEMORY_FIELDS = (
    ("total_bytes", "bytes"),
    ("available_bytes", "bytes"),
    ("used_percent", "percent"),
)
_DRIVE_FIELDS = (
    ("total_bytes", "bytes"),
    ("used_bytes", "bytes"),
    ("free_bytes", "bytes"),
    ("used_percent", "percent"),
)


# --------------------------------------------------------------------------
# Small helpers for measurements and errors
# --------------------------------------------------------------------------

def _leaf(value: Any, unit: str | None, status: str, reason: str | None = None) -> dict[str, Any]:
    """One measurement. ``reason`` is present only when status is not "ok"."""
    leaf: dict[str, Any] = {"value": value, "unit": unit, "status": status}
    if reason is not None:
        leaf["reason"] = reason
    return leaf


def _blank_leaves(fields, status: str, reason: str) -> dict[str, dict[str, Any]]:
    return {name: _leaf(None, unit, status, reason) for name, unit in fields}


def _from_raw(raw: Any, unit: str | None, cast: Callable[[Any], Any] | None = None,
              missing_reason: str = "API returned no value") -> dict[str, Any]:
    """Map a raw API value to a leaf. None/"" means unavailable; 0 is a real value."""
    if raw is None or (isinstance(raw, str) and raw == ""):
        return _leaf(None, unit, "unavailable", missing_reason)
    return _leaf(cast(raw) if cast else raw, unit, "ok")


def _record_error(errors: list, component: str, exc: BaseException, prefix: str = "") -> str:
    """Append a structured, concise, single-line error; return its message."""
    text = " ".join(str(exc).split())[:_MAX_MESSAGE_CHARS] or "no details"
    message = prefix + text
    errors.append({"component": component, "error_type": type(exc).__name__, "message": message})
    return message


def _measure(errors: list, component: str, unit: str | None, read: Callable[[], Any],
             cast: Callable[[Any], Any] | None = None) -> dict[str, Any]:
    """Run one read-only API call. Expected OS errors become an "error" leaf.

    The cast runs outside the try block on purpose: a bad value from the API is
    a programming/assumption error and must reach the outer boundary, not be
    hidden as a routine device error.
    """
    try:
        raw = read()
    except _EXPECTED_ERRORS as exc:
        return _leaf(None, unit, "error", _record_error(errors, component, exc))
    return _from_raw(raw, unit, cast)


def _logical_cpu_count() -> int | None:
    return psutil.cpu_count(logical=True)


def _roll_up(statuses: list[str]) -> tuple[str, str | None]:
    """Group status from item statuses (ok / error / unavailable)."""
    if not statuses:
        return "unavailable", "no items reported"
    if all(s == "ok" for s in statuses):
        return "ok", None
    if any(s == "ok" for s in statuses):
        return "partial", "some items could not be measured"
    if "error" in statuses:
        return "error", "no item could be measured"
    return "unavailable", "no item could be measured"


# --------------------------------------------------------------------------
# Collection groups. Each takes the shared ``errors`` list and returns a group.
# --------------------------------------------------------------------------

def _collect_system(errors: list) -> dict[str, Any]:
    return {
        "os_name": _measure(errors, "system.os_name", None, platform.system),
        "os_release": _measure(errors, "system.os_release", None, platform.release),
        "os_version": _measure(errors, "system.os_version", None, platform.version),
        "machine": _measure(errors, "system.machine", None, platform.machine),
        "logical_cpu_count": _measure(
            errors, "system.logical_cpu_count", "count", _logical_cpu_count, int),
    }


def _collect_cpu(errors: list, interval: float) -> dict[str, Any]:
    return {
        "utilization_percent": _measure(
            errors, "cpu.utilization_percent", "percent",
            lambda: psutil.cpu_percent(interval=interval), float),
        "logical_cpu_count": _measure(
            errors, "cpu.logical_cpu_count", "count", _logical_cpu_count, int),
    }


def _collect_memory(errors: list) -> dict[str, Any]:
    try:
        vm = psutil.virtual_memory()
    except _EXPECTED_ERRORS as exc:
        return _blank_leaves(_MEMORY_FIELDS, "error", _record_error(errors, "memory", exc))
    # "available" (memory usable without swapping), never "free".
    return {
        "total_bytes": _from_raw(vm.total, "bytes", int),
        "available_bytes": _from_raw(vm.available, "bytes", int),
        "used_percent": _from_raw(vm.percent, "percent", float),
    }


def _skip_reason(part: Any) -> str | None:
    """Return why a partition must not be queried, or None if it may be."""
    if not getattr(part, "fstype", ""):
        return "no filesystem reported (for example, no media present)"
    flags = {f.strip() for f in (getattr(part, "opts", "") or "").lower().split(",")}
    for flag, label in _SKIPPED_MOUNT_FLAGS:
        if flag in flags:
            return f"{label} skipped to avoid a potentially blocking call"
    return None


def _drive_entry(part: Any, status: str, leaves: dict, reason: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "mountpoint": part.mountpoint,
        "fstype": getattr(part, "fstype", "") or None,
        "status": status,
    }
    if reason is not None:
        entry["reason"] = reason
    entry.update(leaves)
    return entry


def _collect_drive(errors: list, part: Any) -> dict[str, Any]:
    skip = _skip_reason(part)
    if skip is not None:
        return _drive_entry(part, "not_collected",
                            _blank_leaves(_DRIVE_FIELDS, "not_collected", skip), skip)
    try:
        usage = psutil.disk_usage(part.mountpoint)
    except _EXPECTED_ERRORS as exc:
        reason = _record_error(errors, f"disk.drive[{part.mountpoint}]", exc)
        return _drive_entry(part, "error", _blank_leaves(_DRIVE_FIELDS, "error", reason), reason)
    leaves = {
        "total_bytes": _from_raw(usage.total, "bytes", int),
        "used_bytes": _from_raw(usage.used, "bytes", int),
        "free_bytes": _from_raw(usage.free, "bytes", int),
        "used_percent": _from_raw(usage.percent, "percent", float),
    }
    ok = all(leaf["status"] == "ok" for leaf in leaves.values())
    return _drive_entry(part, "ok" if ok else "unavailable", leaves)


def _collect_disk(errors: list) -> dict[str, Any]:
    try:
        partitions = psutil.disk_partitions(all=False)
    except _EXPECTED_ERRORS as exc:
        return {"status": "error", "reason": _record_error(errors, "disk.partitions", exc),
                "drives": []}
    drives = [_collect_drive(errors, part) for part in partitions]
    # Volumes skipped by policy do not degrade the group status.
    status, reason = _roll_up([d["status"] for d in drives if d["status"] != "not_collected"])
    group: dict[str, Any] = {"status": status}
    if reason is not None:
        group["reason"] = reason
    group["drives"] = drives
    return group


def _interface_entry(name: str, stats: Any) -> dict[str, Any]:
    is_up = _from_raw(getattr(stats, "isup", None), None, bool, "interface state not reported")
    speed = getattr(stats, "speed", None)
    if speed is None or speed <= 0:
        # psutil reports 0 when the speed is unknown; that is not 0 Mbps.
        link = _leaf(None, "Mbps", "unavailable",
                     "link speed not reported (psutil reports 0 when unknown)")
    else:
        link = _leaf(int(speed), "Mbps", "ok")
    return {
        "name": name,
        "status": "ok" if is_up["status"] == "ok" else "unavailable",
        "is_up": is_up,
        "link_speed_mbps": link,
    }


def _collect_network(errors: list) -> dict[str, Any]:
    try:
        stats = psutil.net_if_stats()
    except _EXPECTED_ERRORS as exc:
        return {"status": "error", "reason": _record_error(errors, "network.interfaces", exc),
                "interfaces": []}
    interfaces = [_interface_entry(name, stats[name]) for name in sorted(stats)]
    # An unknown link speed alone does not degrade status (loopback and
    # disconnected adapters routinely report none).
    status, reason = _roll_up([i["status"] for i in interfaces])
    if not interfaces:
        reason = "no network interfaces reported"
    group: dict[str, Any] = {"status": status}
    if reason is not None:
        group["reason"] = reason
    group["interfaces"] = interfaces
    return group


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def _guarded(errors: list, component: str, collect: Callable[[list], Any],
             fallback: Callable[[str], Any]) -> Any:
    """Outer boundary for one group.

    Anything unexpected is recorded (never silently dropped) and replaced by a
    schema-valid "error" group, so other groups' results survive.
    """
    try:
        return collect(errors)
    except Exception as exc:  # noqa: BLE001 - deliberate boundary: record, keep contract valid
        reason = _record_error(errors, component, exc, prefix="Unexpected internal error: ")
        return fallback(reason)


def _overall_status(snapshot: dict[str, Any]) -> str:
    """complete / partial / failed.

    failed:   no "ok" measurement at all in cpu, memory, disk or network
              (system metadata alone is not meaningful diagnostic data).
    complete: every system/cpu/memory measurement is "ok" AND disk.status and
              network.status are both "ok".
    partial:  anything in between.
    """
    resource = list(snapshot["cpu"].values()) + list(snapshot["memory"].values())
    for item in snapshot["disk"]["drives"] + snapshot["network"]["interfaces"]:
        resource.extend(v for v in item.values() if isinstance(v, dict))
    if not any(leaf["status"] == "ok" for leaf in resource):
        return "failed"

    required = (list(snapshot["system"].values()) + list(snapshot["cpu"].values())
                + list(snapshot["memory"].values()))
    healthy = (all(leaf["status"] == "ok" for leaf in required)
               and snapshot["disk"]["status"] == "ok"
               and snapshot["network"]["status"] == "ok")
    return "complete" if healthy else "partial"


def _validate_interval(value: Any) -> None:
    valid = (isinstance(value, (int, float)) and not isinstance(value, bool)
             and MIN_CPU_SAMPLE_INTERVAL_S <= value <= MAX_CPU_SAMPLE_INTERVAL_S)
    if not valid:  # also rejects NaN
        raise ValueError(
            f"cpu_sample_interval must be a number between {MIN_CPU_SAMPLE_INTERVAL_S} "
            f"and {MAX_CPU_SAMPLE_INTERVAL_S} seconds")


def collect_system_snapshot(cpu_sample_interval: float = DEFAULT_CPU_SAMPLE_INTERVAL_S) -> dict[str, Any]:
    """Collect one read-only snapshot and return it as a JSON-serializable dict.

    Blocks for about ``cpu_sample_interval`` seconds (CPU sampling). Does not
    print, write files, or keep state between calls. Raises ValueError only for
    an out-of-range interval (a caller bug); collection problems are reported
    inside the returned dict instead.
    """
    _validate_interval(cpu_sample_interval)

    errors: list[dict[str, str]] = []
    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    system = _guarded(errors, "system", _collect_system,
                      lambda r: _blank_leaves(_SYSTEM_FIELDS, "error", r))
    cpu = _guarded(errors, "cpu", lambda e: _collect_cpu(e, cpu_sample_interval),
                   lambda r: _blank_leaves(_CPU_FIELDS, "error", r))
    memory = _guarded(errors, "memory", _collect_memory,
                      lambda r: _blank_leaves(_MEMORY_FIELDS, "error", r))
    disk = _guarded(errors, "disk", _collect_disk,
                    lambda r: {"status": "error", "reason": r, "drives": []})
    network = _guarded(errors, "network", _collect_network,
                       lambda r: {"status": "error", "reason": r, "interfaces": []})

    snapshot: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "observed_at": observed_at,
        "collection_status": None,
        "system": system,
        "cpu": cpu,
        "memory": memory,
        "disk": disk,
        "network": network,
        "errors": errors,
    }
    snapshot["collection_status"] = _overall_status(snapshot)
    return snapshot
