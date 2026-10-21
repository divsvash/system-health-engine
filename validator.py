"""Strict, pure validator for system snapshots (schema version 1).

``validate_snapshot(snapshot)`` checks that a snapshot produced by
``collector.collect_system_snapshot`` conforms to its documented contract. It is
the trust boundary between collection and any later analysis.

Design rules
------------
* Pure: it only reads the object it is given. No system APIs, no file, network
  or database access, no imports of the collector or psutil. It never mutates
  its input, never coerces, clamps, recomputes or repairs a value, and never
  evaluates anything found in the data.
* Deterministic: issues are produced in a fixed traversal order (the order of
  the schema, not of the input's dict keys), so equivalent inputs give equal
  results.
* Ordinary malformed input (wrong types, missing fields, bad values) becomes
  structured issues, not exceptions. There is deliberately no catch-all
  ``except``: an exception here would be a programming error and should be loud.
* Messages and paths never echo untrusted values (field names are the only
  untrusted text that can appear, and they are length-limited and sanitised).

Policies for schema version 1
-----------------------------
* Unknown fields are rejected (``UNEXPECTED_FIELD``). Schema 1 is a closed
  schema; adding a field requires a new schema version. Unknown fields are never
  interpreted as measurements.
* A missing, non-integer or unsupported ``schema_version`` stops validation
  after reporting that one issue: the v1 rules cannot be applied to an unknown
  contract, so further errors would be misleading.
* Otherwise all independent problems are reported in one run. Cross-field checks
  and the ``collection_status`` / ``errors`` consistency checks run only when
  everything else is structurally valid, because they read the data they compare.
* ``observed_at`` must be ``YYYY-MM-DDTHH:MM:SSZ`` (UTC, the collector's format).
  It is not compared with the current time (that would make validation
  non-deterministic).
* Percent values must be ``float`` (the collector always produces floats; an
  ``int`` is a contract violation, not something to coerce).

A snapshot that validates is well-formed and internally consistent. That says
nothing about whether the readings are true, or whether the computer is healthy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import math
import re
from typing import Any, Callable

SUPPORTED_SCHEMA_VERSION = 1
SEVERITY_ERROR = "error"

# Stable, machine-readable issue codes.
INVALID_ROOT_TYPE = "INVALID_ROOT_TYPE"
MISSING_FIELD = "MISSING_FIELD"
UNEXPECTED_FIELD = "UNEXPECTED_FIELD"
INVALID_TYPE = "INVALID_TYPE"
INVALID_VALUE = "INVALID_VALUE"
UNSUPPORTED_SCHEMA_VERSION = "UNSUPPORTED_SCHEMA_VERSION"
INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
INVALID_STATUS = "INVALID_STATUS"
INVALID_UNIT = "INVALID_UNIT"
NULL_VALUE_FOR_OK_STATUS = "NULL_VALUE_FOR_OK_STATUS"
VALUE_FOR_NON_OK_STATUS = "VALUE_FOR_NON_OK_STATUS"
NON_FINITE_NUMBER = "NON_FINITE_NUMBER"
OUT_OF_RANGE = "OUT_OF_RANGE"
INCONSISTENT_VALUES = "INCONSISTENT_VALUES"
INVALID_COLLECTION_STATUS = "INVALID_COLLECTION_STATUS"

ISSUE_CODES = frozenset({
    INVALID_ROOT_TYPE, MISSING_FIELD, UNEXPECTED_FIELD, INVALID_TYPE, INVALID_VALUE,
    UNSUPPORTED_SCHEMA_VERSION, INVALID_TIMESTAMP, INVALID_STATUS, INVALID_UNIT,
    NULL_VALUE_FOR_OK_STATUS, VALUE_FOR_NON_OK_STATUS, NON_FINITE_NUMBER, OUT_OF_RANGE,
    INCONSISTENT_VALUES, INVALID_COLLECTION_STATUS,
})


@dataclass(frozen=True)
class ValidationIssue:
    """One contract violation.

    ``path`` locates the offending value, e.g. ``cpu.utilization_percent.value``
    or ``disk.drives[1].free_bytes.status`` (``$`` is the root object).
    ``severity`` is always ``"error"`` in this version.
    """

    code: str
    path: str
    message: str
    severity: str = SEVERITY_ERROR


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of validating one snapshot.

    ``schema_version`` is the supported version (1) when the snapshot declared
    it correctly, otherwise ``None``; an untrusted value is never echoed back.
    ``valid`` is derived from ``issues`` so the two cannot disagree: it is True
    if and only if there is no issue with severity ``"error"``.
    """

    schema_version: int | None
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not any(issue.severity == SEVERITY_ERROR for issue in self.issues)


# --------------------------------------------------------------------------
# Contract tables (mirrors collector.py / README "Output contract")
# --------------------------------------------------------------------------

_TOP_FIELDS = ("schema_version", "observed_at", "collection_status", "system", "cpu",
               "memory", "disk", "network", "errors")
_COLLECTION_STATUSES = frozenset({"complete", "partial", "failed"})
_GROUP_STATUSES = frozenset({"ok", "partial", "unavailable", "error"})
_LEAF_STATUSES = frozenset({"ok", "unavailable", "error"})
_DRIVE_STATUSES = frozenset({"ok", "unavailable", "error", "not_collected"})  # also drive leaves
_INTERFACE_STATUSES = frozenset({"ok", "unavailable"})

_SYSTEM_SPEC = {"os_name": "text", "os_release": "text", "os_version": "text",
                "machine": "text", "logical_cpu_count": "count"}
_CPU_SPEC = {"utilization_percent": "percent", "logical_cpu_count": "count"}
_MEMORY_SPEC = {"total_bytes": "bytes", "available_bytes": "bytes", "used_percent": "percent"}
_DRIVE_SPEC = {"total_bytes": "bytes", "used_bytes": "bytes", "free_bytes": "bytes",
               "used_percent": "percent"}

_TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_MAX_KEY_CHARS = 64


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _add(issues: list[ValidationIssue], code: str, path: str, message: str) -> None:
    issues.append(ValidationIssue(code, path, message))


def _join(path: str, name: str) -> str:
    return f"{path}.{name}" if path else name


def _one_of(allowed: frozenset[str]) -> str:
    return "must be one of: " + ", ".join(sorted(allowed))


def _key_text(key: Any) -> str:
    """Printable, length-limited text for an untrusted dict key (used in paths)."""
    if type(key) is not str:
        return "<non-string key>"
    text = "".join(ch if ch.isprintable() else "?" for ch in key[:_MAX_KEY_CHARS])
    return text + "..." if len(key) > _MAX_KEY_CHARS else text


def _check_keys(issues: list[ValidationIssue], path: str, obj: dict,
                required: tuple[str, ...], optional: tuple[str, ...] = ()) -> None:
    """Report missing required fields and any field outside the documented set."""
    for name in required:
        if name not in obj:
            _add(issues, MISSING_FIELD, _join(path, name), "required field is missing")
    known = required + optional
    unexpected = [key for key in obj if key not in known]
    for key in sorted(unexpected, key=lambda k: (type(k).__name__, _key_text(k))):
        _add(issues, UNEXPECTED_FIELD, _join(path, _key_text(key)),
             "field is not part of schema version 1")


def _check_enum(issues: list[ValidationIssue], path: str, obj: dict, key: str,
                allowed: frozenset[str], code: str = INVALID_STATUS) -> str | None:
    """Return the value if it is an allowed string, else record an issue and return None."""
    if key not in obj:
        return None  # already reported as MISSING_FIELD by _check_keys
    value = obj[key]
    if type(value) is str and value in allowed:  # str check first: unhashable values must not reach `in`
        return value
    _add(issues, code, path, _one_of(allowed))
    return None


def _check_text(issues: list[ValidationIssue], path: str, value: Any) -> bool:
    if type(value) is not str:
        _add(issues, INVALID_TYPE, path, "must be a string")
        return False
    if not value:
        _add(issues, INVALID_VALUE, path, "must not be empty")
        return False
    return True


def _check_reason(issues: list[ValidationIssue], path: str, obj: dict, status: str,
                  optional_for: tuple[str, ...] = ()) -> None:
    """``reason`` must be absent for "ok", required for other statuses (unless optional_for)."""
    reason_path = _join(path, "reason")
    if status == "ok":
        if "reason" in obj:
            _add(issues, INCONSISTENT_VALUES, reason_path, 'must be absent when status is "ok"')
    elif "reason" in obj:
        _check_text(issues, reason_path, obj["reason"])
    elif status not in optional_for:
        _add(issues, MISSING_FIELD, reason_path, f'required when status is "{status}"')


# --------------------------------------------------------------------------
# Value checks per measurement kind. Each returns True if the value is valid.
# Exact type tests (type(v) is ...) deliberately reject bool, numeric strings
# and int/float mix-ups. Nothing is coerced.
# --------------------------------------------------------------------------

def _check_value_text(issues: list[ValidationIssue], path: str, value: Any) -> bool:
    return _check_text(issues, path, value)


def _check_value_count(issues: list[ValidationIssue], path: str, value: Any) -> bool:
    if type(value) is not int:
        _add(issues, INVALID_TYPE, path, "must be an integer")
        return False
    if value < 1:
        _add(issues, OUT_OF_RANGE, path, "must be a positive integer")
        return False
    return True


def _check_value_bytes(issues: list[ValidationIssue], path: str, value: Any) -> bool:
    if type(value) is not int:
        _add(issues, INVALID_TYPE, path, "must be an integer byte count")
        return False
    if value < 0:
        _add(issues, OUT_OF_RANGE, path, "must not be negative")
        return False
    return True


def _check_value_percent(issues: list[ValidationIssue], path: str, value: Any) -> bool:
    if type(value) is not float:
        _add(issues, INVALID_TYPE, path, "must be a float")
        return False
    if not math.isfinite(value):
        _add(issues, NON_FINITE_NUMBER, path, "must be finite (not NaN or infinity)")
        return False
    if not 0.0 <= value <= 100.0:
        _add(issues, OUT_OF_RANGE, path, "must be between 0 and 100 inclusive")
        return False
    return True


def _check_value_bool(issues: list[ValidationIssue], path: str, value: Any) -> bool:
    if type(value) is not bool:
        _add(issues, INVALID_TYPE, path, "must be a boolean")
        return False
    return True


def _check_value_mbps(issues: list[ValidationIssue], path: str, value: Any) -> bool:
    if type(value) is not int:
        _add(issues, INVALID_TYPE, path, "must be an integer")
        return False
    if value <= 0:
        # psutil reports 0 for "unknown"; the collector maps that to "unavailable",
        # so a successful measurement is never 0 (and never negative).
        _add(issues, OUT_OF_RANGE, path, 'must be positive; unknown speed is "unavailable", not 0')
        return False
    return True


# kind -> (expected unit, value check)
_KINDS: dict[str, tuple[str | None, Callable[[list[ValidationIssue], str, Any], bool]]] = {
    "text": (None, _check_value_text),
    "count": ("count", _check_value_count),
    "bytes": ("bytes", _check_value_bytes),
    "percent": ("percent", _check_value_percent),
    "bool": (None, _check_value_bool),
    "mbps": ("Mbps", _check_value_mbps),
}


# --------------------------------------------------------------------------
# Measurement ("leaf") validation
# --------------------------------------------------------------------------

def _check_leaf(issues: list[ValidationIssue], path: str, leaf: Any, kind: str,
                allowed_statuses: frozenset[str] = _LEAF_STATUSES) -> tuple[str, Any] | None:
    """Validate one ``{value, unit, status[, reason]}`` measurement.

    Returns ``(status, value)`` when the leaf itself is fully valid, else None.
    """
    if not isinstance(leaf, dict):
        _add(issues, INVALID_TYPE, path, "measurement must be an object")
        return None
    before = len(issues)
    expected_unit, check_value = _KINDS[kind]

    _check_keys(issues, path, leaf, ("value", "unit", "status"), ("reason",))
    status = _check_enum(issues, _join(path, "status"), leaf, "status", allowed_statuses)

    if "unit" in leaf:
        actual = leaf["unit"]
        matches = actual is None if expected_unit is None else (type(actual) is str and actual == expected_unit)
        if not matches:
            expected = "null" if expected_unit is None else f'"{expected_unit}"'
            _add(issues, INVALID_UNIT, _join(path, "unit"), f"must be {expected} for this measurement")

    value = None
    if status is not None:
        if "value" in leaf:
            value = leaf["value"]
            value_path = _join(path, "value")
            if status == "ok":
                if value is None:
                    _add(issues, NULL_VALUE_FOR_OK_STATUS, value_path,
                         'value must not be null when status is "ok"')
                else:
                    check_value(issues, value_path, value)
            elif value is not None:
                _add(issues, VALUE_FOR_NON_OK_STATUS, value_path,
                     f'value must be null when status is "{status}"')
        _check_reason(issues, path, leaf, status)

    if status is None or len(issues) != before:
        return None
    return status, value


def _check_leaf_section(issues: list[ValidationIssue], path: str, section: Any,
                        spec: dict[str, str]) -> dict[str, tuple[str, Any] | None] | None:
    """Validate a flat object of measurements. Returns per-field results, or None if not an object."""
    if not isinstance(section, dict):
        _add(issues, INVALID_TYPE, path, "section must be an object")
        return None
    _check_keys(issues, path, section, tuple(spec))
    return {name: _check_leaf(issues, _join(path, name), section[name], kind)
            for name, kind in spec.items() if name in section}


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------

def _check_timestamp(issues: list[ValidationIssue], snapshot: dict) -> None:
    if "observed_at" not in snapshot:
        return
    value = snapshot["observed_at"]
    if type(value) is not str:
        _add(issues, INVALID_TYPE, "observed_at", "must be a string")
        return
    if _TIMESTAMP_RE.fullmatch(value) is None:
        _add(issues, INVALID_TIMESTAMP, "observed_at",
             "must be a UTC timestamp formatted YYYY-MM-DDTHH:MM:SSZ")
        return
    try:
        datetime.fromisoformat(value)  # the trailing "Z" makes it timezone-aware UTC
    except ValueError:
        _add(issues, INVALID_TIMESTAMP, "observed_at", "is not a real calendar date and time")


def _check_collection_status_field(issues: list[ValidationIssue], snapshot: dict) -> None:
    if "collection_status" not in snapshot:
        return
    value = snapshot["collection_status"]
    if type(value) is not str:
        _add(issues, INVALID_TYPE, "collection_status", "must be a string")
    elif value not in _COLLECTION_STATUSES:
        _add(issues, INVALID_COLLECTION_STATUS, "collection_status", _one_of(_COLLECTION_STATUSES))


def _check_memory(issues: list[ValidationIssue], section: Any) -> None:
    parsed = _check_leaf_section(issues, "memory", section, _MEMORY_SPEC)
    if not parsed:
        return
    total, available = parsed.get("total_bytes"), parsed.get("available_bytes")
    if total and available and total[0] == "ok" and available[0] == "ok" and available[1] > total[1]:
        _add(issues, INCONSISTENT_VALUES, "memory.available_bytes.value",
             "available bytes must not exceed total bytes")


def _check_drive(issues: list[ValidationIssue], path: str, drive: Any) -> str | None:
    """Validate one drive entry; return its status if the status field itself is valid."""
    if not isinstance(drive, dict):
        _add(issues, INVALID_TYPE, path, "drive entry must be an object")
        return None
    before = len(issues)
    _check_keys(issues, path, drive, ("mountpoint", "fstype", "status", *_DRIVE_SPEC), ("reason",))

    if "mountpoint" in drive:
        _check_text(issues, _join(path, "mountpoint"), drive["mountpoint"])
    if drive.get("fstype") is not None:  # null is allowed; absence was reported above
        _check_text(issues, _join(path, "fstype"), drive["fstype"])

    status = _check_enum(issues, _join(path, "status"), drive, "status", _DRIVE_STATUSES)
    leaves = {name: _check_leaf(issues, _join(path, name), drive[name], kind, _DRIVE_STATUSES)
              for name, kind in _DRIVE_SPEC.items() if name in drive}
    if status is not None:
        # The collector omits the drive-level reason for "unavailable" (detail is on the leaves).
        _check_reason(issues, path, drive, status, optional_for=("unavailable",))

    if status is not None and len(issues) == before:
        leaf_statuses = [leaves[name][0] for name in _DRIVE_SPEC]
        if status in ("error", "not_collected"):
            consistent = all(s == status for s in leaf_statuses)
        elif status == "ok":
            consistent = all(s == "ok" for s in leaf_statuses)
        else:  # "unavailable": measurements are ok/unavailable, with at least one unavailable
            consistent = (all(s in ("ok", "unavailable") for s in leaf_statuses)
                          and "unavailable" in leaf_statuses)
        if not consistent:
            _add(issues, INCONSISTENT_VALUES, _join(path, "status"),
                 "drive status contradicts its measurement statuses")

        total = leaves["total_bytes"]
        if total[0] == "ok":
            for name in ("used_bytes", "free_bytes"):
                part = leaves[name]
                if part[0] == "ok" and part[1] > total[1]:
                    _add(issues, INCONSISTENT_VALUES, _join(_join(path, name), "value"),
                         "must not exceed total bytes")
    return status


def _check_interface(issues: list[ValidationIssue], path: str, interface: Any) -> str | None:
    """Validate one interface entry; return its status if the status field itself is valid."""
    if not isinstance(interface, dict):
        _add(issues, INVALID_TYPE, path, "interface entry must be an object")
        return None
    _check_keys(issues, path, interface, ("name", "status", "is_up", "link_speed_mbps"))

    if "name" in interface:
        _check_text(issues, _join(path, "name"), interface["name"])
    status = _check_enum(issues, _join(path, "status"), interface, "status", _INTERFACE_STATUSES)
    is_up = (_check_leaf(issues, _join(path, "is_up"), interface["is_up"], "bool", _INTERFACE_STATUSES)
             if "is_up" in interface else None)
    if "link_speed_mbps" in interface:
        _check_leaf(issues, _join(path, "link_speed_mbps"), interface["link_speed_mbps"], "mbps",
                    _INTERFACE_STATUSES)

    if status is not None and is_up is not None and (status == "ok") != (is_up[0] == "ok"):
        _add(issues, INCONSISTENT_VALUES, _join(path, "status"),
             'interface status must be "ok" exactly when its up/down state was measured')
    return status


def _expected_group_statuses(item_statuses: list[str], item_count: int) -> frozenset[str]:
    """Group status implied by item statuses (same roll-up as the collector's contract)."""
    if item_count == 0:
        # Nothing found ("unavailable") or enumeration failed ("error"); data cannot tell which.
        return frozenset({"unavailable", "error"})
    if not item_statuses:  # every item skipped by policy
        return frozenset({"unavailable"})
    if all(s == "ok" for s in item_statuses):
        return frozenset({"ok"})
    if any(s == "ok" for s in item_statuses):
        return frozenset({"partial"})
    if "error" in item_statuses:
        return frozenset({"error"})
    return frozenset({"unavailable"})


def _check_group(issues: list[ValidationIssue], path: str, group: Any, items_key: str,
                 check_item: Callable[[list[ValidationIssue], str, Any], str | None]) -> None:
    """Validate ``{status, [reason], <items_key>: [...]}`` (disk and network)."""
    if not isinstance(group, dict):
        _add(issues, INVALID_TYPE, path, "section must be an object")
        return
    before = len(issues)
    _check_keys(issues, path, group, ("status", items_key), ("reason",))
    status = _check_enum(issues, _join(path, "status"), group, "status", _GROUP_STATUSES)
    if status is not None:
        _check_reason(issues, path, group, status)

    item_statuses: list[str | None] = []
    items = group.get(items_key)
    if items_key in group:
        if not isinstance(items, list):
            _add(issues, INVALID_TYPE, _join(path, items_key), "must be a list")
        else:
            item_statuses = [check_item(issues, f"{path}.{items_key}[{i}]", item)
                             for i, item in enumerate(items)]

    if status is not None and len(issues) == before:
        counted = [s for s in item_statuses if s != "not_collected"]  # skipped items don't degrade status
        if status not in _expected_group_statuses(counted, len(item_statuses)):
            _add(issues, INCONSISTENT_VALUES, _join(path, "status"),
                 "group status contradicts the statuses of its items")


def _check_network(issues: list[ValidationIssue], section: Any) -> None:
    _check_group(issues, "network", section, "interfaces", _check_interface)
    interfaces = section.get("interfaces") if isinstance(section, dict) else None
    if isinstance(interfaces, list):
        seen: set[str] = set()
        for index, interface in enumerate(interfaces):
            name = interface.get("name") if isinstance(interface, dict) else None
            if type(name) is str and name:
                if name in seen:
                    _add(issues, INCONSISTENT_VALUES, f"network.interfaces[{index}].name",
                         "interface names must be unique")
                seen.add(name)


def _check_errors(issues: list[ValidationIssue], errors: Any) -> None:
    if not isinstance(errors, list):
        _add(issues, INVALID_TYPE, "errors", "must be a list")
        return
    for index, entry in enumerate(errors):
        path = f"errors[{index}]"
        if not isinstance(entry, dict):
            _add(issues, INVALID_TYPE, path, "error entry must be an object")
            continue
        _check_keys(issues, path, entry, ("component", "error_type", "message"))
        for name in ("component", "error_type", "message"):
            if name in entry:
                valid = _check_text(issues, _join(path, name), entry[name])
                if valid and name == "message" and ("\n" in entry[name] or "\r" in entry[name]):
                    _add(issues, INVALID_VALUE, _join(path, name), "must be a single line")


# --------------------------------------------------------------------------
# Whole-snapshot consistency (runs only when everything above is clean, so the
# raw structure can be read directly)
# --------------------------------------------------------------------------

def _check_snapshot_consistency(issues: list[ValidationIssue], snapshot: dict) -> None:
    system = list(snapshot["system"].values())
    cpu = list(snapshot["cpu"].values())
    memory = list(snapshot["memory"].values())
    drives = snapshot["disk"]["drives"]
    interfaces = snapshot["network"]["interfaces"]
    drive_leaves = [drive[name] for drive in drives for name in _DRIVE_SPEC]
    interface_leaves = [interface[name] for interface in interfaces for name in ("is_up", "link_speed_mbps")]

    # Every failure the collector reports as "error" is also recorded in `errors`.
    all_statuses = ([leaf["status"] for leaf in system + cpu + memory + drive_leaves + interface_leaves]
                    + [drive["status"] for drive in drives]
                    + [snapshot["disk"]["status"], snapshot["network"]["status"]])
    if "error" in all_statuses and not snapshot["errors"]:
        _add(issues, INCONSISTENT_VALUES, "errors",
             'must contain an entry when any measurement or group has status "error"')

    # collection_status rules (README "Status rules"; independent re-statement of them).
    resource = cpu + memory + drive_leaves + interface_leaves
    if not any(leaf["status"] == "ok" for leaf in resource):
        expected = "failed"
    elif (all(leaf["status"] == "ok" for leaf in system + cpu + memory)
          and snapshot["disk"]["status"] == "ok" and snapshot["network"]["status"] == "ok"):
        expected = "complete"
    else:
        expected = "partial"
    if snapshot["collection_status"] != expected:
        _add(issues, INCONSISTENT_VALUES, "collection_status",
             "contradicts the measurement statuses under the documented collection-status rules")


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def _check_schema_version(issues: list[ValidationIssue], snapshot: dict) -> bool:
    """Return True only for the supported version. Never echoes the supplied value."""
    if "schema_version" not in snapshot:
        _add(issues, MISSING_FIELD, "schema_version", "required field is missing")
        return False
    value = snapshot["schema_version"]
    if type(value) is not int:  # also rejects True/False
        _add(issues, INVALID_TYPE, "schema_version", "must be an integer")
        return False
    if value != SUPPORTED_SCHEMA_VERSION:
        _add(issues, UNSUPPORTED_SCHEMA_VERSION, "schema_version",
             f"only schema version {SUPPORTED_SCHEMA_VERSION} is supported")
        return False
    return True


def validate_snapshot(snapshot: Any) -> ValidationResult:
    """Validate ``snapshot`` against the schema-version-1 contract.

    Returns a ``ValidationResult``; never raises for malformed data and never
    modifies ``snapshot``.
    """
    issues: list[ValidationIssue] = []
    if not isinstance(snapshot, dict):
        _add(issues, INVALID_ROOT_TYPE, "$", "snapshot must be a dictionary")
        return ValidationResult(None, issues)

    if not _check_schema_version(issues, snapshot):
        return ValidationResult(None, issues)  # v1 rules do not apply to an unknown contract

    _check_keys(issues, "", snapshot, _TOP_FIELDS)
    _check_timestamp(issues, snapshot)
    _check_collection_status_field(issues, snapshot)
    if "system" in snapshot:
        _check_leaf_section(issues, "system", snapshot["system"], _SYSTEM_SPEC)
    if "cpu" in snapshot:
        _check_leaf_section(issues, "cpu", snapshot["cpu"], _CPU_SPEC)
    if "memory" in snapshot:
        _check_memory(issues, snapshot["memory"])
    if "disk" in snapshot:
        _check_group(issues, "disk", snapshot["disk"], "drives", _check_drive)
    if "network" in snapshot:
        _check_network(issues, snapshot["network"])
    if "errors" in snapshot:
        _check_errors(issues, snapshot["errors"])

    if not issues:
        _check_snapshot_consistency(issues, snapshot)
    return ValidationResult(SUPPORTED_SCHEMA_VERSION, issues)
