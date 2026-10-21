"""Does everything the real collector can emit pass the validator?

test_validator.py is hermetic and uses hand-built snapshots. This file feeds the
validator actual output of collector.collect_system_snapshot() (with psutil and
platform faked, reusing helpers from test_collector.py) so the two modules
cannot silently drift apart. The reverse also matters: a validator that
rejects genuine collector output would be useless.

Needs psutil (a runtime requirement). No admin rights, internet or host changes.
"""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import psutil
import pytest

import collector
import test_collector as tc  # reuse fakes: install(), scan(), boom(), partition(), nic()
from validator import validate_snapshot

GIB = tc.GIB


def usage_zero(path):
    return NS(total=0, used=0, free=0, percent=0.0)


SCENARIOS = {
    "complete": {},
    **{f"partial:{name}": overrides for name, overrides in tc.PARTIAL_CASES.items()},
    "all_groups_fail": dict(cpu_percent=tc.boom(), cpu_count=tc.boom(), virtual_memory=tc.boom(),
                            disk_partitions=tc.boom(), net_if_stats=tc.boom()),
    "unexpected_error_in_cpu": dict(cpu_percent=tc.boom(TypeError("wrong signature"))),
    "bad_value_from_api": dict(virtual_memory=lambda: NS(total="not-a-number", available=1, percent=1.0)),
    "permission_error": dict(cpu_percent=tc.boom(PermissionError("Access is denied"))),
    "multiline_error_text": dict(cpu_percent=tc.boom(OSError("line1\nline2\t" + "x" * 1000))),
    "psutil_error": dict(virtual_memory=tc.boom(psutil.Error("sensor gone"))),
    "skipped_volumes": dict(disk_partitions=lambda all=False: [
        tc.partition("C:\\"),
        tc.partition("E:\\", fstype="", opts="cdrom"),
        tc.partition("Z:\\", fstype="NTFS", opts="rw,remote"),
        tc.partition("G:\\", fstype="", opts="rw,removable"),
    ]),
    "all_volumes_skipped": dict(disk_partitions=lambda all=False: [
        tc.partition("E:\\", fstype="", opts="cdrom")]),
    "all_volumes_fail": dict(disk_usage=tc.boom(OSError("not ready"))),
    "no_partitions": dict(disk_partitions=lambda all=False: []),
    "no_interfaces": dict(net_if_stats=lambda: {}),
    "unknown_link_speed": dict(net_if_stats=lambda: {"Ethernet": tc.nic(True, 0), "Odd": tc.nic(True, None)}),
    "interface_state_missing": dict(net_if_stats=lambda: {"A": tc.nic(True, 100), "B": tc.nic(None, 100)}),
    "zero_sized_volume": dict(disk_partitions=lambda all=False: [tc.partition("C:\\")],
                              disk_usage=usage_zero),
    "zero_cpu": dict(cpu_percent=lambda interval=None: 0.0),
    "missing_values": dict(
        platform_overrides={"machine": lambda: ""},
        cpu_count=lambda logical=True: None,
        virtual_memory=lambda: NS(total=16 * GIB, available=None, percent=None)),
}


@pytest.mark.parametrize("name", SCENARIOS)
def test_collector_output_passes_validation(monkeypatch, name):
    snapshot = tc.scan(monkeypatch, **SCENARIOS[name])
    result = validate_snapshot(snapshot)
    assert result.issues == [], [(i.code, i.path) for i in result.issues]
    assert result.valid is True
    assert result.schema_version == collector.SCHEMA_VERSION


@pytest.mark.parametrize("name", SCENARIOS)
def test_collector_output_passes_validation_after_json_round_trip(monkeypatch, name):
    snapshot = tc.scan(monkeypatch, **SCENARIOS[name])
    restored = json.loads(json.dumps(snapshot))
    assert validate_snapshot(restored).issues == []


@pytest.mark.parametrize("name,expected", [
    ("complete", "complete"), ("permission_error", "partial"), ("all_groups_fail", "failed"),
])
def test_validator_agrees_with_collector_on_collection_status(monkeypatch, name, expected):
    snapshot = tc.scan(monkeypatch, **SCENARIOS[name])
    assert snapshot["collection_status"] == expected
    assert validate_snapshot(snapshot).valid is True


def test_validator_catches_tampering_with_real_collector_output(monkeypatch):
    snapshot = tc.scan(monkeypatch)
    snapshot["cpu"]["utilization_percent"]["value"] = 140.0
    assert validate_snapshot(snapshot).valid is False

    snapshot = tc.scan(monkeypatch)
    snapshot["collection_status"] = "failed"
    assert validate_snapshot(snapshot).valid is False


def test_real_psutil_output_passes_validation():
    """No fakes: whatever this host's psutil reports must satisfy the contract."""
    snapshot = collector.collect_system_snapshot(cpu_sample_interval=0.1)
    result = validate_snapshot(snapshot)
    assert result.issues == [], [(i.code, i.path) for i in result.issues]
