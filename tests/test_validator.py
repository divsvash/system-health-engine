"""Contract tests for validator.py.

This file is hermetic: it builds synthetic snapshots (matching the schema the
collector actually emits) and never imports the collector or psutil. Whether the
collector's real output passes the validator is checked separately in
test_validator_collector_contract.py.
"""

from __future__ import annotations

import ast
import builtins
import copy
import dataclasses
import json
import os
import re
import socket
import subprocess
from pathlib import Path

import pytest

import validator
from validator import (
    INCONSISTENT_VALUES, INVALID_COLLECTION_STATUS, INVALID_ROOT_TYPE, INVALID_STATUS,
    INVALID_TIMESTAMP, INVALID_TYPE, INVALID_UNIT, INVALID_VALUE, ISSUE_CODES, MISSING_FIELD,
    NON_FINITE_NUMBER, NULL_VALUE_FOR_OK_STATUS, OUT_OF_RANGE, UNEXPECTED_FIELD,
    UNSUPPORTED_SCHEMA_VERSION, VALUE_FOR_NON_OK_STATUS, ValidationIssue, ValidationResult,
    validate_snapshot,
)

GIB = 1024 ** 3
NAN, INF = float("nan"), float("inf")


# --------------------------------------------------------------------------
# Snapshot builders (fresh objects on every call: tests never share state)
# --------------------------------------------------------------------------

def leaf(value, unit, status="ok", reason=None):
    out = {"value": value, "unit": unit, "status": status}
    if reason is not None:
        out["reason"] = reason
    return out


def na(unit, reason="API returned no value"):
    return leaf(None, unit, "unavailable", reason)


def failed_leaf(unit, reason="boom"):
    return leaf(None, unit, "error", reason)


def drive(mountpoint, total, used, free, percent, fstype="NTFS"):
    return {
        "mountpoint": mountpoint, "fstype": fstype, "status": "ok",
        "total_bytes": leaf(total, "bytes"), "used_bytes": leaf(used, "bytes"),
        "free_bytes": leaf(free, "bytes"), "used_percent": leaf(percent, "percent"),
    }


def interface(name, is_up, speed):
    return {
        "name": name, "status": "ok", "is_up": leaf(is_up, None),
        "link_speed_mbps": leaf(speed, "Mbps") if speed else na("Mbps", "link speed not reported"),
    }


def err(component="cpu.utilization_percent", error_type="PermissionError", message="Access is denied"):
    return {"component": component, "error_type": error_type, "message": message}


def build_snapshot():
    """A complete snapshot in exactly the shape collector.py produces."""
    return {
        "schema_version": 1,
        "observed_at": "2026-10-18T15:49:07Z",
        "collection_status": "complete",
        "system": {
            "os_name": leaf("Windows", None), "os_release": leaf("11", None),
            "os_version": leaf("10.0.22631", None), "machine": leaf("AMD64", None),
            "logical_cpu_count": leaf(8, "count"),
        },
        "cpu": {"utilization_percent": leaf(12.5, "percent"), "logical_cpu_count": leaf(8, "count")},
        "memory": {
            "total_bytes": leaf(16 * GIB, "bytes"), "available_bytes": leaf(6 * GIB, "bytes"),
            "used_percent": leaf(62.5, "percent"),
        },
        "disk": {"status": "ok", "drives": [
            drive("C:\\", 512 * GIB, 300 * GIB, 212 * GIB, 58.6),
            drive("D:\\", 1024 * GIB, 100 * GIB, 924 * GIB, 9.8),
        ]},
        "network": {"status": "ok", "interfaces": [
            interface("Ethernet", False, 0), interface("Wi-Fi", True, 866),
        ]},
        "errors": [],
    }


def skipped_drive():
    reason = "optical drive skipped to avoid a potentially blocking call"
    return {
        "mountpoint": "E:\\", "fstype": None, "status": "not_collected", "reason": reason,
        "total_bytes": leaf(None, "bytes", "not_collected", reason),
        "used_bytes": leaf(None, "bytes", "not_collected", reason),
        "free_bytes": leaf(None, "bytes", "not_collected", reason),
        "used_percent": leaf(None, "percent", "not_collected", reason),
    }


def variant_partial():
    s = build_snapshot()
    s["cpu"]["utilization_percent"] = failed_leaf("percent", "Access is denied")
    s["errors"] = [err()]
    s["collection_status"] = "partial"
    return s


def variant_failed():
    s = build_snapshot()
    s["cpu"] = {"utilization_percent": failed_leaf("percent"), "logical_cpu_count": failed_leaf("count")}
    s["memory"] = {"total_bytes": failed_leaf("bytes"), "available_bytes": failed_leaf("bytes"),
                   "used_percent": failed_leaf("percent")}
    s["disk"] = {"status": "error", "reason": "boom", "drives": []}
    s["network"] = {"status": "error", "reason": "boom", "interfaces": []}
    s["errors"] = [err("cpu.utilization_percent", "OSError", "boom"), err("memory", "OSError", "boom"),
                   err("disk.partitions", "OSError", "boom"), err("network.interfaces", "OSError", "boom")]
    s["collection_status"] = "failed"
    return s


def variant_skipped_drive():
    s = build_snapshot()
    s["disk"]["drives"].append(skipped_drive())
    return s  # skipped volumes do not degrade status: still complete


def variant_all_drives_skipped():
    s = build_snapshot()
    s["disk"] = {"status": "unavailable", "reason": "no items reported", "drives": [skipped_drive()]}
    s["collection_status"] = "partial"
    return s


def variant_unavailable_drive(with_reason=False):
    s = build_snapshot()
    d = s["disk"]["drives"][1]
    d["used_percent"] = na("percent")
    d["status"] = "unavailable"
    if with_reason:
        d["reason"] = "some measurements unavailable"
    s["disk"]["status"] = "partial"
    s["disk"]["reason"] = "some items could not be measured"
    s["collection_status"] = "partial"
    return s


def variant_error_drive():
    s = build_snapshot()
    reason = "Access is denied"
    d = s["disk"]["drives"][1]
    d.update(status="error", reason=reason, total_bytes=failed_leaf("bytes", reason),
             used_bytes=failed_leaf("bytes", reason), free_bytes=failed_leaf("bytes", reason),
             used_percent=failed_leaf("percent", reason))
    s["disk"]["status"] = "partial"
    s["disk"]["reason"] = "some items could not be measured"
    s["errors"] = [err("disk.drive[D:\\]", "PermissionError", reason)]
    s["collection_status"] = "partial"
    return s


def variant_network_empty():
    s = build_snapshot()
    s["network"] = {"status": "unavailable", "reason": "no network interfaces reported", "interfaces": []}
    s["collection_status"] = "partial"
    return s


def variant_interface_state_unknown():
    s = build_snapshot()
    s["network"]["interfaces"][1].update(status="unavailable", is_up=na(None, "interface state not reported"))
    s["network"].update(status="partial", reason="some items could not be measured")
    s["collection_status"] = "partial"
    return s


def variant_memory_unavailable():
    s = build_snapshot()
    s["memory"]["available_bytes"] = na("bytes")
    s["memory"]["used_percent"] = na("percent")
    s["collection_status"] = "partial"
    return s


def variant_system_unavailable():
    s = build_snapshot()
    s["system"]["machine"] = na(None)
    s["system"]["logical_cpu_count"] = na("count")
    s["collection_status"] = "partial"
    return s


def variant_boundaries():
    s = build_snapshot()
    s["cpu"]["utilization_percent"] = leaf(0.0, "percent")
    s["memory"]["available_bytes"] = leaf(16 * GIB, "bytes")  # available == total is allowed
    s["memory"]["used_percent"] = leaf(100.0, "percent")
    s["disk"]["drives"][0] = drive("C:\\", 100 * GIB, 100 * GIB, 0, 100.0)
    s["disk"]["drives"][1] = drive("D:\\", 0, 0, 0, 0.0, fstype=None)
    return s


def variant_disk_enumeration_error():
    s = build_snapshot()
    s["disk"] = {"status": "error", "reason": "enumeration failed", "drives": []}
    s["errors"] = [err("disk.partitions", "OSError", "enumeration failed")]
    s["collection_status"] = "partial"
    return s


def variant_unrelated_values_not_enforced():
    """Relationships the collector does not guarantee must not be enforced."""
    s = build_snapshot()
    s["system"]["os_name"] = leaf("Linux", None)               # not required to be Windows
    s["system"]["logical_cpu_count"] = leaf(4, "count")        # may differ from cpu count
    s["disk"]["drives"][0]["used_bytes"] = leaf(400 * GIB, "bytes")   # used + free != total
    s["disk"]["drives"][0]["used_percent"] = leaf(1.0, "percent")     # percent not recomputed
    s["memory"]["used_percent"] = leaf(5.0, "percent")                 # psutil's own figure
    return s


VALID_VARIANTS = {
    "complete": build_snapshot,
    "partial_cpu_error": variant_partial,
    "failed": variant_failed,
    "skipped_drive": variant_skipped_drive,
    "all_drives_skipped": variant_all_drives_skipped,
    "unavailable_drive": variant_unavailable_drive,
    "unavailable_drive_with_reason": lambda: variant_unavailable_drive(with_reason=True),
    "error_drive": variant_error_drive,
    "network_empty": variant_network_empty,
    "interface_state_unknown": variant_interface_state_unknown,
    "memory_unavailable": variant_memory_unavailable,
    "system_unavailable": variant_system_unavailable,
    "boundaries": variant_boundaries,
    "disk_enumeration_error": variant_disk_enumeration_error,
    "unrelated_values_not_enforced": variant_unrelated_values_not_enforced,
}


# --------------------------------------------------------------------------
# Test helpers
# --------------------------------------------------------------------------

_PART = re.compile(r"([^\[\]]+)((?:\[[0-9]+\])*)")


def tokens(path):
    """'disk.drives[1].status' -> ['disk', 'drives', 1, 'status']."""
    out = []
    for part in path.split("."):
        match = _PART.fullmatch(part)
        out.append(match.group(1))
        out.extend(int(i) for i in re.findall(r"\[([0-9]+)\]", match.group(2)))
    return out


def navigate(root, toks):
    for token in toks:
        root = root[token]
    return root


def mutated(path, value, base=build_snapshot):
    snap = base()
    toks = tokens(path)
    navigate(snap, toks[:-1])[toks[-1]] = value
    return snap


def without(path, base=build_snapshot):
    snap = base()
    toks = tokens(path)
    del navigate(snap, toks[:-1])[toks[-1]]
    return snap


def pairs(result):
    return [(i.code, i.path) for i in result.issues]


def assert_only(result, code, path):
    assert pairs(result) == [(code, path)]
    assert result.valid is False


def assert_valid(result):
    assert result.issues == [], pairs(result)
    assert result.valid is True
    assert result.schema_version == 1


# --------------------------------------------------------------------------
# 1-2. Valid snapshots pass
# --------------------------------------------------------------------------

def test_valid_complete_snapshot_passes():
    result = validate_snapshot(build_snapshot())
    assert_valid(result)
    assert isinstance(result, ValidationResult)


@pytest.mark.parametrize("name", VALID_VARIANTS)
def test_documented_valid_variants_pass(name):
    assert_valid(validate_snapshot(VALID_VARIANTS[name]()))


def test_valid_partial_snapshot_passes_and_is_not_confused_with_invalid():
    snap = variant_partial()
    assert snap["collection_status"] == "partial"
    assert_valid(validate_snapshot(snap))


def test_valid_snapshot_survives_json_round_trip():
    snap = json.loads(json.dumps(variant_partial()))
    assert_valid(validate_snapshot(snap))


# --------------------------------------------------------------------------
# 3. Missing fields
# --------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["observed_at", "collection_status", "system", "cpu", "memory",
                                 "disk", "network", "errors"])
def test_missing_top_level_field_is_rejected(key):
    assert_only(validate_snapshot(without(key)), MISSING_FIELD, key)


def test_missing_schema_version_is_rejected():
    result = validate_snapshot(without("schema_version"))
    assert_only(result, MISSING_FIELD, "schema_version")
    assert result.schema_version is None


@pytest.mark.parametrize("path", [
    "system.os_name", "system.logical_cpu_count", "cpu.utilization_percent",
    "cpu.utilization_percent.value", "cpu.utilization_percent.unit", "cpu.utilization_percent.status",
    "memory.total_bytes", "memory.available_bytes.value", "memory.used_percent.unit",
    "disk.status", "disk.drives", "disk.drives[0].mountpoint", "disk.drives[0].fstype",
    "disk.drives[0].status", "disk.drives[0].total_bytes", "disk.drives[1].free_bytes.unit",
    "network.status", "network.interfaces", "network.interfaces[0].name",
    "network.interfaces[0].status", "network.interfaces[0].is_up",
    "network.interfaces[1].link_speed_mbps", "network.interfaces[1].is_up.status",
])
def test_missing_nested_field_is_rejected(path):
    assert_only(validate_snapshot(without(path)), MISSING_FIELD, path)


@pytest.mark.parametrize("path,make", [
    ("cpu.utilization_percent.reason", variant_partial),
    ("disk.reason", variant_disk_enumeration_error),
    ("network.reason", variant_network_empty),
    ("disk.drives[0].reason", variant_all_drives_skipped),
])
def test_missing_reason_is_rejected_where_required(path, make):
    assert_only(validate_snapshot(without(path, make)), MISSING_FIELD, path)


@pytest.mark.parametrize("path", ["errors[0].component", "errors[0].error_type", "errors[0].message"])
def test_missing_error_entry_field_is_rejected(path):
    assert_only(validate_snapshot(without(path, variant_partial)), MISSING_FIELD, path)


# --------------------------------------------------------------------------
# 4. Wrong types
# --------------------------------------------------------------------------

@pytest.mark.parametrize("root", [None, [], (), "", "snapshot", 0, 1, 1.5, True, b"x", object()])
def test_wrong_root_type_is_rejected_without_crashing(root):
    result = validate_snapshot(root)
    assert_only(result, INVALID_ROOT_TYPE, "$")
    assert result.schema_version is None


@pytest.mark.parametrize("path,value,base", [
    ("system", [], build_snapshot), ("cpu", None, build_snapshot), ("memory", "x", build_snapshot),
    ("disk", [], build_snapshot), ("network", 5, build_snapshot),
    ("errors", {}, build_snapshot), ("errors", "none", build_snapshot),
    ("disk.drives", {}, build_snapshot), ("network.interfaces", "x", build_snapshot),
    ("disk.drives[0]", [], build_snapshot), ("network.interfaces[0]", "x", build_snapshot),
    ("cpu.utilization_percent", 12.5, build_snapshot), ("system.os_name", "Windows", build_snapshot),
    ("disk.drives[0].total_bytes", [1], build_snapshot),
    ("errors[0]", "boom", variant_partial), ("errors[0]", [], variant_partial),
])
def test_wrong_nested_type_is_rejected(path, value, base):
    assert_only(validate_snapshot(mutated(path, value, base)), INVALID_TYPE, path)


@pytest.mark.parametrize("path,value", [
    ("observed_at", None), ("observed_at", 20261018), ("collection_status", None),
    ("collection_status", ["complete"]), ("disk.drives[0].mountpoint", None),
    ("disk.drives[0].mountpoint", 5), ("disk.drives[0].fstype", 5),
    ("network.interfaces[0].name", None),
])
def test_wrong_primitive_type_is_rejected(path, value):
    assert_only(validate_snapshot(mutated(path, value)), INVALID_TYPE, path)


# --------------------------------------------------------------------------
# 5. Schema version
# --------------------------------------------------------------------------

@pytest.mark.parametrize("version", [0, 2, -1, 10 ** 9, 987654])
def test_unsupported_schema_version_is_rejected(version):
    result = validate_snapshot(mutated("schema_version", version))
    assert_only(result, UNSUPPORTED_SCHEMA_VERSION, "schema_version")
    assert result.schema_version is None


@pytest.mark.parametrize("version", [1.0, True, False, "1", "SECRET-VALUE", None, [], {}])
def test_non_integer_schema_version_is_rejected(version):
    result = validate_snapshot(mutated("schema_version", version))
    assert_only(result, INVALID_TYPE, "schema_version")
    assert result.schema_version is None


def test_untrusted_schema_version_is_not_echoed():
    for version in (987654, "SECRET-VALUE"):
        result = validate_snapshot(mutated("schema_version", version))
        text = " ".join(i.message + i.path for i in result.issues)
        assert str(version) not in text
        assert result.schema_version is None


def test_unknown_schema_version_does_not_apply_v1_rules_to_the_rest():
    snap = mutated("schema_version", 2)
    snap["cpu"] = None
    assert_only(validate_snapshot(snap), UNSUPPORTED_SCHEMA_VERSION, "schema_version")


# --------------------------------------------------------------------------
# 6. Timestamps
# --------------------------------------------------------------------------

@pytest.mark.parametrize("stamp", [
    "", "yesterday", "2026-10-18T15:49:07",              # naive
    "2026-10-18T15:49:07+00:00", "2026-10-18T15:49:07+05:30", "2026-10-18T15:49:07-05:00",
    "2026-10-18", "2026-10-18 15:49:07Z", "2026-10-18T15:49:07z", "2026-10-18T15:49:07.123Z",
    "20261018T154907Z", "2026-13-01T00:00:00Z", "2026-02-30T00:00:00Z", "2026-10-18T25:00:00Z",
    "2026-10-18T15:49:07Z\n", " 2026-10-18T15:49:07Z", "２０２６-10-18T15:49:07Z",
])
def test_invalid_or_non_utc_timestamp_is_rejected(stamp):
    assert_only(validate_snapshot(mutated("observed_at", stamp)), INVALID_TIMESTAMP, "observed_at")


def test_valid_timestamp_is_accepted_and_not_compared_with_clock():
    assert_valid(validate_snapshot(mutated("observed_at", "1999-12-31T23:59:59Z")))
    assert_valid(validate_snapshot(mutated("observed_at", "2999-01-01T00:00:00Z")))


# --------------------------------------------------------------------------
# 7. Status values
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["bogus", "OK", "Ok", "", None, 5, ["ok"], {}, True])
def test_unknown_measurement_status_is_rejected(status):
    assert_only(validate_snapshot(mutated("cpu.utilization_percent.status", status)),
                INVALID_STATUS, "cpu.utilization_percent.status")


@pytest.mark.parametrize("path,status", [
    ("cpu.utilization_percent.status", "not_collected"),      # only drive leaves may be skipped
    ("memory.total_bytes.status", "not_collected"),
    ("network.interfaces[0].is_up.status", "error"),          # interface leaves: ok/unavailable only
    ("network.interfaces[0].link_speed_mbps.status", "not_collected"),
    ("disk.status", "bogus"), ("disk.status", "not_collected"), ("network.status", "error?"),
    ("disk.drives[0].status", "bogus"), ("disk.drives[0].total_bytes.status", "bogus"),
    ("network.interfaces[0].status", "error"), ("network.interfaces[0].status", "partial"),
])
def test_status_not_allowed_in_context_is_rejected(path, status):
    assert_only(validate_snapshot(mutated(path, status)), INVALID_STATUS, path)


@pytest.mark.parametrize("value", ["bogus", "Complete", "COMPLETE", "", "ok", "failed "])
def test_unknown_collection_status_is_rejected(value):
    assert_only(validate_snapshot(mutated("collection_status", value)),
                INVALID_COLLECTION_STATUS, "collection_status")


# --------------------------------------------------------------------------
# 8-9. Status / value relationship
# --------------------------------------------------------------------------

OK_LEAF_PATHS = [
    "system.os_name.value", "system.logical_cpu_count.value", "cpu.utilization_percent.value",
    "memory.total_bytes.value", "memory.used_percent.value", "disk.drives[0].free_bytes.value",
    "disk.drives[1].used_percent.value", "network.interfaces[1].is_up.value",
    "network.interfaces[1].link_speed_mbps.value",
]


@pytest.mark.parametrize("path", OK_LEAF_PATHS)
def test_ok_measurement_with_null_value_is_rejected(path):
    assert_only(validate_snapshot(mutated(path, None)), NULL_VALUE_FOR_OK_STATUS, path)


@pytest.mark.parametrize("status,populated", [
    ("unavailable", 0.0), ("unavailable", 12.5), ("unavailable", "n/a"), ("unavailable", False),
    ("error", 0.0), ("error", 5.0), ("error", ""), ("error", "boom"),
])
def test_unavailable_or_failed_measurement_with_value_is_rejected(status, populated):
    snap = mutated("cpu.utilization_percent", leaf(populated, "percent", status, "why"))
    assert_only(validate_snapshot(snap), VALUE_FOR_NON_OK_STATUS, "cpu.utilization_percent.value")


def test_not_collected_measurement_with_value_is_rejected():
    snap = variant_all_drives_skipped()
    snap["disk"]["drives"][0]["total_bytes"]["value"] = 0
    assert_only(validate_snapshot(snap), VALUE_FOR_NON_OK_STATUS, "disk.drives[0].total_bytes.value")


def test_unavailable_link_speed_must_stay_null_not_zero():
    snap = mutated("network.interfaces[0].link_speed_mbps.value", 0)  # speed leaf is unavailable here
    assert_only(validate_snapshot(snap), VALUE_FOR_NON_OK_STATUS, "network.interfaces[0].link_speed_mbps.value")


def test_reason_rules_for_measurements():
    # non-ok needs a non-empty string reason; ok must not carry one
    snap = variant_partial()
    snap["cpu"]["utilization_percent"]["reason"] = ""
    assert_only(validate_snapshot(snap), INVALID_VALUE, "cpu.utilization_percent.reason")
    snap["cpu"]["utilization_percent"]["reason"] = 5
    assert_only(validate_snapshot(snap), INVALID_TYPE, "cpu.utilization_percent.reason")
    ok_with_reason = build_snapshot()
    ok_with_reason["cpu"]["utilization_percent"]["reason"] = "should not be here"
    assert_only(validate_snapshot(ok_with_reason), INCONSISTENT_VALUES, "cpu.utilization_percent.reason")


# --------------------------------------------------------------------------
# 10-12. Python-specific type traps
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path,value", [
    ("cpu.utilization_percent.value", "12.5"), ("memory.used_percent.value", "62.5"),
    ("disk.drives[0].used_percent.value", "58.6"),
    ("memory.total_bytes.value", "17179869184"), ("disk.drives[0].free_bytes.value", "1"),
    ("cpu.logical_cpu_count.value", "8"), ("system.logical_cpu_count.value", "8"),
    ("network.interfaces[1].link_speed_mbps.value", "866"),
    ("network.interfaces[1].is_up.value", "true"),
])
def test_numeric_or_boolean_strings_are_rejected_not_coerced(path, value):
    assert_only(validate_snapshot(mutated(path, value)), INVALID_TYPE, path)


@pytest.mark.parametrize("path", [
    "memory.total_bytes.value", "memory.available_bytes.value", "disk.drives[0].total_bytes.value",
    "disk.drives[1].used_bytes.value", "cpu.logical_cpu_count.value", "system.logical_cpu_count.value",
    "network.interfaces[1].link_speed_mbps.value", "cpu.utilization_percent.value",
])
@pytest.mark.parametrize("value", [True, False])
def test_booleans_are_rejected_where_numbers_are_required(path, value):
    assert_only(validate_snapshot(mutated(path, value)), INVALID_TYPE, path)


def test_integers_are_rejected_for_booleans_and_vice_versa():
    for value in (1, 0):
        assert_only(validate_snapshot(mutated("network.interfaces[1].is_up.value", value)),
                    INVALID_TYPE, "network.interfaces[1].is_up.value")
    # percent must be a float; byte counts and counts must be ints
    assert_only(validate_snapshot(mutated("cpu.utilization_percent.value", 50)),
                INVALID_TYPE, "cpu.utilization_percent.value")
    for value in (1.0, 1e10):
        assert_only(validate_snapshot(mutated("memory.total_bytes.value", value)),
                    INVALID_TYPE, "memory.total_bytes.value")
    assert_only(validate_snapshot(mutated("cpu.logical_cpu_count.value", 8.0)),
                INVALID_TYPE, "cpu.logical_cpu_count.value")


@pytest.mark.parametrize("path", [
    "cpu.utilization_percent.value", "memory.used_percent.value", "disk.drives[0].used_percent.value",
])
@pytest.mark.parametrize("value", [NAN, INF, -INF])
def test_non_finite_percentages_are_rejected(path, value):
    assert_only(validate_snapshot(mutated(path, value)), NON_FINITE_NUMBER, path)


@pytest.mark.parametrize("path", ["memory.total_bytes.value", "disk.drives[0].total_bytes.value",
                                  "network.interfaces[1].link_speed_mbps.value"])
@pytest.mark.parametrize("value", [NAN, INF, -INF])
def test_non_finite_integers_are_rejected(path, value):
    assert_only(validate_snapshot(mutated(path, value)), INVALID_TYPE, path)


# --------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path,unit", [
    ("cpu.utilization_percent.unit", "%"), ("cpu.utilization_percent.unit", "Percent"),
    ("cpu.utilization_percent.unit", None), ("memory.total_bytes.unit", "KB"),
    ("memory.total_bytes.unit", "bytes "), ("cpu.logical_cpu_count.unit", None),
    ("network.interfaces[1].link_speed_mbps.unit", "Gbps"),
    ("network.interfaces[1].link_speed_mbps.unit", "mbps"),
    ("system.os_name.unit", "string"), ("network.interfaces[0].is_up.unit", "bool"),
    ("disk.drives[0].used_bytes.unit", "percent"), ("cpu.utilization_percent.unit", 5),
])
def test_wrong_unit_is_rejected(path, unit):
    assert_only(validate_snapshot(mutated(path, unit)), INVALID_UNIT, path)


def test_unit_is_enforced_even_when_value_is_unavailable():
    snap = variant_partial()
    snap["cpu"]["utilization_percent"]["unit"] = "bytes"
    assert_only(validate_snapshot(snap), INVALID_UNIT, "cpu.utilization_percent.unit")


# --------------------------------------------------------------------------
# 13-15. Ranges and cross-field relationships
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value", [100.1, 140.0, 1e9, -0.1, -100.0])
def test_cpu_utilization_outside_0_100_is_rejected_not_clamped(value):
    snap = mutated("cpu.utilization_percent.value", value)
    result = validate_snapshot(snap)
    assert_only(result, OUT_OF_RANGE, "cpu.utilization_percent.value")
    assert snap["cpu"]["utilization_percent"]["value"] == value  # not clamped


@pytest.mark.parametrize("value", [0.0, 100.0, 0.001, 99.999])
def test_cpu_utilization_boundaries_are_accepted(value):
    assert_valid(validate_snapshot(mutated("cpu.utilization_percent.value", value)))


def test_unavailable_cpu_is_not_a_successful_zero():
    ok_zero = mutated("cpu.utilization_percent.value", 0.0)
    assert_valid(validate_snapshot(ok_zero))                      # a real measured 0.0 is fine
    fake_zero = mutated("cpu.utilization_percent", leaf(0.0, "percent", "unavailable", "none"))
    assert_only(validate_snapshot(fake_zero), VALUE_FOR_NON_OK_STATUS, "cpu.utilization_percent.value")


@pytest.mark.parametrize("path", [
    "memory.total_bytes.value", "memory.available_bytes.value", "disk.drives[0].total_bytes.value",
    "disk.drives[0].used_bytes.value", "disk.drives[1].free_bytes.value",
])
@pytest.mark.parametrize("value", [-1, -(2 ** 40)])
def test_negative_byte_counts_are_rejected(path, value):
    assert_only(validate_snapshot(mutated(path, value)), OUT_OF_RANGE, path)


@pytest.mark.parametrize("path", ["system.logical_cpu_count.value", "cpu.logical_cpu_count.value"])
@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_cpu_count_is_rejected(path, value):
    assert_only(validate_snapshot(mutated(path, value)), OUT_OF_RANGE, path)


@pytest.mark.parametrize("path", ["memory.used_percent.value", "disk.drives[0].used_percent.value"])
@pytest.mark.parametrize("value", [100.01, -0.5, 250.0])
def test_percentages_outside_0_100_are_rejected(path, value):
    assert_only(validate_snapshot(mutated(path, value)), OUT_OF_RANGE, path)


def test_available_memory_exceeding_total_is_rejected():
    snap = mutated("memory.available_bytes.value", 16 * GIB + 1)
    assert_only(validate_snapshot(snap), INCONSISTENT_VALUES, "memory.available_bytes.value")


def test_memory_relationship_is_skipped_when_a_value_is_unavailable():
    assert_valid(validate_snapshot(variant_memory_unavailable()))


@pytest.mark.parametrize("name", ["used_bytes", "free_bytes"])
def test_drive_used_or_free_exceeding_total_is_rejected(name):
    path = f"disk.drives[1].{name}.value"
    assert_only(validate_snapshot(mutated(path, 1024 * GIB + 1)), INCONSISTENT_VALUES, path)


def test_negative_link_speed_is_rejected():
    path = "network.interfaces[1].link_speed_mbps.value"
    assert_only(validate_snapshot(mutated(path, -100)), OUT_OF_RANGE, path)


def test_zero_link_speed_cannot_be_a_successful_measurement():
    path = "network.interfaces[1].link_speed_mbps.value"
    assert_only(validate_snapshot(mutated(path, 0)), OUT_OF_RANGE, path)


# --------------------------------------------------------------------------
# Drive / interface / group structure
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path,value,code", [
    ("network.interfaces[0].name", "", INVALID_VALUE),
    ("network.interfaces[0].name", 5, INVALID_TYPE),
    ("network.interfaces[0].is_up.value", "yes", INVALID_TYPE),
    ("network.interfaces[0].is_up.unit", "bool", INVALID_UNIT),
    ("network.interfaces[1].link_speed_mbps.value", "1000", INVALID_TYPE),
    ("network.interfaces[1].link_speed_mbps.value", 866.0, INVALID_TYPE),
    ("network.interfaces[1].link_speed_mbps.unit", "Gbps", INVALID_UNIT),
    ("network.interfaces[1].link_speed_mbps.status", "error", INVALID_STATUS),
    ("disk.drives[0].mountpoint", "", INVALID_VALUE),
    ("disk.drives[0].fstype", "", INVALID_VALUE),
])
def test_invalid_interface_and_drive_fields_are_rejected(path, value, code):
    assert_only(validate_snapshot(mutated(path, value)), code, path)


def test_duplicate_interface_names_are_rejected():
    snap = mutated("network.interfaces[1].name", "Ethernet")
    assert_only(validate_snapshot(snap), INCONSISTENT_VALUES, "network.interfaces[1].name")


def test_interface_status_must_match_up_state_measurement():
    wrong_ok = mutated("network.interfaces[1].is_up", na(None, "interface state not reported"))
    assert_only(validate_snapshot(wrong_ok), INCONSISTENT_VALUES, "network.interfaces[1].status")
    wrong_unavailable = mutated("network.interfaces[1].status", "unavailable")
    assert_only(validate_snapshot(wrong_unavailable), INCONSISTENT_VALUES, "network.interfaces[1].status")


def test_interface_with_reason_field_is_rejected():
    snap = mutated("network.interfaces[0].reason", "x")
    assert_only(validate_snapshot(snap), UNEXPECTED_FIELD, "network.interfaces[0].reason")


def test_drive_reason_rules():
    ok_with_reason = mutated("disk.drives[0].reason", "x")
    assert_only(validate_snapshot(ok_with_reason), INCONSISTENT_VALUES, "disk.drives[0].reason")
    assert_valid(validate_snapshot(variant_unavailable_drive(False)))   # optional for "unavailable"
    assert_valid(validate_snapshot(variant_unavailable_drive(True)))


# --------------------------------------------------------------------------
# 16. Overall collection status and group consistency
# --------------------------------------------------------------------------

@pytest.mark.parametrize("claimed,base", [
    ("complete", variant_partial),                  # a failed measurement cannot be "complete"
    ("failed", variant_partial),                    # some data was collected
    ("partial", build_snapshot),                    # everything was ok
    ("failed", build_snapshot),
    ("complete", variant_failed),
    ("partial", variant_failed),                    # nothing meaningful was collected
    ("complete", variant_unavailable_drive),
    ("complete", variant_network_empty),
    ("complete", variant_system_unavailable),       # system metadata counts toward "complete"
    ("failed", variant_system_unavailable),
    ("complete", variant_disk_enumeration_error),
    ("complete", variant_all_drives_skipped),
])
def test_contradictory_collection_status_is_rejected(claimed, base):
    snap = base()
    snap["collection_status"] = claimed
    assert_only(validate_snapshot(snap), INCONSISTENT_VALUES, "collection_status")


def test_failed_snapshot_with_ok_system_metadata_is_still_failed():
    snap = variant_failed()
    assert snap["system"]["os_name"]["status"] == "ok"
    assert_valid(validate_snapshot(snap))


@pytest.mark.parametrize("mutator,path", [
    (lambda s: (s["disk"].update(status="ok"), s["disk"].pop("reason")), "disk.status"),  # with an error drive
    (lambda s: s["disk"].update(status="unavailable"), "disk.status"),
    (lambda s: s["disk"].update(status="error"), "disk.status"),
])
def test_contradictory_disk_group_status_is_rejected(mutator, path):
    snap = variant_error_drive()
    mutator(snap)
    assert_only(validate_snapshot(snap), INCONSISTENT_VALUES, path)


@pytest.mark.parametrize("status", ["partial", "unavailable", "error"])
def test_group_status_must_be_ok_when_all_items_ok(status):
    snap = build_snapshot()
    snap["disk"]["status"] = status
    snap["disk"]["reason"] = "claimed problem"
    assert_only(validate_snapshot(snap), INCONSISTENT_VALUES, "disk.status")


def test_group_ok_with_unmeasured_interface_is_rejected():
    snap = variant_interface_state_unknown()
    snap["network"].pop("reason")
    snap["network"]["status"] = "ok"
    assert_only(validate_snapshot(snap), INCONSISTENT_VALUES, "network.status")


def test_group_reason_rules():
    snap = build_snapshot()
    snap["disk"]["reason"] = "x"
    assert_only(validate_snapshot(snap), INCONSISTENT_VALUES, "disk.reason")
    assert_only(validate_snapshot(without("disk.reason", variant_disk_enumeration_error)),
                MISSING_FIELD, "disk.reason")


def test_empty_group_may_be_unavailable_or_error():
    assert_valid(validate_snapshot(variant_network_empty()))
    assert_valid(validate_snapshot(variant_disk_enumeration_error()))
    snap = variant_network_empty()
    snap["network"]["status"] = "ok"
    snap["network"].pop("reason")
    assert_only(validate_snapshot(snap), INCONSISTENT_VALUES, "network.status")


@pytest.mark.parametrize("claimed", ["ok", "unavailable", "not_collected"])
def test_drive_status_must_match_its_measurements(claimed):
    snap = variant_error_drive()                 # all four measurements have status "error"
    drive_entry = snap["disk"]["drives"][1]
    drive_entry["status"] = claimed
    if claimed == "ok":
        drive_entry.pop("reason")                # isolate the status/measurement contradiction
    snap["disk"]["status"] = "partial"
    assert_only(validate_snapshot(snap), INCONSISTENT_VALUES, "disk.drives[1].status")


def test_drive_ok_with_unavailable_measurement_is_rejected():
    snap = build_snapshot()
    snap["disk"]["drives"][0]["used_percent"] = na("percent")
    assert_only(validate_snapshot(snap), INCONSISTENT_VALUES, "disk.drives[0].status")


def test_drive_unavailable_with_all_measurements_ok_is_rejected():
    snap = build_snapshot()
    snap["disk"]["drives"][0]["status"] = "unavailable"
    snap["disk"].update(status="partial", reason="some items could not be measured")
    assert_only(validate_snapshot(snap), INCONSISTENT_VALUES, "disk.drives[0].status")


def test_error_status_requires_an_entry_in_errors():
    snap = variant_partial()
    snap["errors"] = []
    assert_only(validate_snapshot(snap), INCONSISTENT_VALUES, "errors")


def test_unavailable_alone_does_not_require_errors():
    assert variant_memory_unavailable()["errors"] == []
    assert_valid(validate_snapshot(variant_memory_unavailable()))


@pytest.mark.parametrize("path,value,code", [
    ("errors[0].component", "", INVALID_VALUE),
    ("errors[0].error_type", 5, INVALID_TYPE),
    ("errors[0].message", "line one\nline two", INVALID_VALUE),
    ("errors[0].message", "carriage\rreturn", INVALID_VALUE),
    ("errors[0].message", None, INVALID_TYPE),
])
def test_invalid_error_entries_are_rejected(path, value, code):
    assert_only(validate_snapshot(mutated(path, value, variant_partial)), code, path)


def test_consistency_checks_are_skipped_when_structure_is_invalid():
    """Documented policy: collection_status is only judged on a structurally clean snapshot."""
    snap = variant_partial()
    snap["collection_status"] = "complete"          # contradictory...
    snap["observed_at"] = "not a time"              # ...but structure is already invalid
    assert pairs(validate_snapshot(snap)) == [(INVALID_TIMESTAMP, "observed_at")]


# --------------------------------------------------------------------------
# Unknown fields (policy: rejected, never interpreted)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("parent,base", [
    ("", build_snapshot), ("system", build_snapshot), ("cpu", build_snapshot),
    ("memory", build_snapshot), ("cpu.utilization_percent", build_snapshot),
    ("disk", build_snapshot), ("disk.drives[0]", build_snapshot), ("disk.drives[0].total_bytes", build_snapshot),
    ("network", build_snapshot), ("network.interfaces[0]", build_snapshot),
    ("errors[0]", variant_partial),
])
def test_unknown_fields_are_rejected(parent, base):
    snap = base()
    target = navigate(snap, tokens(parent)) if parent else snap
    target["extra"] = 1
    expected_path = f"{parent}.extra" if parent else "extra"
    assert_only(validate_snapshot(snap), UNEXPECTED_FIELD, expected_path)


def test_unknown_field_is_not_interpreted_as_a_measurement():
    snap = build_snapshot()
    snap["cpu"]["temperature_celsius"] = leaf(55.0, "celsius")   # looks like a valid measurement
    assert_only(validate_snapshot(snap), UNEXPECTED_FIELD, "cpu.temperature_celsius")


def test_hostname_and_identifiers_are_not_part_of_the_contract():
    snap = build_snapshot()
    snap["system"]["hostname"] = leaf("MY-PC", None)
    assert_only(validate_snapshot(snap), UNEXPECTED_FIELD, "system.hostname")


def test_untrusted_key_names_are_sanitised_in_paths():
    snap = build_snapshot()
    snap["cpu"]["bad\nkey\x00" + "x" * 200] = 1
    snap["cpu"][42] = 2
    result = validate_snapshot(snap)
    assert sorted(i.code for i in result.issues) == [UNEXPECTED_FIELD, UNEXPECTED_FIELD]
    for issue in result.issues:
        assert "\n" not in issue.path and "\x00" not in issue.path
        assert len(issue.path) < 100
    assert "cpu.<non-string key>" in [i.path for i in result.issues]


# --------------------------------------------------------------------------
# 17. Multiple independent errors in one run
# --------------------------------------------------------------------------

def test_multiple_independent_errors_are_reported_together_in_a_stable_order():
    snap = build_snapshot()
    snap["observed_at"] = "yesterday"
    snap["cpu"]["utilization_percent"]["value"] = 140.0
    snap["memory"]["total_bytes"]["value"] = -1
    snap["disk"]["drives"][1]["status"] = "bogus"
    snap["network"]["interfaces"][0]["name"] = ""
    snap["system"]["hostname"] = leaf("PC", None)
    snap["errors"] = {}

    result = validate_snapshot(snap)
    assert pairs(result) == [
        (INVALID_TIMESTAMP, "observed_at"),
        (UNEXPECTED_FIELD, "system.hostname"),
        (OUT_OF_RANGE, "cpu.utilization_percent.value"),
        (OUT_OF_RANGE, "memory.total_bytes.value"),
        (INVALID_STATUS, "disk.drives[1].status"),
        (INVALID_VALUE, "network.interfaces[0].name"),
        (INVALID_TYPE, "errors"),
    ]
    assert result.valid is False
    assert result.schema_version == 1
    assert pairs(validate_snapshot(snap)) == pairs(result)


def test_several_bad_fields_in_one_leaf_are_all_reported():
    snap = build_snapshot()
    snap["cpu"]["utilization_percent"] = {"value": "x", "unit": "%", "status": "ok", "extra": 1}
    assert pairs(validate_snapshot(snap)) == [
        (UNEXPECTED_FIELD, "cpu.utilization_percent.extra"),
        (INVALID_UNIT, "cpu.utilization_percent.unit"),
        (INVALID_TYPE, "cpu.utilization_percent.value"),
    ]


# --------------------------------------------------------------------------
# 18. Input is never mutated
# --------------------------------------------------------------------------

class _FrozenDict(dict):
    def _blocked(self, *args, **kwargs):
        raise AssertionError("validator mutated its input")
    __setitem__ = __delitem__ = pop = popitem = clear = update = setdefault = _blocked


class _FrozenList(list):
    def _blocked(self, *args, **kwargs):
        raise AssertionError("validator mutated its input")
    __setitem__ = __delitem__ = append = extend = insert = pop = remove = clear = _blocked
    sort = reverse = __iadd__ = __imul__ = _blocked


def freeze(obj):
    if isinstance(obj, dict):
        return _FrozenDict((k, freeze(v)) for k, v in obj.items())
    if isinstance(obj, list):
        return _FrozenList(freeze(v) for v in obj)
    return obj


MALFORMED_SNAPSHOTS = {
    "nan": lambda: mutated("cpu.utilization_percent.value", NAN),
    "types": lambda: mutated("memory.total_bytes.value", "17"),
    "version": lambda: mutated("schema_version", 7),
    "multiple": lambda: {**build_snapshot(), "observed_at": "x", "errors": None, "extra": 1},
    "contradiction": lambda: {**variant_partial(), "collection_status": "complete"},
}


@pytest.mark.parametrize("make", [*VALID_VARIANTS.values(), *MALFORMED_SNAPSHOTS.values()])
def test_input_is_not_mutated(make):
    snap = make()
    before = repr(snap)
    validate_snapshot(snap)
    assert repr(snap) == before
    validate_snapshot(freeze(make()))  # a mutation attempt on frozen containers raises AssertionError


def test_failed_validation_leaves_input_identical():
    snap = mutated("cpu.utilization_percent.value", 140.0)
    original = copy.deepcopy(snap)
    assert validate_snapshot(snap).valid is False
    assert snap == original
    assert snap["cpu"]["utilization_percent"]["value"] == 140.0   # not clamped or repaired


# --------------------------------------------------------------------------
# 19. Determinism
# --------------------------------------------------------------------------

def reordered(obj):
    """Same content, dict keys in reverse order at every level."""
    if isinstance(obj, dict):
        return {k: reordered(v) for k, v in reversed(list(obj.items()))}
    if isinstance(obj, list):
        return [reordered(v) for v in obj]
    return obj


@pytest.mark.parametrize("make", [*VALID_VARIANTS.values(), *MALFORMED_SNAPSHOTS.values()])
def test_equivalent_inputs_give_equal_results(make):
    snap = make()
    first = validate_snapshot(snap)
    assert validate_snapshot(snap) == first
    assert validate_snapshot(copy.deepcopy(snap)) == first
    assert validate_snapshot(reordered(snap)) == first


def test_unexpected_fields_are_reported_in_a_stable_order():
    snap = build_snapshot()
    for key in ("zeta", "alpha", "mid"):
        snap[key] = 1
    expected = ["alpha", "mid", "zeta"]
    assert [i.path for i in validate_snapshot(snap).issues] == expected
    assert [i.path for i in validate_snapshot(reordered(snap)).issues] == expected


def test_results_do_not_leak_state_between_calls():
    bad = validate_snapshot(mutated("cpu.utilization_percent.value", 140.0))
    good = validate_snapshot(build_snapshot())
    assert bad.valid is False and good.valid is True
    assert good.issues == [] and good.issues is not bad.issues
    again = validate_snapshot(mutated("cpu.utilization_percent.value", 140.0))
    assert again == bad


def test_contract_tables_are_not_modified_by_validation():
    tables = ("_SYSTEM_SPEC", "_CPU_SPEC", "_MEMORY_SPEC", "_DRIVE_SPEC", "_KINDS")
    before = {name: repr(getattr(validator, name)) for name in tables}
    for make in (*VALID_VARIANTS.values(), *MALFORMED_SNAPSHOTS.values()):
        validate_snapshot(make())
    assert {name: repr(getattr(validator, name)) for name in tables} == before


# --------------------------------------------------------------------------
# 20. Ordinary malformed input never crashes the validator
# --------------------------------------------------------------------------

JUNK = [None, True, False, 0, -1, 1.5, NAN, INF, "", "x", "1", [], {}, [None], {"a": 1},
        ("tuple",), b"bytes", object()]


def node_paths(obj, prefix=()):
    """Every dict key / list index in the snapshot, as token paths."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield prefix + (key,)
            yield from node_paths(value, prefix + (key,))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield prefix + (index,)
            yield from node_paths(value, prefix + (index,))


def assert_well_formed(result):
    assert isinstance(result, ValidationResult)
    assert result.schema_version in (1, None)
    assert result.valid == (not result.issues)
    for issue in result.issues:
        assert isinstance(issue, ValidationIssue)
        assert issue.code in ISSUE_CODES
        assert issue.severity == "error"
        assert isinstance(issue.path, str) and issue.path
        assert isinstance(issue.message, str) and issue.message


@pytest.mark.parametrize("base", [build_snapshot, variant_partial, variant_failed, variant_skipped_drive],
                         ids=["complete", "partial", "failed", "skipped_drive"])
def test_replacing_or_deleting_any_node_never_crashes(base):
    paths = list(node_paths(base()))
    assert len(paths) > 60  # the sweep must be non-trivial
    for toks in paths:
        for junk in JUNK:
            snap = base()
            navigate(snap, toks[:-1])[toks[-1]] = junk
            assert_well_formed(validate_snapshot(snap))
        snap = base()
        del navigate(snap, toks[:-1])[toks[-1]]
        assert_well_formed(validate_snapshot(snap))


def test_self_referencing_snapshot_does_not_crash_or_recurse():
    snap = variant_partial()
    snap["errors"].append(snap)
    snap["cpu"]["self"] = snap["cpu"]
    result = validate_snapshot(snap)
    assert_well_formed(result)
    assert result.valid is False


def test_dict_subclass_and_odd_keys_are_handled():
    snap = build_snapshot()
    snap["system"][("tuple", "key")] = 1
    snap["system"][None] = 2
    assert_well_formed(validate_snapshot(snap))
    assert_well_formed(validate_snapshot(_FrozenDict(build_snapshot())))


# --------------------------------------------------------------------------
# Result contract
# --------------------------------------------------------------------------

def test_result_types_follow_the_documented_contract():
    result = validate_snapshot(mutated("cpu.utilization_percent.value", 140.0))
    assert dataclasses.is_dataclass(result) and dataclasses.is_dataclass(result.issues[0])
    issue = result.issues[0]
    assert (issue.code, issue.path, issue.severity) == (OUT_OF_RANGE, "cpu.utilization_percent.value", "error")
    assert isinstance(issue.message, str) and issue.message
    with pytest.raises(dataclasses.FrozenInstanceError):
        issue.code = "OTHER"
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.schema_version = 2


def test_valid_is_true_if_and_only_if_there_are_no_error_issues():
    assert ValidationResult(1, []).valid is True
    assert ValidationResult(1, [ValidationIssue("X", "p", "m")]).valid is False
    assert ValidationResult(None, [ValidationIssue("X", "p", "m", "error")]).valid is False


def test_every_documented_code_is_defined_and_stable():
    assert {INVALID_ROOT_TYPE, MISSING_FIELD, INVALID_TYPE, UNSUPPORTED_SCHEMA_VERSION,
            INVALID_TIMESTAMP, INVALID_STATUS, INVALID_UNIT, NULL_VALUE_FOR_OK_STATUS,
            OUT_OF_RANGE, INCONSISTENT_VALUES, INVALID_COLLECTION_STATUS} <= ISSUE_CODES
    assert all(code == code.upper() and " " not in code for code in ISSUE_CODES)


# --------------------------------------------------------------------------
# 21. Purity: no collection, I/O, subprocess or network
# --------------------------------------------------------------------------

def test_validation_performs_no_io_subprocess_or_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("forbidden side effect attempted during validation")

    snapshots = [make() for make in (*VALID_VARIANTS.values(), *MALFORMED_SNAPSHOTS.values())]
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", blocked)          # any file access, read or write
        patch.setattr(socket.socket, "connect", blocked)
        patch.setattr(socket.socket, "connect_ex", blocked)
        patch.setattr(socket, "create_connection", blocked)
        patch.setattr(socket, "getaddrinfo", blocked)
        patch.setattr(subprocess, "Popen", blocked)
        patch.setattr(os, "system", blocked)
        for snap in snapshots:
            assert_well_formed(validate_snapshot(snap))


SOURCE = Path(validator.__file__).read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
ALLOWED_IMPORTS = {"__future__", "dataclasses", "datetime", "math", "re", "typing"}
ALLOWED_MODULE_ATTRIBUTES = {"datetime": {"fromisoformat"}, "math": {"isfinite"}, "re": {"compile"}}
FORBIDDEN_CALLS = {"open", "eval", "exec", "compile", "__import__", "input", "print", "getattr",
                   "setattr", "delattr", "globals", "locals", "vars"}


def test_static_validator_imports_only_allowlisted_stdlib_modules():
    imported = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= ALLOWED_IMPORTS, imported - ALLOWED_IMPORTS
    assert not imported & {"collector", "psutil", "os", "subprocess", "socket", "platform"}


def test_static_validator_uses_only_allowlisted_module_attributes():
    used = {}
    for node in ast.walk(TREE):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id in ALLOWED_MODULE_ATTRIBUTES):
            used.setdefault(node.value.id, set()).add(node.attr)
    for module, attrs in used.items():
        assert attrs <= ALLOWED_MODULE_ATTRIBUTES[module], (module, attrs)
    assert used == {"datetime": {"fromisoformat"}, "math": {"isfinite"}, "re": {"compile"}}  # not vacuous


def test_static_validator_has_no_dynamic_execution_or_file_builtins():
    called = {node.func.id for node in ast.walk(TREE)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert not called & FORBIDDEN_CALLS, called & FORBIDDEN_CALLS


def test_static_validator_has_no_broad_exception_handler():
    handlers = [node for node in ast.walk(TREE) if isinstance(node, ast.ExceptHandler)]
    assert [ast.unparse(h.type) for h in handlers] == ["ValueError"]   # only the anticipated timestamp error


# What these tests do NOT prove:
# * The static checks only read validator.py's own source; they cannot see what
#   the standard library does internally, or a later edit that also edits the
#   allowlists above.
# * The "no I/O" test patches specific functions (open, sockets, subprocess,
#   os.system) for the duration of the calls; it does not trace every possible
#   OS interaction.
# * Passing validation shows a snapshot is well-formed and internally
#   consistent. It does not show the readings are true, that the collector was
#   honest, or that the computer is healthy.
