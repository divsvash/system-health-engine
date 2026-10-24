"""Tests for diagnostics.py.

Fixtures reuse the collector-shaped builders from test_validator.py (hand-built
snapshots in exactly the schema collector.py emits; no psutil needed). The last
section feeds real ``collect_system_snapshot()`` output (psutil faked) through
the engine so the modules cannot drift apart; it needs psutil and is skipped
cleanly where psutil is not installed.

Nothing here needs hardware failures, admin rights, internet access, or any
change to the host.
"""

from __future__ import annotations

import ast
import builtins
import copy
import dataclasses
import json
import os
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import diagnostics
import test_validator as tv
from diagnostics import (
    DEFAULT_POLICY, DiagnosticEvidence, DiagnosticFinding, DiagnosticPolicy, DiagnosticReport,
    DriveProfile, HardwareProfile, REASON_MEASUREMENT_ERROR, REASON_MEASUREMENT_UNAVAILABLE,
    REASON_NO_DRIVES, REASON_NOT_COLLECTED, REASON_ZERO_CAPACITY, RULE_DISK_CRITICAL,
    RULE_DISK_WARNING, RULE_MEMORY_CRITICAL, RULE_MEMORY_WARNING, SEVERITY_CRITICAL,
    SEVERITY_WARNING, SkippedRule, diagnose_snapshot,
)
from validator import ValidationIssue, ValidationResult, validate_snapshot

GIB = 1024 ** 3
MOUNTS = ("C:\\", "D:\\", "E:\\", "F:\\", "G:\\")
ALL_RULE_IDS = {RULE_MEMORY_WARNING, RULE_MEMORY_CRITICAL, RULE_DISK_WARNING, RULE_DISK_CRITICAL}
W, C = "warning", "critical"


# --------------------------------------------------------------------------
# Builders (fresh objects on every call). All results pass the real validator.
# --------------------------------------------------------------------------

def drive_ok(index, total, used):
    percent = round(used * 100 / total, 1) if total else 0.0
    return tv.drive(MOUNTS[index], total, used, total - used, float(percent))


def make_snapshot(*, memory=(1000, 500), drives=((1000, 100),)):
    """Valid snapshot with chosen (total, available) memory and (total, used) drives."""
    snap = tv.build_snapshot()
    total, available = memory
    snap["memory"]["total_bytes"]["value"] = total
    snap["memory"]["available_bytes"]["value"] = available
    snap["disk"]["drives"] = [drive_ok(i, t, u) for i, (t, u) in enumerate(drives)]
    return snap


def memory_error_snapshot():
    snap = make_snapshot()
    snap["memory"] = {"total_bytes": tv.failed_leaf("bytes", "Access is denied"),
                      "available_bytes": tv.failed_leaf("bytes", "Access is denied"),
                      "used_percent": tv.failed_leaf("percent", "Access is denied")}
    snap["errors"] = [tv.err("memory", "PermissionError", "Access is denied")]
    snap["collection_status"] = "partial"
    return snap


def degraded_drive_snapshot(*leaf_names):
    """Two drives; the second has the named leaves 'unavailable' (collector shape)."""
    snap = make_snapshot(drives=((1000, 100), (1000, 960)))
    drive = snap["disk"]["drives"][1]
    for name in leaf_names:
        drive[name] = tv.na("percent" if name == "used_percent" else "bytes")
    drive["status"] = "unavailable"
    snap["disk"].update(status="partial", reason="some items could not be measured")
    snap["collection_status"] = "partial"
    return snap


def finding_ids(report):
    return [(f.rule_id, f.resource_id) for f in report.findings]


def assert_valid_input(snapshot):
    assert validate_snapshot(snapshot).valid, "fixture must satisfy the real validator"
    return snapshot


# --------------------------------------------------------------------------
# 1. Invalid snapshots are rejected before any rule runs
# --------------------------------------------------------------------------

def invalid_critical_snapshot():
    """Memory and disk would both be critical, but the snapshot is invalid (CPU 140 %)."""
    snap = make_snapshot(memory=(1000, 10), drives=((1000, 990),))
    snap["cpu"]["utilization_percent"]["value"] = 140.0
    return snap


INVALID_INPUTS = {
    "none": lambda: None,
    "list": lambda: [],
    "string": lambda: "snapshot",
    "missing_field": lambda: tv.without("memory"),
    "unsupported_version": lambda: tv.mutated("schema_version", 2),
    "bad_timestamp": lambda: tv.mutated("observed_at", "yesterday"),
    "cpu_out_of_range": invalid_critical_snapshot,
    "numeric_string": lambda: tv.mutated("memory.total_bytes.value", "1000"),
    "contradictory_status": lambda: {**tv.build_snapshot(), "collection_status": "failed"},
}


@pytest.mark.parametrize("name", INVALID_INPUTS)
def test_invalid_snapshot_is_rejected_with_validation_issues(name):
    snap = INVALID_INPUTS[name]()
    expected = validate_snapshot(snap)
    assert not expected.valid

    report = diagnose_snapshot(snap)
    assert report.status == "rejected"
    assert report.validation_issues == tuple(expected.issues) and report.validation_issues
    assert report.findings == () and report.skipped_rules == ()
    assert report.rules_evaluated == 0 and report.rules_skipped == 0 and report.rules_applicable == 0
    assert report.profile is None
    assert report.snapshot_observed_at is None          # nothing from an invalid snapshot is trusted
    assert report.schema_version == 1
    assert report.policy_version == DEFAULT_POLICY.version


def test_no_rule_or_profile_code_runs_for_an_invalid_snapshot(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("diagnostic code ran for an invalid snapshot")

    for name in ("_evaluate_memory", "_evaluate_drive", "_build_profile", "_gather"):
        monkeypatch.setattr(diagnostics, name, forbidden)
    for make in INVALID_INPUTS.values():
        assert diagnose_snapshot(make()).status == "rejected"


def test_critical_looking_data_in_an_invalid_snapshot_produces_no_finding():
    report = diagnose_snapshot(invalid_critical_snapshot())
    assert report.findings == ()
    assert report.status == "rejected"          # and never "complete": no false all-clear either


def test_the_existing_validator_is_the_gate(monkeypatch):
    perfectly_fine = make_snapshot()
    issue = ValidationIssue("INCONSISTENT_VALUES", "cpu", "synthetic")
    monkeypatch.setattr(diagnostics, "validate_snapshot", lambda snapshot: ValidationResult(1, [issue]))
    report = diagnose_snapshot(perfectly_fine)
    assert report.status == "rejected" and report.validation_issues == (issue,)


def test_validation_runs_before_any_rule(monkeypatch):
    calls = []
    real = {name: getattr(diagnostics, name) for name in
            ("validate_snapshot", "_build_profile", "_evaluate_memory", "_evaluate_drive")}
    for name, function in real.items():
        def spy(*args, _name=name, _function=function, **kwargs):
            calls.append(_name)
            return _function(*args, **kwargs)
        monkeypatch.setattr(diagnostics, name, spy)
    diagnose_snapshot(make_snapshot())
    assert calls[0] == "validate_snapshot"
    assert calls[1:] == ["_build_profile", "_evaluate_memory", "_evaluate_drive"]


# --------------------------------------------------------------------------
# 2. Memory thresholds: below, exactly at, above each boundary
# --------------------------------------------------------------------------

@pytest.mark.parametrize("available,expected", [
    (1000, None), (500, None), (101, None),     # above the warning boundary (10 %)
    (100, RULE_MEMORY_WARNING),                 # exactly at 10 %
    (99, RULE_MEMORY_WARNING),                  # immediately below 10 %
    (51, RULE_MEMORY_WARNING),                  # immediately above the critical boundary (5 %)
    (50, RULE_MEMORY_CRITICAL),                 # exactly at 5 %
    (49, RULE_MEMORY_CRITICAL),                 # immediately below 5 %
    (1, RULE_MEMORY_CRITICAL), (0, RULE_MEMORY_CRITICAL),
])
def test_memory_thresholds_with_default_policy(available, expected):
    report = diagnose_snapshot(make_snapshot(memory=(1000, available)))
    assert [f.rule_id for f in report.findings] == ([expected] if expected else [])
    assert report.status == "complete" and report.rules_evaluated == 4


@pytest.mark.parametrize("total,available,expected", [
    (10 ** 12, 10 ** 11 + 1, None), (10 ** 12, 10 ** 11, RULE_MEMORY_WARNING),
    (10 ** 12, 10 ** 11 - 1, RULE_MEMORY_WARNING),
    (10 ** 12, 5 * 10 ** 10 + 1, RULE_MEMORY_WARNING), (10 ** 12, 5 * 10 ** 10, RULE_MEMORY_CRITICAL),
    (10 ** 12, 5 * 10 ** 10 - 1, RULE_MEMORY_CRITICAL),
    (16 * GIB, 1717986919, None),               # 16 GiB: 10 % is 1717986918.4 bytes
    (16 * GIB, 1717986918, RULE_MEMORY_WARNING),
    (16 * GIB, 858993460, RULE_MEMORY_WARNING),  # 5 % is 858993459.2 bytes
    (16 * GIB, 858993459, RULE_MEMORY_CRITICAL),
])
def test_memory_thresholds_with_realistic_byte_counts(total, available, expected):
    report = diagnose_snapshot(make_snapshot(memory=(total, available)))
    assert [f.rule_id for f in report.findings] == ([expected] if expected else [])


def test_memory_rule_uses_available_not_the_reported_used_percent():
    snap = make_snapshot(memory=(1000, 40))
    snap["memory"]["used_percent"]["value"] = 5.0       # psutil figure deliberately contradicts
    assert [f.rule_id for f in diagnose_snapshot(snap).findings] == [RULE_MEMORY_CRITICAL]
    snap = make_snapshot(memory=(1000, 900))
    snap["memory"]["used_percent"]["value"] = 99.0
    assert diagnose_snapshot(snap).findings == ()


# --------------------------------------------------------------------------
# 3. Disk thresholds: below, exactly at, above each boundary
# --------------------------------------------------------------------------

@pytest.mark.parametrize("used,expected", [
    (0, None), (500, None), (899, None),        # immediately below 90 %
    (900, RULE_DISK_WARNING),                   # exactly at 90 %
    (901, RULE_DISK_WARNING),                   # immediately above 90 %
    (949, RULE_DISK_WARNING),                   # immediately below 95 %
    (950, RULE_DISK_CRITICAL),                  # exactly at 95 %
    (951, RULE_DISK_CRITICAL), (1000, RULE_DISK_CRITICAL),
])
def test_disk_thresholds_with_default_policy(used, expected):
    report = diagnose_snapshot(make_snapshot(drives=((1000, used),)))
    assert [f.rule_id for f in report.findings] == ([expected] if expected else [])
    assert report.status == "complete" and report.rules_evaluated == 4


@pytest.mark.parametrize("total,used,expected", [
    (10 ** 12, 9 * 10 ** 11 - 1, None), (10 ** 12, 9 * 10 ** 11, RULE_DISK_WARNING),
    (10 ** 12, 95 * 10 ** 10 - 1, RULE_DISK_WARNING), (10 ** 12, 95 * 10 ** 10, RULE_DISK_CRITICAL),
    (500 * GIB, 450 * GIB - 1, None), (500 * GIB, 450 * GIB, RULE_DISK_WARNING),   # exactly 90 %
    (500 * GIB, 475 * GIB - 1, RULE_DISK_WARNING), (500 * GIB, 475 * GIB, RULE_DISK_CRITICAL),
])
def test_disk_thresholds_with_realistic_byte_counts(total, used, expected):
    report = diagnose_snapshot(make_snapshot(drives=((total, used),)))
    assert [f.rule_id for f in report.findings] == ([expected] if expected else [])


def test_disk_rule_uses_bytes_not_the_reported_used_percent():
    """psutil's used_percent is rounded and platform-defined; the rule ignores it."""
    snap = make_snapshot()
    snap["disk"]["drives"][0] = tv.drive("C:\\", 1000, 990, 10, 1.0)    # percent says 1 %
    assert [f.rule_id for f in diagnose_snapshot(snap).findings] == [RULE_DISK_CRITICAL]
    snap["disk"]["drives"][0] = tv.drive("C:\\", 1000, 100, 900, 99.0)  # percent says 99 %
    assert diagnose_snapshot(snap).findings == ()


def test_drive_with_unavailable_used_percent_but_good_bytes_is_still_evaluated():
    snap = tv.variant_unavailable_drive()
    report = diagnose_snapshot(snap)
    assert report.status == "complete" and report.skipped_rules == () and report.rules_evaluated == 6


# --------------------------------------------------------------------------
# Exact arithmetic (a float percentage misjudges these boundaries)
# --------------------------------------------------------------------------

def test_exact_boundary_where_float_percent_would_be_wrong_memory():
    assert 7 / 100 * 100 > 7.0                          # the trap: naive result is 7.000000000000001
    policy = DiagnosticPolicy(version="t", memory_warning_available_percent=7.0,
                              memory_critical_available_percent=1.0)
    at = diagnose_snapshot(make_snapshot(memory=(100, 7)), policy)
    above = diagnose_snapshot(make_snapshot(memory=(100, 8)), policy)
    assert [f.rule_id for f in at.findings] == [RULE_MEMORY_WARNING]
    assert above.findings == ()


def test_exact_boundary_where_float_percent_would_be_wrong_disk():
    assert 57 / 100 * 100 < 57.0                        # naive result is 56.99999999999999
    policy = DiagnosticPolicy(version="t", disk_warning_used_percent=57.0)
    at = diagnose_snapshot(make_snapshot(drives=((100, 57),)), policy)
    below = diagnose_snapshot(make_snapshot(drives=((100, 56),)), policy)
    assert [f.rule_id for f in at.findings] == [RULE_DISK_WARNING]
    assert below.findings == ()


@pytest.mark.parametrize("available,fires", [(75, True), (76, False), (74, True)])
def test_decimal_thresholds_mean_what_they_say(available, fires):
    policy = DiagnosticPolicy(version="t", memory_warning_available_percent=7.5,
                              memory_critical_available_percent=2.0)
    report = diagnose_snapshot(make_snapshot(memory=(1000, available)), policy)
    assert bool(report.findings) is fires


def test_decimal_thresholds_are_not_read_as_their_binary_float_approximation():
    # 0.3 is stored as 0.29999999999999998...: read literally, 3 of 1000 (0.3 %) would miss a "<= 0.3" rule.
    memory_policy = DiagnosticPolicy(version="t", memory_warning_available_percent=0.3,
                                     memory_critical_available_percent=0.1)
    assert [f.rule_id for f in diagnose_snapshot(make_snapshot(memory=(1000, 3)), memory_policy).findings] == [
        RULE_MEMORY_WARNING]
    assert diagnose_snapshot(make_snapshot(memory=(1000, 4)), memory_policy).findings == ()
    # 0.1 is stored as 0.1000000000000000055...: read literally, 1 of 1000 would miss a ">= 0.1" rule.
    disk_policy = DiagnosticPolicy(version="t", disk_warning_used_percent=0.1, disk_critical_used_percent=50.0)
    assert [f.rule_id for f in diagnose_snapshot(make_snapshot(drives=((1000, 1),)), disk_policy).findings] == [
        RULE_DISK_WARNING]
    assert diagnose_snapshot(make_snapshot(drives=((1000, 0),)), disk_policy).findings == ()


def test_observed_percentage_is_reported_unrounded():
    finding = diagnose_snapshot(make_snapshot(memory=(16 * GIB, GIB // 2))).findings[0]
    assert finding.evidence[0].observed_value == 3.125          # 0.5 GiB of 16 GiB, not 3.1


def test_threshold_zero_fires_only_at_exactly_zero_available():
    policy = DiagnosticPolicy(version="t", memory_warning_available_percent=0,
                              memory_critical_available_percent=0)
    assert diagnose_snapshot(make_snapshot(memory=(1000, 0)), policy).findings[0].severity == C
    assert diagnose_snapshot(make_snapshot(memory=(1000, 1)), policy).findings == ()


# --------------------------------------------------------------------------
# 4. Critical suppresses the duplicate warning for the same resource
# --------------------------------------------------------------------------

def test_critical_memory_suppresses_the_warning_for_memory():
    report = diagnose_snapshot(make_snapshot(memory=(1000, 30)))
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert (finding.rule_id, finding.severity) == (RULE_MEMORY_CRITICAL, SEVERITY_CRITICAL)
    assert finding.superseded_rule_ids == (RULE_MEMORY_WARNING,)
    assert RULE_MEMORY_WARNING not in [f.rule_id for f in report.findings]
    assert report.rules_evaluated == 4          # the warning was tested; its finding was superseded


def test_critical_disk_suppresses_the_warning_for_that_drive():
    report = diagnose_snapshot(make_snapshot(drives=((1000, 970),)))
    assert [(f.rule_id, f.superseded_rule_ids) for f in report.findings] == [
        (RULE_DISK_CRITICAL, (RULE_DISK_WARNING,))]


def test_warning_alone_does_not_claim_to_supersede_anything():
    report = diagnose_snapshot(make_snapshot(memory=(1000, 80), drives=((1000, 920),)))
    assert [f.severity for f in report.findings] == [W, W]
    assert all(f.superseded_rule_ids == () for f in report.findings)


def test_equal_critical_and_warning_thresholds_report_critical_only():
    policy = DiagnosticPolicy(version="t", disk_warning_used_percent=95.0, disk_critical_used_percent=95.0)
    report = diagnose_snapshot(make_snapshot(drives=((1000, 950),)), policy)
    assert [(f.rule_id, f.superseded_rule_ids) for f in report.findings] == [
        (RULE_DISK_CRITICAL, (RULE_DISK_WARNING,))]


# --------------------------------------------------------------------------
# 5. Multiple drives are evaluated independently
# --------------------------------------------------------------------------

MULTI = ((1000, 960), (1000, 910), (1000, 100), (1000, 950))     # C critical, D warning, E ok, F critical


def test_each_drive_gets_its_own_finding_in_snapshot_order():
    report = diagnose_snapshot(make_snapshot(drives=MULTI))
    assert finding_ids(report) == [(RULE_DISK_CRITICAL, "C:\\"), (RULE_DISK_WARNING, "D:\\"),
                                   (RULE_DISK_CRITICAL, "F:\\")]
    assert [f.resource_path for f in report.findings] == [
        "disk.drives[0]", "disk.drives[1]", "disk.drives[3]"]
    assert report.rules_evaluated == 2 + 4 * 2 and report.status == "complete"


def test_changing_one_drive_does_not_change_the_findings_of_the_others():
    base = diagnose_snapshot(make_snapshot(drives=MULTI))
    changed = diagnose_snapshot(make_snapshot(drives=((1000, 100),) + MULTI[1:]))
    assert finding_ids(changed) == finding_ids(base)[1:]
    assert changed.findings == base.findings[1:]


def test_one_unusable_drive_does_not_stop_the_other_drives():
    snap = make_snapshot(drives=MULTI)
    snap["disk"]["drives"][1] = tv.drive("D:\\", 1000, 910, 90, 91.0)
    snap = degraded_drive_snapshot("used_bytes")
    snap["disk"]["drives"][0] = drive_ok(0, 1000, 990)
    report = diagnose_snapshot(snap)
    assert finding_ids(report) == [(RULE_DISK_CRITICAL, "C:\\")]
    assert {s.resource_id for s in report.skipped_rules} == {"D:\\"}


def test_findings_are_ordered_memory_first_then_drives():
    report = diagnose_snapshot(make_snapshot(memory=(1000, 20), drives=MULTI))
    assert [f.resource_type for f in report.findings] == ["memory", "disk", "disk", "disk"]
    assert report.findings[0].resource_id == "memory"


# --------------------------------------------------------------------------
# 6. Unavailable measurements skip only the affected rules
# --------------------------------------------------------------------------

def test_unavailable_memory_skips_memory_rules_only():
    snap = make_snapshot(drives=((1000, 960),))
    snap["memory"]["available_bytes"] = tv.na("bytes")
    snap["memory"]["used_percent"] = tv.na("percent")
    snap["collection_status"] = "partial"
    report = diagnose_snapshot(assert_valid_input(snap))

    assert [(s.rule_id, s.resource_type, s.resource_id, s.reason_code, s.missing_paths)
            for s in report.skipped_rules] == [
        (RULE_MEMORY_CRITICAL, "memory", "memory", REASON_MEASUREMENT_UNAVAILABLE,
         ("memory.available_bytes.value",)),
        (RULE_MEMORY_WARNING, "memory", "memory", REASON_MEASUREMENT_UNAVAILABLE,
         ("memory.available_bytes.value",)),
    ]
    assert finding_ids(report) == [(RULE_DISK_CRITICAL, "C:\\")]    # unrelated rules still ran
    assert report.rules_evaluated == 2 and report.status == "partial"


def test_memory_total_unavailable_skips_memory_rules():
    snap = make_snapshot(memory=(1000, 10))
    snap["memory"]["total_bytes"] = tv.na("bytes")
    snap["collection_status"] = "partial"
    report = diagnose_snapshot(assert_valid_input(snap))
    assert report.findings == ()                     # not guessed from the available figure
    assert [s.missing_paths for s in report.skipped_rules] == [("memory.total_bytes.value",)] * 2
    assert report.profile.total_memory_bytes is None


def test_failed_memory_reading_is_a_skip_with_the_error_reason():
    report = diagnose_snapshot(assert_valid_input(memory_error_snapshot()))
    assert {s.reason_code for s in report.skipped_rules if s.resource_type == "memory"} == {
        REASON_MEASUREMENT_ERROR}
    assert report.rules_evaluated == 2               # the drive rules ran


def test_all_missing_inputs_are_listed_in_a_fixed_order():
    snap = make_snapshot()
    snap["memory"]["available_bytes"] = tv.failed_leaf("bytes", "boom")
    snap["memory"]["total_bytes"] = tv.na("bytes")
    snap["errors"] = [tv.err("memory.available_bytes", "OSError", "boom")]
    snap["collection_status"] = "partial"
    skip = diagnose_snapshot(assert_valid_input(snap)).skipped_rules[0]
    assert skip.missing_paths == ("memory.available_bytes.value", "memory.total_bytes.value")
    assert skip.reason_code == REASON_MEASUREMENT_ERROR      # taken from the first missing input


@pytest.mark.parametrize("leaves,missing", [
    (("used_bytes",), ("disk.drives[1].used_bytes.value",)),
    (("total_bytes",), ("disk.drives[1].total_bytes.value",)),
    (("used_bytes", "total_bytes"), ("disk.drives[1].used_bytes.value", "disk.drives[1].total_bytes.value")),
])
def test_unavailable_drive_measurement_skips_only_that_drive(leaves, missing):
    report = diagnose_snapshot(assert_valid_input(degraded_drive_snapshot(*leaves)))
    assert [(s.rule_id, s.resource_id, s.resource_path, s.reason_code, s.missing_paths)
            for s in report.skipped_rules] == [
        (RULE_DISK_CRITICAL, "D:\\", "disk.drives[1]", REASON_MEASUREMENT_UNAVAILABLE, missing),
        (RULE_DISK_WARNING, "D:\\", "disk.drives[1]", REASON_MEASUREMENT_UNAVAILABLE, missing),
    ]
    assert report.rules_evaluated == 4 and report.status == "partial"


def test_failed_drive_is_skipped_and_the_other_drive_is_judged():
    report = diagnose_snapshot(assert_valid_input(tv.variant_error_drive()))
    assert [(s.resource_id, s.reason_code) for s in report.skipped_rules] == [
        ("D:\\", REASON_MEASUREMENT_ERROR)] * 2
    assert report.rules_evaluated == 4 and report.status == "partial"


def test_drive_the_collector_chose_not_to_measure_is_a_skip():
    report = diagnose_snapshot(assert_valid_input(tv.variant_skipped_drive()))
    assert [(s.resource_id, s.reason_code) for s in report.skipped_rules] == [
        ("E:\\", REASON_NOT_COLLECTED)] * 2
    assert report.rules_evaluated == 6 and report.status == "partial"


def test_empty_drive_list_skips_both_disk_rules_at_resource_level():
    report = diagnose_snapshot(assert_valid_input(tv.variant_disk_enumeration_error()))
    assert [(s.rule_id, s.resource_type, s.resource_id, s.resource_path, s.reason_code)
            for s in report.skipped_rules] == [
        (RULE_DISK_CRITICAL, "disk", None, "disk.drives", REASON_NO_DRIVES),
        (RULE_DISK_WARNING, "disk", None, "disk.drives", REASON_NO_DRIVES)]
    assert "error" in report.skipped_rules[0].message      # the (validated) disk group status
    assert report.rules_evaluated == 2 and report.status == "partial"


def test_only_skipped_drives_means_no_disk_evidence_at_all():
    report = diagnose_snapshot(assert_valid_input(tv.variant_all_drives_skipped()))
    assert {s.reason_code for s in report.skipped_rules} == {REASON_NOT_COLLECTED}
    assert report.status == "partial"


def test_zero_byte_capacity_is_skipped_not_treated_as_zero_percent():
    report = diagnose_snapshot(assert_valid_input(tv.variant_boundaries()))
    assert [(s.resource_id, s.reason_code) for s in report.skipped_rules] == [
        ("D:\\", REASON_ZERO_CAPACITY)] * 2
    assert finding_ids(report) == [(RULE_DISK_CRITICAL, "C:\\")]    # C is exactly full
    memory_zero = diagnose_snapshot(make_snapshot(memory=(0, 0)))
    assert {s.reason_code for s in memory_zero.skipped_rules} == {REASON_ZERO_CAPACITY}
    assert memory_zero.findings == ()


@pytest.mark.parametrize("make", [tv.variant_partial, tv.variant_network_empty,
                                  tv.variant_interface_state_unknown, tv.variant_system_unavailable],
                         ids=["cpu_failed", "no_interfaces", "interface_state_unknown", "system_unavailable"])
def test_unrelated_unavailable_data_skips_nothing(make):
    report = diagnose_snapshot(assert_valid_input(make()))
    assert report.skipped_rules == () and report.rules_evaluated == 6 and report.status == "complete"


def test_skip_messages_never_repeat_snapshot_text():
    snap = tv.variant_error_drive()
    snap["disk"]["drives"][1]["mountpoint"] = "evil\nmount"
    snap["errors"] = [tv.err("disk.drive[evil]", "PermissionError", "SECRET-DETAIL")]
    for leaf in ("total_bytes", "used_bytes", "free_bytes", "used_percent"):
        snap["disk"]["drives"][1][leaf]["reason"] = "SECRET-DETAIL"
    snap["disk"]["drives"][1]["reason"] = "SECRET-DETAIL"
    report = diagnose_snapshot(assert_valid_input(snap))
    for skip in report.skipped_rules:
        assert "SECRET" not in skip.message and "evil" not in skip.message
    assert report.skipped_rules[0].resource_id == "evil\nmount"       # kept as data, not prose


# --------------------------------------------------------------------------
# 7. Partial evidence never produces a false all-clear
# --------------------------------------------------------------------------

def test_no_findings_with_skipped_rules_is_partial_not_complete():
    report = diagnose_snapshot(assert_valid_input(tv.variant_error_drive()))
    assert report.findings == () and report.skipped_rules
    assert report.status == "partial"


def test_total_collection_failure_gives_partial_with_zero_rules_evaluated():
    report = diagnose_snapshot(assert_valid_input(tv.variant_failed()))
    assert report.findings == () and report.rules_evaluated == 0
    assert report.rules_skipped == 4 and report.status == "partial"


@pytest.mark.parametrize("name", tv.VALID_VARIANTS)
def test_status_and_counts_follow_the_documented_definitions_for_every_variant(name):
    snap = assert_valid_input(tv.VALID_VARIANTS[name]())
    report = diagnose_snapshot(snap)
    drives = len(snap["disk"]["drives"])
    assert report.rules_applicable == report.rules_evaluated + len(report.skipped_rules)
    assert report.rules_applicable == 2 + 2 * max(1, drives)
    assert report.status == ("partial" if report.skipped_rules else "complete")
    assert report.validation_issues == ()
    assert report.snapshot_observed_at == snap["observed_at"]
    assert ALL_RULE_IDS >= {s.rule_id for s in report.skipped_rules} | {f.rule_id for f in report.findings}


# Scenario -> (expected status, rules evaluated, rules skipped). Documented definitions:
#   rejected: invalid snapshot, nothing evaluated; partial: >= 1 rule instance skipped;
#   complete: nothing skipped. A rule instance is (rule, resource): 2 memory + 2 per drive.
COUNT_TABLE = {
    "healthy": (tv.build_snapshot, "complete", 6, 0),
    "memory_unavailable": (tv.variant_memory_unavailable, "partial", 4, 2),
    "error_drive": (tv.variant_error_drive, "partial", 4, 2),
    "skipped_drive": (tv.variant_skipped_drive, "partial", 6, 2),
    "all_drives_skipped": (tv.variant_all_drives_skipped, "partial", 2, 2),
    "enumeration_error": (tv.variant_disk_enumeration_error, "partial", 2, 2),
    "everything_failed": (tv.variant_failed, "partial", 0, 4),
    "zero_capacity_drive": (tv.variant_boundaries, "partial", 4, 2),
    "cpu_failed": (tv.variant_partial, "complete", 6, 0),
    "unavailable_used_percent_only": (tv.variant_unavailable_drive, "complete", 6, 0),
    "unrelated_values": (tv.variant_unrelated_values_not_enforced, "complete", 6, 0),
    "rejected": (invalid_critical_snapshot, "rejected", 0, 0),
}


@pytest.mark.parametrize("name", COUNT_TABLE)
def test_status_and_rule_counts_match_the_documented_table(name):
    make, status, evaluated, skipped = COUNT_TABLE[name]
    report = diagnose_snapshot(make())
    assert (report.status, report.rules_evaluated, report.rules_skipped) == (status, evaluated, skipped)


# --------------------------------------------------------------------------
# Scope: exactly four rules, no CPU rule
# --------------------------------------------------------------------------

def test_there_is_no_cpu_rule_and_cpu_readings_never_matter():
    base = diagnose_snapshot(make_snapshot())
    for value in (0.0, 99.9, 100.0):
        snap = make_snapshot()
        snap["cpu"]["utilization_percent"]["value"] = value
        assert diagnose_snapshot(snap) == base
    assert all("cpu" not in rule_id for rule_id in ALL_RULE_IDS)


def test_rule_ids_are_the_four_documented_ones():
    assert ALL_RULE_IDS == {"memory.available_low.warning", "memory.available_low.critical",
                            "disk.used_high.warning", "disk.used_high.critical"}


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------

def test_memory_finding_carries_structured_evidence():
    finding = diagnose_snapshot(make_snapshot(memory=(1000, 40))).findings[0]
    derived, available, total = finding.evidence
    assert derived == DiagnosticEvidence(
        "memory.available_percent_of_total", 4.0, "percent", 5.0, "<=",
        ("memory.available_bytes.value", "memory.total_bytes.value"))
    assert available == DiagnosticEvidence("memory.available_bytes.value", 40, "bytes")
    assert total == DiagnosticEvidence("memory.total_bytes.value", 1000, "bytes")
    assert (finding.resource_type, finding.resource_id, finding.resource_path) == ("memory", "memory", "memory")


def test_warning_evidence_uses_the_warning_threshold():
    finding = diagnose_snapshot(make_snapshot(memory=(1000, 80))).findings[0]
    assert (finding.evidence[0].threshold, finding.evidence[0].observed_value) == (10.0, 8.0)


def test_disk_finding_identifies_the_drive_and_its_evidence():
    finding = diagnose_snapshot(make_snapshot(drives=((1000, 100), (2000, 1900)))).findings[0]
    derived, used, total = finding.evidence
    assert (finding.resource_type, finding.resource_id, finding.resource_path) == (
        "disk", "D:\\", "disk.drives[1]")
    assert derived == DiagnosticEvidence(
        "disk.drives[1].used_percent_of_total", 95.0, "percent", 95.0, ">=",
        ("disk.drives[1].used_bytes.value", "disk.drives[1].total_bytes.value"))
    assert used == DiagnosticEvidence("disk.drives[1].used_bytes.value", 1900, "bytes")
    assert total == DiagnosticEvidence("disk.drives[1].total_bytes.value", 2000, "bytes")


def test_messages_are_hedged_and_never_repeat_snapshot_text():
    snap = make_snapshot(drives=((1000, 990),))
    snap["disk"]["drives"][0]["mountpoint"] = "weird\nname"
    for finding in diagnose_snapshot(snap).findings:
        assert "weird" not in finding.message
        assert "not proof" in finding.message
    assert diagnose_snapshot(snap).findings[0].resource_id == "weird\nname"


# --------------------------------------------------------------------------
# 8. Input is never modified; 9. determinism
# --------------------------------------------------------------------------

SAMPLES = {**tv.VALID_VARIANTS, "critical_everything": lambda: make_snapshot(memory=(1000, 10), drives=MULTI),
           "invalid": invalid_critical_snapshot}


@pytest.mark.parametrize("name", SAMPLES)
def test_input_snapshot_is_not_modified(name):
    snap = SAMPLES[name]()
    before, original = repr(snap), copy.deepcopy(snap)
    diagnose_snapshot(snap)
    assert repr(snap) == before and snap == original
    diagnose_snapshot(tv.freeze(SAMPLES[name]()))       # any mutation attempt raises AssertionError


@pytest.mark.parametrize("name", SAMPLES)
def test_repeated_evaluations_produce_equal_reports(name):
    snap = SAMPLES[name]()
    first = diagnose_snapshot(snap)
    assert diagnose_snapshot(snap) == first
    assert diagnose_snapshot(copy.deepcopy(snap)) == first
    assert diagnose_snapshot(tv.reordered(snap)) == first
    assert hash(diagnose_snapshot(snap)) == hash(first)


def test_no_state_leaks_between_calls():
    bad = diagnose_snapshot(make_snapshot(memory=(1000, 10), drives=MULTI))
    good = diagnose_snapshot(make_snapshot())
    assert bad.findings and good.findings == ()
    assert diagnose_snapshot(make_snapshot(memory=(1000, 10), drives=MULTI)) == bad


def test_report_is_json_serializable_by_a_future_presenter():
    report = diagnose_snapshot(make_snapshot(memory=(1000, 10), drives=MULTI))
    text = json.dumps(dataclasses.asdict(report), allow_nan=False)
    assert json.loads(text)["status"] == "complete"


# --------------------------------------------------------------------------
# 10. Policy: explicit, immutable, versioned, overridable without side effects
# --------------------------------------------------------------------------

def test_default_policy_values_and_version():
    assert DEFAULT_POLICY == DiagnosticPolicy("1", 10.0, 5.0, 90.0, 95.0)
    assert diagnose_snapshot(make_snapshot()).policy is DEFAULT_POLICY
    assert diagnose_snapshot(make_snapshot()).policy_version == "1"


def test_policy_is_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        DEFAULT_POLICY.disk_warning_used_percent = 50.0
    assert hash(DEFAULT_POLICY) == hash(DiagnosticPolicy())


def test_policy_override_changes_results_without_mutating_the_default():
    snap = make_snapshot(memory=(1000, 120), drives=((1000, 700),))
    assert diagnose_snapshot(snap).findings == ()
    custom = dataclasses.replace(DEFAULT_POLICY, version="custom-1",
                                 memory_warning_available_percent=15.0, disk_warning_used_percent=60.0,
                                 disk_critical_used_percent=80.0)
    report = diagnose_snapshot(snap, custom)
    assert [(f.rule_id, f.severity) for f in report.findings] == [
        (RULE_MEMORY_WARNING, W), (RULE_DISK_WARNING, W)]
    assert report.policy is custom and report.policy_version == "custom-1"
    assert report.findings[0].evidence[0].threshold == 15.0
    assert DEFAULT_POLICY == DiagnosticPolicy() and DEFAULT_POLICY.memory_warning_available_percent == 10.0
    assert diagnose_snapshot(snap).findings == ()          # default still behaves as before


def test_a_stricter_policy_can_escalate_to_critical():
    snap = make_snapshot(drives=((1000, 700),))
    policy = DiagnosticPolicy(version="strict", disk_warning_used_percent=50.0, disk_critical_used_percent=70.0)
    assert [f.rule_id for f in diagnose_snapshot(snap, policy).findings] == [RULE_DISK_CRITICAL]


def test_the_report_embeds_the_whole_policy_so_a_reused_label_cannot_hide_changes():
    sneaky = dataclasses.replace(DEFAULT_POLICY, disk_warning_used_percent=10.0)
    report = diagnose_snapshot(make_snapshot(), sneaky)
    assert report.policy_version == DEFAULT_POLICY.version
    assert report.policy.disk_warning_used_percent == 10.0 != DEFAULT_POLICY.disk_warning_used_percent


@pytest.mark.parametrize("field,value,error", [
    ("memory_warning_available_percent", True, TypeError),
    ("memory_warning_available_percent", "10", TypeError),
    ("memory_warning_available_percent", None, TypeError),
    ("disk_warning_used_percent", float("nan"), ValueError),
    ("disk_critical_used_percent", float("inf"), ValueError),
    ("memory_critical_available_percent", -0.1, ValueError),
    ("disk_warning_used_percent", 100.1, ValueError),
    ("memory_critical_available_percent", 11.0, ValueError),     # critical above warning
    ("disk_critical_used_percent", 89.0, ValueError),            # critical below warning
    ("version", "", ValueError), ("version", 1, ValueError),
])
def test_invalid_policy_is_rejected_at_construction(field, value, error):
    with pytest.raises(error):
        dataclasses.replace(DEFAULT_POLICY, **{field: value})


@pytest.mark.parametrize("policy", [{"disk_warning_used_percent": 50}, "default", 1, object()])
def test_a_non_policy_argument_is_a_caller_bug(policy):
    with pytest.raises(TypeError):
        diagnose_snapshot(make_snapshot(), policy)


# --------------------------------------------------------------------------
# Hardware profile
# --------------------------------------------------------------------------

def test_profile_of_a_complete_snapshot():
    profile = diagnose_snapshot(tv.build_snapshot()).profile
    assert profile == HardwareProfile(
        logical_cpu_count=8, logical_cpu_count_source="cpu.logical_cpu_count.value",
        total_memory_bytes=16 * GIB,
        drives=(DriveProfile("disk.drives[0]", "C:\\", 512 * GIB, 212 * GIB),
                DriveProfile("disk.drives[1]", "D:\\", 1024 * GIB, 924 * GIB)),
        completeness="complete", missing=())


def test_profile_falls_back_to_the_system_cpu_count_and_names_the_source():
    snap = tv.build_snapshot()
    snap["cpu"]["logical_cpu_count"] = tv.na("count")
    snap["collection_status"] = "partial"
    profile = diagnose_snapshot(assert_valid_input(snap)).profile
    assert (profile.logical_cpu_count, profile.logical_cpu_count_source) == (
        8, "system.logical_cpu_count.value")
    assert profile.completeness == "complete"


def test_unknown_cpu_count_stays_none_and_is_reported_missing():
    snap = tv.build_snapshot()
    snap["cpu"]["logical_cpu_count"] = tv.na("count")
    snap["system"]["logical_cpu_count"] = tv.na("count")
    snap["collection_status"] = "partial"
    profile = diagnose_snapshot(assert_valid_input(snap)).profile
    assert profile.logical_cpu_count is None and profile.logical_cpu_count_source is None
    assert profile.completeness == "partial" and profile.missing == ("cpu.logical_cpu_count.value",)


def test_unknown_memory_capacity_is_none_not_zero():
    profile = diagnose_snapshot(memory_error_snapshot()).profile
    assert profile.total_memory_bytes is None
    assert profile.missing == ("memory.total_bytes.value",)
    assert profile.completeness == "partial"


def test_failed_and_unmeasured_drives_keep_unknown_capacity_as_none():
    profile = diagnose_snapshot(tv.variant_error_drive()).profile
    assert profile.drives[0].total_bytes == 512 * GIB
    assert (profile.drives[1].total_bytes, profile.drives[1].free_bytes) == (None, None)
    assert profile.missing == ("disk.drives[1].total_bytes.value", "disk.drives[1].free_bytes.value")

    skipped = diagnose_snapshot(tv.variant_skipped_drive()).profile
    assert (skipped.drives[2].mountpoint, skipped.drives[2].total_bytes) == ("E:\\", None)
    assert skipped.completeness == "partial"


def test_no_reported_drives_is_a_missing_item():
    profile = diagnose_snapshot(tv.variant_disk_enumeration_error()).profile
    assert profile.drives == () and profile.missing == ("disk.drives",)
    assert profile.completeness == "partial"


def test_a_real_zero_capacity_is_kept_as_zero_and_is_distinct_from_unknown():
    profile = diagnose_snapshot(tv.variant_boundaries()).profile
    assert (profile.drives[1].total_bytes, profile.drives[1].free_bytes) == (0, 0)
    assert profile.completeness == "complete"


def test_profile_describes_capacity_and_does_not_decide_health():
    healthy = diagnose_snapshot(make_snapshot(memory=(1000, 900), drives=((1000, 100),)))
    stressed = diagnose_snapshot(make_snapshot(memory=(1000, 10), drives=((1000, 990),)))
    assert healthy.findings == () and stressed.findings
    assert healthy.profile.total_memory_bytes == stressed.profile.total_memory_bytes == 1000
    assert healthy.profile.drives[0].total_bytes == stressed.profile.drives[0].total_bytes == 1000
    assert {f.name for f in dataclasses.fields(HardwareProfile)} == {
        "logical_cpu_count", "logical_cpu_count_source", "total_memory_bytes", "drives",
        "completeness", "missing"}         # no severity / status / verdict fields


def test_profile_completeness_is_separate_from_evaluation_status():
    snap = tv.build_snapshot()
    snap["cpu"]["logical_cpu_count"] = tv.na("count")
    snap["system"]["logical_cpu_count"] = tv.na("count")
    snap["collection_status"] = "partial"
    report = diagnose_snapshot(snap)
    assert report.profile.completeness == "partial" and report.status == "complete"


# --------------------------------------------------------------------------
# Immutability of the data structures
# --------------------------------------------------------------------------

def assert_immutable_tree(obj):
    if dataclasses.is_dataclass(obj):
        for field in dataclasses.fields(obj):
            assert_immutable_tree(getattr(obj, field.name))
    elif isinstance(obj, tuple):
        for item in obj:
            assert_immutable_tree(item)
    else:
        assert type(obj) in (str, int, float, bool, type(None)), type(obj)


@pytest.mark.parametrize("name", SAMPLES)
def test_reports_contain_only_frozen_dataclasses_and_immutable_values(name):
    assert_immutable_tree(diagnose_snapshot(SAMPLES[name]()))


@pytest.mark.parametrize("instance,field", [
    (DEFAULT_POLICY, "version"),
    (DriveProfile("p", "C:\\", 1, 1), "total_bytes"),
    (diagnose_snapshot(make_snapshot()).profile, "total_memory_bytes"),
    (DiagnosticEvidence("m", 1, "bytes"), "observed_value"),
    (diagnose_snapshot(make_snapshot(memory=(1000, 10))).findings[0], "severity"),
    (SkippedRule("r", "memory", "memory", "memory", "X", (), "m"), "reason_code"),
    (diagnose_snapshot(make_snapshot()), "status"),
])
def test_every_result_type_is_frozen(instance, field):
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(instance, field, "changed")


def test_a_report_cannot_be_used_to_alter_the_snapshot_or_policy():
    snap = make_snapshot(memory=(1000, 10))
    report = diagnose_snapshot(snap)
    snap["memory"]["total_bytes"]["value"] = 5          # later edits to the snapshot ...
    assert report.profile.total_memory_bytes == 1000     # ... do not reach the report
    assert report.findings[0].evidence[2].observed_value == 1000


# --------------------------------------------------------------------------
# Errors: only the expected rule-level skip is handled
# --------------------------------------------------------------------------

def test_programming_errors_in_a_rule_are_not_turned_into_an_all_clear(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("bug in a rule")
    monkeypatch.setattr(diagnostics, "_evaluate_memory", broken)
    with pytest.raises(RuntimeError, match="bug in a rule"):
        diagnose_snapshot(make_snapshot())


@pytest.mark.parametrize("error", [KeyError("x"), TypeError("x"), ZeroDivisionError("x"), AttributeError("x")])
def test_unexpected_exceptions_inside_input_handling_propagate(monkeypatch, error):
    def broken(*args, **kwargs):
        raise error
    monkeypatch.setattr(diagnostics, "_gather", broken)
    with pytest.raises(type(error)):
        diagnose_snapshot(make_snapshot())


def test_only_the_private_skip_exception_is_caught():
    tree = ast.parse(Path(diagnostics.__file__).read_text(encoding="utf-8"))
    handlers = [ast.unparse(node.type) for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)]
    assert handlers == ["_RuleSkip", "_RuleSkip"]


# --------------------------------------------------------------------------
# 11. Safety: no OS queries, I/O, subprocess, network, clock or randomness
# --------------------------------------------------------------------------

def test_diagnosis_performs_no_io_subprocess_or_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("forbidden side effect attempted during diagnosis")

    snapshots = [make() for make in SAMPLES.values()]
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", blocked)
        patch.setattr(socket.socket, "connect", blocked)
        patch.setattr(socket.socket, "connect_ex", blocked)
        patch.setattr(socket, "create_connection", blocked)
        patch.setattr(socket, "getaddrinfo", blocked)
        patch.setattr(subprocess, "Popen", blocked)
        patch.setattr(os, "system", blocked)
        for snap in snapshots:
            assert isinstance(diagnose_snapshot(snap), DiagnosticReport)


SOURCE = Path(diagnostics.__file__).read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
ALLOWED_IMPORTS = {"__future__", "dataclasses", "fractions", "math", "types", "validator"}
FORBIDDEN_CALLS = {"open", "eval", "exec", "compile", "__import__", "input", "print", "getattr",
                   "setattr", "delattr", "globals", "locals", "vars"}


def test_static_imports_are_allowlisted_and_exclude_collection_clock_and_randomness():
    imported = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= ALLOWED_IMPORTS, imported - ALLOWED_IMPORTS
    assert not imported & {"collector", "psutil", "platform", "os", "subprocess", "socket", "time",
                           "datetime", "random", "sqlite3", "urllib", "http", "json", "pathlib"}


def test_static_only_math_isfinite_is_used_from_modules():
    used = {(node.value.id, node.attr) for node in ast.walk(TREE)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id in {"math", "os", "time", "random", "datetime"}}
    assert used == {("math", "isfinite")}


def test_static_no_dynamic_execution_or_file_builtins():
    called = {node.func.id for node in ast.walk(TREE)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert not called & FORBIDDEN_CALLS, called & FORBIDDEN_CALLS


def test_static_no_module_level_mutable_state():
    mutable = [name for name, value in vars(diagnostics).items()
               if not name.startswith("__") and isinstance(value, (list, dict, set, bytearray))]
    assert mutable == []


# --------------------------------------------------------------------------
# Real collector output (psutil faked) flows through validation and the engine
# --------------------------------------------------------------------------

@pytest.fixture
def tc():
    return pytest.importorskip("test_collector")     # needs psutil, a runtime requirement


def test_real_collector_output_healthy_machine(tc, monkeypatch):
    report = diagnose_snapshot(tc.scan(monkeypatch))
    assert report.status == "complete" and report.findings == () and report.rules_evaluated == 6
    assert report.profile.total_memory_bytes == 16 * GIB and report.profile.logical_cpu_count == 8
    assert [d.mountpoint for d in report.profile.drives] == ["C:\\", "D:\\"]


def test_real_collector_output_low_memory_and_full_drive(tc, monkeypatch):
    usage = {tc.C: NS(total=512 * GIB, used=490 * GIB, free=22 * GIB, percent=95.7),
             tc.D: NS(total=1024 * GIB, used=950 * GIB, free=74 * GIB, percent=92.8)}
    snapshot = tc.scan(
        monkeypatch,
        virtual_memory=lambda: NS(total=16 * GIB, available=GIB // 2, percent=96.9, free=1),
        disk_usage=lambda path: usage[path])
    report = diagnose_snapshot(snapshot)
    assert [(f.rule_id, f.resource_id) for f in report.findings] == [
        (RULE_MEMORY_CRITICAL, "memory"), (RULE_DISK_CRITICAL, "C:\\"), (RULE_DISK_WARNING, "D:\\")]
    assert report.status == "complete"


def test_real_collector_output_with_failures_is_partial_not_rejected(tc, monkeypatch):
    snapshot = tc.scan(monkeypatch, disk_usage=tc.boom(OSError("not ready")))
    report = diagnose_snapshot(snapshot)
    assert report.status == "partial" and report.rules_evaluated == 2
    assert {s.reason_code for s in report.skipped_rules} == {REASON_MEASUREMENT_ERROR}


def test_every_collector_scenario_is_accepted_and_counted_consistently(tc, monkeypatch):
    scenarios = {"complete": {}, "all_fail": dict(cpu_percent=tc.boom(), cpu_count=tc.boom(),
                                                   virtual_memory=tc.boom(), disk_partitions=tc.boom(),
                                                   net_if_stats=tc.boom()),
                 **tc.PARTIAL_CASES}
    for name, overrides in scenarios.items():
        snapshot = tc.scan(monkeypatch, **overrides)
        report = diagnose_snapshot(snapshot)
        assert report.status != "rejected", name
        assert report.validation_issues == (), name
        assert report.rules_applicable == 2 + 2 * max(1, len(snapshot["disk"]["drives"])), name


def test_real_psutil_output_is_diagnosable_on_this_host(tc):
    import collector
    report = diagnose_snapshot(collector.collect_system_snapshot(cpu_sample_interval=0.1))
    assert report.status in ("complete", "partial")          # no machine-specific values asserted
    assert report.validation_issues == ()


# What these tests do NOT prove:
# * Thresholds are heuristics: nothing here shows that a 10 % memory headroom or a
#   90 % full volume actually affects performance or drive health.
# * Fixtures follow the collector's schema as of this version; the real-collector tests
#   use faked psutil values, and the one real-psutil run asserts structure only. Nothing
#   was run on Windows 11.
# * The static and runtime side-effect checks cover only the paths they patch and the
#   source text of diagnostics.py; they do not trace every possible OS interaction.
