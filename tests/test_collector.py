"""Contract and boundary tests for collector.py.

All behavior tests replace ``collector.psutil`` and ``collector.platform`` with
fakes, so they need no real hardware failures, admin rights, internet access or
changes to the host. An autouse fixture turns accidental network, subprocess,
os.system or file-write attempts into test failures.

What these tests do NOT prove about the read-only boundary: see the last
section of this file and the README.
"""

from __future__ import annotations

import ast
import builtins
import json
import math
import os
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import psutil
import pytest

import collector

GIB = 1024 ** 3
LEAF_STATUSES = {"ok", "unavailable", "error", "not_collected"}
GROUP_STATUSES = {"ok", "partial", "unavailable", "error"}
TOP_LEVEL_KEYS = ["schema_version", "observed_at", "collection_status", "system", "cpu",
                  "memory", "disk", "network", "errors"]

C = "C:\\"
D = "D:\\"
USAGE = {
    C: NS(total=512 * GIB, used=300 * GIB, free=212 * GIB, percent=58.6),
    D: NS(total=1024 * GIB, used=100 * GIB, free=924 * GIB, percent=9.8),
}


# --------------------------------------------------------------------------
# Fakes and helpers
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def forbid_side_effects(monkeypatch):
    """Tripwires: fail loudly if anything tries network, processes or file writes."""
    def blocked(*args, **kwargs):
        raise AssertionError("forbidden side effect attempted during a test")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(subprocess, "Popen", blocked)
    monkeypatch.setattr(os, "system", blocked)

    real_open = builtins.open

    def guarded_open(file, mode="r", *args, **kwargs):
        if any(flag in str(mode) for flag in "wax+"):
            raise AssertionError("file write attempted during a test")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)


def boom(exc=None):
    exc = exc or OSError("boom")

    def raiser(*args, **kwargs):
        raise exc
    return raiser


def partition(mountpoint, fstype="NTFS", opts="rw,fixed"):
    return NS(device=mountpoint, mountpoint=mountpoint, fstype=fstype, opts=opts)


def nic(isup=True, speed=1000):
    return NS(isup=isup, duplex=2, speed=speed, mtu=1500)


def install(monkeypatch, platform_overrides=None, **overrides):
    """Install fake psutil/platform into collector; return the psutil fake."""
    calls = {"cpu_percent": [], "disk_usage": []}

    def cpu_percent(interval=None):
        calls["cpu_percent"].append(interval)
        return 12.5

    def disk_usage(path):
        calls["disk_usage"].append(path)
        return USAGE[path]

    fake = NS(
        calls=calls,
        cpu_percent=cpu_percent,
        cpu_count=lambda logical=True: 8,
        # `free` is deliberately wrong-looking to catch free/available mix-ups.
        virtual_memory=lambda: NS(total=16 * GIB, available=6 * GIB, percent=62.5, free=1),
        disk_partitions=lambda all=False: [partition(C), partition(D)],
        disk_usage=disk_usage,
        net_if_stats=lambda: {"Wi-Fi": nic(True, 866), "Ethernet": nic(False, 0)},
    )
    for name, value in overrides.items():
        setattr(fake, name, value)

    fake_platform = NS(system=lambda: "Windows", release=lambda: "11",
                       version=lambda: "10.0.22631", machine=lambda: "AMD64")
    for name, value in (platform_overrides or {}).items():
        setattr(fake_platform, name, value)

    monkeypatch.setattr(collector, "psutil", fake)
    monkeypatch.setattr(collector, "platform", fake_platform)
    return fake


def scan(monkeypatch, platform_overrides=None, **overrides):
    install(monkeypatch, platform_overrides, **overrides)
    return collector.collect_system_snapshot(cpu_sample_interval=0.1)


def iter_leaves(node):
    if isinstance(node, dict):
        if "value" in node and "status" in node:
            yield node
            return
        for child in node.values():
            yield from iter_leaves(child)
    elif isinstance(node, list):
        for child in node:
            yield from iter_leaves(child)


def assert_contract(snap):
    """Invariants every snapshot must satisfy, however the scan went."""
    assert list(snap) == TOP_LEVEL_KEYS
    assert snap["schema_version"] == 1
    assert snap["collection_status"] in {"complete", "partial", "failed"}
    assert snap["disk"]["status"] in GROUP_STATUSES
    assert isinstance(snap["disk"]["drives"], list)
    assert snap["network"]["status"] in GROUP_STATUSES
    assert isinstance(snap["network"]["interfaces"], list)

    leaves = list(iter_leaves(snap))
    assert leaves
    for leaf in leaves:
        assert set(leaf) - {"reason"} == {"value", "unit", "status"}
        assert leaf["status"] in LEAF_STATUSES
        if leaf["status"] == "ok":
            assert leaf["value"] is not None
            assert "reason" not in leaf
        else:
            assert leaf["value"] is None
            assert isinstance(leaf["reason"], str) and leaf["reason"]

    for err in snap["errors"]:
        assert set(err) == {"component", "error_type", "message"}
        assert all(isinstance(v, str) and v for v in err.values())

    json.dumps(snap, allow_nan=False)


def assert_leaf(leaf, value, unit, status="ok"):
    assert leaf["value"] == value
    assert type(leaf["value"]) is type(value)
    assert leaf["unit"] == unit
    assert leaf["status"] == status


def drive(snap, mountpoint):
    return next(d for d in snap["disk"]["drives"] if d["mountpoint"] == mountpoint)


def iface(snap, name):
    return next(i for i in snap["network"]["interfaces"] if i["name"] == name)


# --------------------------------------------------------------------------
# 1. Schema, types, units
# --------------------------------------------------------------------------

def test_successful_snapshot_has_expected_schema(monkeypatch):
    snap = scan(monkeypatch)
    assert_contract(snap)
    assert snap["collection_status"] == "complete"
    assert snap["errors"] == []
    assert set(snap["system"]) == {"os_name", "os_release", "os_version", "machine",
                                   "logical_cpu_count"}
    assert set(snap["cpu"]) == {"utilization_percent", "logical_cpu_count"}
    assert set(snap["memory"]) == {"total_bytes", "available_bytes", "used_percent"}
    assert snap["disk"]["status"] == "ok"
    assert snap["network"]["status"] == "ok"
    assert all(leaf["status"] == "ok" for leaf in
               list(snap["system"].values()) + list(snap["cpu"].values())
               + list(snap["memory"].values()))


def test_result_is_plain_python_types(monkeypatch):
    allowed = (dict, list, str, int, float, bool, type(None))

    def walk(node):
        assert type(node) in allowed
        if isinstance(node, dict):
            assert all(type(k) is str for k in node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(scan(monkeypatch))


# --------------------------------------------------------------------------
# 2. Timestamp
# --------------------------------------------------------------------------

def test_timestamp_is_iso8601_utc(monkeypatch):
    install(monkeypatch)
    before = datetime.now(timezone.utc).replace(microsecond=0)
    snap = collector.collect_system_snapshot(cpu_sample_interval=0.1)
    after = datetime.now(timezone.utc)

    stamp = snap["observed_at"]
    assert stamp.endswith("Z")
    parsed = datetime.fromisoformat(stamp)
    assert parsed.utcoffset() == timedelta(0)
    assert before <= parsed <= after


# --------------------------------------------------------------------------
# 3. Mapping from mocked API results
# --------------------------------------------------------------------------

def test_system_and_cpu_mapping(monkeypatch):
    fake = install(monkeypatch)
    snap = collector.collect_system_snapshot(cpu_sample_interval=0.25)

    assert_leaf(snap["system"]["os_name"], "Windows", None)
    assert_leaf(snap["system"]["os_release"], "11", None)
    assert_leaf(snap["system"]["os_version"], "10.0.22631", None)
    assert_leaf(snap["system"]["machine"], "AMD64", None)
    assert_leaf(snap["system"]["logical_cpu_count"], 8, "count")
    assert_leaf(snap["cpu"]["utilization_percent"], 12.5, "percent")
    assert_leaf(snap["cpu"]["logical_cpu_count"], 8, "count")
    assert fake.calls["cpu_percent"] == [0.25]  # interval is forwarded, never 0/None


def test_memory_mapping_uses_available_not_free(monkeypatch):
    snap = scan(monkeypatch)
    assert_leaf(snap["memory"]["total_bytes"], 16 * GIB, "bytes")
    assert_leaf(snap["memory"]["available_bytes"], 6 * GIB, "bytes")  # not free (=1)
    assert_leaf(snap["memory"]["used_percent"], 62.5, "percent")


def test_disk_mapping(monkeypatch):
    snap = scan(monkeypatch)
    assert [d["mountpoint"] for d in snap["disk"]["drives"]] == [C, D]
    c = drive(snap, C)
    assert c["fstype"] == "NTFS" and c["status"] == "ok"
    assert_leaf(c["total_bytes"], 512 * GIB, "bytes")
    assert_leaf(c["used_bytes"], 300 * GIB, "bytes")
    assert_leaf(c["free_bytes"], 212 * GIB, "bytes")
    assert_leaf(c["used_percent"], 58.6, "percent")
    assert_leaf(drive(snap, D)["total_bytes"], 1024 * GIB, "bytes")


def test_network_mapping(monkeypatch):
    snap = scan(monkeypatch)
    names = [i["name"] for i in snap["network"]["interfaces"]]
    assert names == sorted(names) == ["Ethernet", "Wi-Fi"]
    wifi = iface(snap, "Wi-Fi")
    assert wifi["status"] == "ok"
    assert_leaf(wifi["is_up"], True, None)
    assert_leaf(wifi["link_speed_mbps"], 866, "Mbps")
    eth = iface(snap, "Ethernet")
    assert_leaf(eth["is_up"], False, None)  # "down" is a real value, not missing


# --------------------------------------------------------------------------
# Partition-selection policy
# --------------------------------------------------------------------------

def test_policy_skips_optical_network_and_medialess_volumes_without_touching_them(monkeypatch):
    parts = [
        partition(C),
        partition("E:\\", fstype="", opts="cdrom"),
        partition("F:\\", fstype="UDF", opts="ro,cdrom"),
        partition("Z:\\", fstype="NTFS", opts="rw,remote"),
        partition("G:\\", fstype="", opts="rw,removable"),
    ]
    fake = install(monkeypatch, disk_partitions=lambda all=False: parts)
    snap = collector.collect_system_snapshot(cpu_sample_interval=0.1)

    assert_contract(snap)
    assert fake.calls["disk_usage"] == [C]  # skipped volumes were never queried
    for mount in ("E:\\", "F:\\", "Z:\\", "G:\\"):
        d = drive(snap, mount)
        assert d["status"] == "not_collected" and d["reason"]
        for field in ("total_bytes", "used_bytes", "free_bytes", "used_percent"):
            assert d[field]["value"] is None and d[field]["status"] == "not_collected"
    assert snap["disk"]["status"] == "ok"
    assert snap["collection_status"] == "complete"  # policy skips don't degrade status
    assert snap["errors"] == []


def test_disk_partitions_requested_without_all_flag(monkeypatch):
    seen = []

    def partitions(all=False):
        seen.append(all)
        return [partition(C)]
    scan(monkeypatch, disk_partitions=partitions)
    assert seen == [False]


# --------------------------------------------------------------------------
# 4. CPU failure isolation
# --------------------------------------------------------------------------

def test_cpu_failure_does_not_block_other_groups(monkeypatch):
    snap = scan(monkeypatch, cpu_percent=boom(PermissionError("Access is denied")))
    assert_contract(snap)

    cpu = snap["cpu"]["utilization_percent"]
    assert cpu["value"] is None and cpu["status"] == "error" and cpu["unit"] == "percent"
    assert snap["cpu"]["logical_cpu_count"]["status"] == "ok"
    assert snap["memory"]["total_bytes"]["status"] == "ok"
    assert snap["disk"]["status"] == "ok"
    assert snap["network"]["status"] == "ok"
    assert snap["collection_status"] == "partial"
    assert snap["errors"] == [{"component": "cpu.utilization_percent",
                               "error_type": "PermissionError",
                               "message": "Access is denied"}]


def test_psutil_error_class_is_an_expected_failure(monkeypatch):
    snap = scan(monkeypatch, virtual_memory=boom(psutil.Error("sensor gone")))
    assert snap["memory"]["total_bytes"]["status"] == "error"
    assert snap["errors"][0]["error_type"] == "Error"
    assert snap["errors"][0]["component"] == "memory"
    assert snap["cpu"]["utilization_percent"]["status"] == "ok"


# --------------------------------------------------------------------------
# 5. Per-drive isolation
# --------------------------------------------------------------------------

def test_one_disk_failure_does_not_block_other_disk(monkeypatch):
    def usage(path):
        if path == D:
            raise PermissionError("Access is denied")
        return USAGE[path]

    snap = scan(monkeypatch, disk_usage=usage)
    assert_contract(snap)

    assert drive(snap, C)["status"] == "ok"
    assert_leaf(drive(snap, C)["free_bytes"], 212 * GIB, "bytes")
    bad = drive(snap, D)
    assert bad["status"] == "error" and bad["reason"] == "Access is denied"
    assert bad["total_bytes"]["value"] is None and bad["total_bytes"]["status"] == "error"
    assert snap["disk"]["status"] == "partial"
    assert snap["collection_status"] == "partial"
    assert [e["component"] for e in snap["errors"]] == ["disk.drive[D:\\]"]


def test_all_drives_failing_gives_disk_error(monkeypatch):
    snap = scan(monkeypatch, disk_usage=boom(OSError("not ready")))
    assert snap["disk"]["status"] == "error"
    assert len(snap["disk"]["drives"]) == 2
    assert snap["memory"]["total_bytes"]["status"] == "ok"
    assert snap["collection_status"] == "partial"


def test_partition_enumeration_failure_is_contained(monkeypatch):
    snap = scan(monkeypatch, disk_partitions=boom(OSError("enumeration failed")))
    assert_contract(snap)
    assert snap["disk"]["status"] == "error"
    assert snap["disk"]["drives"] == []
    assert snap["cpu"]["utilization_percent"]["status"] == "ok"
    assert snap["errors"][0]["component"] == "disk.partitions"


def test_no_partitions_is_unavailable(monkeypatch):
    snap = scan(monkeypatch, disk_partitions=lambda all=False: [])
    assert snap["disk"]["status"] == "unavailable"
    assert snap["disk"]["drives"] == []
    assert snap["errors"] == []  # nothing failed; there was simply nothing to measure
    assert snap["collection_status"] == "partial"


# --------------------------------------------------------------------------
# 6. Unavailable is null + status, never a fabricated/default zero
# --------------------------------------------------------------------------

def test_missing_values_are_null_with_status_not_zero(monkeypatch):
    snap = scan(
        monkeypatch,
        platform_overrides={"machine": lambda: ""},
        cpu_count=lambda logical=True: None,
        virtual_memory=lambda: NS(total=16 * GIB, available=None, percent=None),
    )
    assert_contract(snap)
    for leaf in (snap["system"]["machine"], snap["system"]["logical_cpu_count"],
                 snap["cpu"]["logical_cpu_count"], snap["memory"]["available_bytes"],
                 snap["memory"]["used_percent"]):
        assert leaf["value"] is None and leaf["status"] == "unavailable"
        assert leaf["value"] != 0
    assert snap["memory"]["total_bytes"]["status"] == "ok"
    assert snap["errors"] == []  # unavailable is not an error
    assert snap["collection_status"] == "partial"


def test_unknown_link_speed_is_null_not_zero(monkeypatch):
    snap = scan(monkeypatch, net_if_stats=lambda: {"Ethernet": nic(True, 0),
                                                   "Odd": nic(True, None)})
    for name in ("Ethernet", "Odd"):
        speed = iface(snap, name)["link_speed_mbps"]
        assert speed["value"] is None and speed["status"] == "unavailable"
        assert speed["unit"] == "Mbps"
        assert iface(snap, name)["is_up"]["value"] is True
    # Unknown speed alone does not degrade the network group or the scan.
    assert snap["network"]["status"] == "ok"
    assert snap["collection_status"] == "complete"


def test_genuine_zero_values_are_preserved_as_ok(monkeypatch):
    zero_usage = {C: NS(total=100 * GIB, used=100 * GIB, free=0, percent=100.0),
                  D: NS(total=GIB, used=0, free=GIB, percent=0.0)}
    snap = scan(monkeypatch, cpu_percent=lambda interval=None: 0.0,
                disk_usage=lambda p: zero_usage[p])
    assert_leaf(snap["cpu"]["utilization_percent"], 0.0, "percent")
    assert_leaf(drive(snap, C)["free_bytes"], 0, "bytes")
    assert_leaf(drive(snap, D)["used_bytes"], 0, "bytes")
    assert_leaf(drive(snap, D)["used_percent"], 0.0, "percent")
    assert snap["collection_status"] == "complete"


# --------------------------------------------------------------------------
# 7. Overall status rules
# --------------------------------------------------------------------------

def _usage_d_fails(path):
    if path == D:
        raise OSError("not ready")
    return USAGE[path]


PARTIAL_CASES = {
    "cpu_percent_error": dict(cpu_percent=boom()),
    "cpu_count_none": dict(cpu_count=lambda logical=True: None),
    "memory_error": dict(virtual_memory=boom()),
    "disk_enumeration_error": dict(disk_partitions=boom()),
    "one_drive_error": dict(disk_usage=_usage_d_fails),
    "network_error": dict(net_if_stats=boom()),
    "network_empty": dict(net_if_stats=lambda: {}),
    "interface_state_missing": dict(net_if_stats=lambda: {"A": nic(True, 100),
                                                          "B": nic(None, 100)}),
    "platform_value_missing": dict(platform_overrides={"machine": lambda: ""}),
}


@pytest.mark.parametrize("case", PARTIAL_CASES)
def test_status_partial_when_a_required_item_fails_or_is_missing(monkeypatch, case):
    snap = scan(monkeypatch, **PARTIAL_CASES[case])
    assert_contract(snap)
    assert snap["collection_status"] == "partial"


def test_status_complete_requires_everything_ok(monkeypatch):
    snap = scan(monkeypatch)
    assert snap["collection_status"] == "complete"


def test_status_failed_when_no_resource_measurement_succeeds(monkeypatch):
    snap = scan(monkeypatch, cpu_percent=boom(), cpu_count=boom(), virtual_memory=boom(),
                disk_partitions=boom(), net_if_stats=boom())
    assert_contract(snap)
    # OS metadata from the stdlib still succeeded, but it alone is not meaningful.
    assert snap["system"]["os_name"]["status"] == "ok"
    assert snap["collection_status"] == "failed"
    assert snap["disk"] == {"status": "error", "reason": "boom", "drives": []}
    assert snap["network"] == {"status": "error", "reason": "boom", "interfaces": []}
    assert {e["component"] for e in snap["errors"]} >= {
        "cpu.utilization_percent", "memory", "disk.partitions", "network.interfaces"}


def test_status_not_failed_if_only_one_group_works(monkeypatch):
    snap = scan(monkeypatch, cpu_percent=boom(), cpu_count=boom(), virtual_memory=boom(),
                disk_partitions=boom())  # network still works
    assert snap["collection_status"] == "partial"


# --------------------------------------------------------------------------
# Outer boundary: unexpected errors are recorded, not hidden
# --------------------------------------------------------------------------

def test_unexpected_error_in_group_is_recorded_and_isolated(monkeypatch):
    install(monkeypatch)
    monkeypatch.setattr(collector, "_collect_memory", boom(RuntimeError("bug")))
    snap = collector.collect_system_snapshot(cpu_sample_interval=0.1)

    assert_contract(snap)
    assert all(leaf["status"] == "error" for leaf in snap["memory"].values())
    assert snap["cpu"]["utilization_percent"]["status"] == "ok"
    assert snap["disk"]["status"] == "ok"
    assert snap["collection_status"] == "partial"
    assert snap["errors"] == [{"component": "memory", "error_type": "RuntimeError",
                               "message": "Unexpected internal error: bug"}]


def test_bad_api_value_is_not_silently_hidden_as_a_routine_error(monkeypatch):
    snap = scan(monkeypatch, virtual_memory=lambda: NS(total="not-a-number",
                                                       available=1, percent=1.0))
    assert_contract(snap)
    assert snap["errors"][0]["error_type"] == "ValueError"
    assert snap["errors"][0]["message"].startswith("Unexpected internal error")
    assert snap["memory"]["total_bytes"]["status"] == "error"
    assert snap["cpu"]["utilization_percent"]["status"] == "ok"


def test_unexpected_exception_inside_a_measurement_reaches_the_outer_boundary(monkeypatch):
    """A TypeError from an API call is not an expected OS/device failure.

    It must not be filed as a routine per-measurement error; it escapes to the
    group boundary, which records it as an unexpected internal error and marks
    the whole group (here: cpu) as errored while other groups survive.
    """
    snap = scan(monkeypatch, cpu_percent=boom(TypeError("wrong signature")))
    assert_contract(snap)
    assert snap["errors"] == [{"component": "cpu", "error_type": "TypeError",
                               "message": "Unexpected internal error: wrong signature"}]
    assert all(leaf["status"] == "error" for leaf in snap["cpu"].values())
    assert snap["memory"]["total_bytes"]["status"] == "ok"
    assert snap["collection_status"] == "partial"


def test_error_messages_are_single_line_and_bounded(monkeypatch):
    snap = scan(monkeypatch, cpu_percent=boom(OSError("line1\nline2\t" + "x" * 1000)))
    message = snap["errors"][0]["message"]
    assert "\n" not in message and "\t" not in message
    assert len(message) <= 200
    assert "Traceback" not in json.dumps(snap)


# --------------------------------------------------------------------------
# 8. Interface edge cases
# --------------------------------------------------------------------------

def test_empty_interface_list_is_unavailable_not_error(monkeypatch):
    snap = scan(monkeypatch, net_if_stats=lambda: {})
    assert snap["network"]["status"] == "unavailable"
    assert snap["network"]["interfaces"] == []
    assert snap["network"]["reason"]
    assert snap["errors"] == []


def test_interface_api_failure_is_error(monkeypatch):
    snap = scan(monkeypatch, net_if_stats=boom(OSError("no adapters access")))
    assert snap["network"]["status"] == "error"
    assert snap["network"]["interfaces"] == []
    assert snap["errors"][0]["component"] == "network.interfaces"
    assert snap["cpu"]["utilization_percent"]["status"] == "ok"


def test_interface_with_missing_state_is_partial(monkeypatch):
    snap = scan(monkeypatch, net_if_stats=lambda: {"A": nic(True, 100), "B": nic(None, 100)})
    assert snap["network"]["status"] == "partial"
    assert iface(snap, "A")["status"] == "ok"
    b = iface(snap, "B")
    assert b["status"] == "unavailable"
    assert b["is_up"]["value"] is None and b["is_up"]["status"] == "unavailable"


# --------------------------------------------------------------------------
# 9. JSON serialization
# --------------------------------------------------------------------------

@pytest.mark.parametrize("overrides", [
    {},
    dict(cpu_percent=boom()),
    dict(cpu_percent=boom(), cpu_count=boom(), virtual_memory=boom(),
         disk_partitions=boom(), net_if_stats=boom()),
    dict(net_if_stats=lambda: {}),
])
def test_snapshot_round_trips_through_json(monkeypatch, overrides):
    snap = scan(monkeypatch, **overrides)
    text = json.dumps(snap, allow_nan=False)
    assert json.loads(text) == snap


# --------------------------------------------------------------------------
# 10. No hidden state; no printing; interval validation
# --------------------------------------------------------------------------

def test_repeated_scans_do_not_share_state(monkeypatch):
    first = scan(monkeypatch, cpu_percent=boom(), net_if_stats=boom())
    assert first["errors"]
    second = scan(monkeypatch)
    assert second["collection_status"] == "complete"
    assert second["errors"] == []
    assert second["errors"] is not first["errors"]

    second["system"]["os_name"]["value"] = "mutated"
    third = scan(monkeypatch)
    assert third["system"]["os_name"]["value"] == "Windows"


def test_module_holds_no_mutable_global_containers():
    mutable = [name for name, value in vars(collector).items()
               if not name.startswith("__") and isinstance(value, (list, dict, set, bytearray))]
    assert mutable == []


def test_collect_does_not_print(monkeypatch, capsys):
    scan(monkeypatch)
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


@pytest.mark.parametrize("bad", [0, 0.0, -1, 0.05, 5.01, 60, math.nan, math.inf, True, "1", None])
def test_out_of_range_interval_is_rejected(monkeypatch, bad):
    install(monkeypatch)
    with pytest.raises(ValueError):
        collector.collect_system_snapshot(cpu_sample_interval=bad)


@pytest.mark.parametrize("ok", [0.1, 1, 5.0])
def test_in_range_interval_is_accepted_and_forwarded(monkeypatch, ok):
    fake = install(monkeypatch)
    collector.collect_system_snapshot(cpu_sample_interval=ok)
    assert fake.calls["cpu_percent"] == [ok]


def test_default_interval_is_the_documented_constant(monkeypatch):
    fake = install(monkeypatch)
    collector.collect_system_snapshot()
    assert fake.calls["cpu_percent"] == [collector.DEFAULT_CPU_SAMPLE_INTERVAL_S]


# --------------------------------------------------------------------------
# 11. Host independence + one real-API shape check
# --------------------------------------------------------------------------

def test_fakes_cover_every_api_the_collector_uses(monkeypatch):
    """A tripwire inside a tripwire: scanning with fakes must not touch real psutil."""
    install(monkeypatch)
    monkeypatch.setattr(psutil, "cpu_percent", boom(AssertionError("real psutil used")))
    monkeypatch.setattr(psutil, "virtual_memory", boom(AssertionError("real psutil used")))
    monkeypatch.setattr(psutil, "disk_usage", boom(AssertionError("real psutil used")))
    assert collector.collect_system_snapshot(cpu_sample_interval=0.1)["collection_status"] == "complete"


def test_real_psutil_shapes_match_what_the_collector_expects():
    """Structure-only smoke test against the real psutil on whatever host runs it.

    Asserts no machine values. Its purpose is to catch fakes drifting from real
    API shapes: a wrong attribute name would surface as an
    "Unexpected internal error" entry.
    """
    snap = collector.collect_system_snapshot(cpu_sample_interval=0.1)
    assert_contract(snap)
    unexpected = [e for e in snap["errors"] if e["message"].startswith("Unexpected internal error")]
    assert unexpected == []


# --------------------------------------------------------------------------
# Static read-only tripwires (NOT a proof; see note below)
# --------------------------------------------------------------------------

SOURCE = Path(collector.__file__).read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)

ALLOWED_IMPORTS = {"__future__", "datetime", "platform", "psutil", "typing"}
ALLOWED_ATTRIBUTES = {
    "psutil": {"cpu_percent", "cpu_count", "virtual_memory", "disk_partitions",
               "disk_usage", "net_if_stats", "Error"},
    "platform": {"system", "release", "version", "machine"},
    "datetime": {"now"},
    "timezone": {"utc"},
}
FORBIDDEN_CALLS = {"open", "eval", "exec", "compile", "__import__", "input"}


def test_static_imports_are_allowlisted():
    imported = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= ALLOWED_IMPORTS, imported - ALLOWED_IMPORTS


def test_static_only_allowlisted_psutil_and_stdlib_attributes_are_used():
    used = {}
    for node in ast.walk(TREE):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id in ALLOWED_ATTRIBUTES:
                used.setdefault(node.value.id, set()).add(node.attr)
    for module, attrs in used.items():
        assert attrs <= ALLOWED_ATTRIBUTES[module], (module, attrs - ALLOWED_ATTRIBUTES[module])
    # And the allowlist is actually exercised (guards against a vacuous test).
    assert used["psutil"] >= {"cpu_percent", "cpu_count", "virtual_memory",
                              "disk_partitions", "disk_usage", "net_if_stats"}


def test_static_no_dynamic_execution_or_file_builtins():
    called = {node.func.id for node in ast.walk(TREE)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert not (called & FORBIDDEN_CALLS)


# What the tests above do NOT prove:
# * The static checks only see this one file's source text. They cannot see
#   what psutil or the standard library do internally, nor code reached via
#   getattr tricks, third-party monkeypatching, or a later edit that also edits
#   the allowlists in this test.
# * The behavior tests run against fakes, so they show the collector's own
#   logic is side-effect-free, not that real psutil calls have no side effects.
# * The tripwires only cover the paths they patch (sockets, subprocess.Popen,
#   os.system, builtins.open write modes), not every possible OS interaction.
# * Nothing here was run on Windows 11 or checks Windows-specific psutil
#   behavior (for example how a hung volume or a removable drive responds).
