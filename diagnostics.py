"""Deterministic, hardware-aware diagnostic engine (report schema 1, first version).

``diagnose_snapshot(snapshot, policy=None)`` validates a snapshot with
``validator.validate_snapshot`` and, only if it is valid, applies four fixed,
threshold-based rules. It is a pure function over in-memory data: no operating-
system queries, no file/network/subprocess/AI access, no clock, no randomness,
and the input snapshot is never modified.

What the rules mean
-------------------
They are heuristic *occupancy / headroom* indicators. Crossing a threshold is
not proof that the machine performs badly, and crossing none is not proof that
it is healthy: "no findings" means only "no rule condition was met by the
evidence that was available". There is deliberately no CPU rule: a single CPU
utilization sample cannot establish sustained pressure.

Rules (policy defaults in brackets; resource = what the rule is applied to)
----------------------------------------------------------------------------
* ``memory.available_low.warning``   available memory <= 10 % of total
* ``memory.available_low.critical``  available memory <=  5 % of total
* ``disk.used_high.warning``         drive used space  >= 90 % of total
* ``disk.used_high.critical``        drive used space  >= 95 % of total

Percentages are derived from integer byte counts and compared with exact
(``fractions.Fraction``) arithmetic, so a value exactly on a threshold is never
misjudged by floating-point rounding. For drives the reported ``used_percent``
is NOT used (psutil rounds it to one decimal and defines it per platform).
Capacity (the denominator) comes from the hardware profile; the current
measurement (the numerator) comes from the snapshot. The profile describes
capacity only; it never decides health on its own.

Critical beats warning: if both conditions hold for one resource, only the
critical finding is reported, and it lists the warning rule in
``superseded_rule_ids``. Each drive is evaluated independently.

Rule counts and evaluation status (exact definitions)
-----------------------------------------------------
A *rule instance* is one rule applied to one resource: 2 memory instances, plus
2 per reported drive (or 2 resource-level disk instances if no drive is
reported at all).

* ``rules_evaluated``: instances whose required measurements were all available
  and whose condition was actually tested. A warning that was tested but whose
  finding was superseded by a critical finding still counts as evaluated.
* ``skipped_rules``: instances that could not be tested, each with a stable
  reason code. ``rules_skipped == len(skipped_rules)`` and
  ``rules_applicable == rules_evaluated + rules_skipped``.
* ``status``:
    - ``"rejected"``: the snapshot failed validation. No rule ran:
      ``rules_evaluated == 0``, no findings, no skips, no profile, and
      ``snapshot_observed_at`` is ``None`` (nothing in an invalid snapshot is
      trusted, not even its timestamp).
    - ``"partial"``: the snapshot is valid but at least one instance was skipped,
      so an empty ``findings`` tuple must not be read as an all-clear.
    - ``"complete"``: valid and nothing skipped.
  Status describes rule coverage only; it says nothing about profile
  completeness (``profile.completeness`` reports that separately).

Skip reasons: ``MEASUREMENT_UNAVAILABLE`` / ``MEASUREMENT_ERROR`` /
``NOT_COLLECTED`` (a required measurement had that status in the snapshot),
``ZERO_CAPACITY`` (total is 0 bytes, so a percentage is undefined) and
``NO_DRIVES_REPORTED``. Skip messages never repeat free text from the snapshot.

Errors
------
Only one rule-level failure is expected, and it is modelled explicitly: a rule
whose input is missing raises the private ``_RuleSkip``, which the runner turns
into a ``SkippedRule``. Nothing else is caught, so a programming error is loud
instead of becoming an all-clear report. A caller bug (a ``policy`` that is not
a ``DiagnosticPolicy``, or an invalid threshold) raises ``TypeError`` /
``ValueError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import math

from validator import ValidationIssue, validate_snapshot

__all__ = [
    "REPORT_SCHEMA_VERSION", "DEFAULT_POLICY_VERSION", "DEFAULT_POLICY",
    "STATUS_COMPLETE", "STATUS_PARTIAL", "STATUS_REJECTED",
    "SEVERITY_WARNING", "SEVERITY_CRITICAL",
    "RULE_MEMORY_WARNING", "RULE_MEMORY_CRITICAL", "RULE_DISK_WARNING", "RULE_DISK_CRITICAL",
    "REASON_MEASUREMENT_UNAVAILABLE", "REASON_MEASUREMENT_ERROR", "REASON_NOT_COLLECTED",
    "REASON_ZERO_CAPACITY", "REASON_NO_DRIVES",
    "DiagnosticPolicy", "DriveProfile", "HardwareProfile", "DiagnosticEvidence",
    "DiagnosticFinding", "SkippedRule", "DiagnosticReport", "diagnose_snapshot",
]

REPORT_SCHEMA_VERSION = 1
DEFAULT_POLICY_VERSION = "1"

STATUS_COMPLETE = "complete"
STATUS_PARTIAL = "partial"
STATUS_REJECTED = "rejected"

SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

RULE_MEMORY_WARNING = "memory.available_low.warning"
RULE_MEMORY_CRITICAL = "memory.available_low.critical"
RULE_DISK_WARNING = "disk.used_high.warning"
RULE_DISK_CRITICAL = "disk.used_high.critical"

REASON_MEASUREMENT_UNAVAILABLE = "MEASUREMENT_UNAVAILABLE"
REASON_MEASUREMENT_ERROR = "MEASUREMENT_ERROR"
REASON_NOT_COLLECTED = "NOT_COLLECTED"
REASON_ZERO_CAPACITY = "ZERO_CAPACITY"
REASON_NO_DRIVES = "NO_DRIVES_REPORTED"

_REASON_BY_LEAF_STATUS = {
    "unavailable": REASON_MEASUREMENT_UNAVAILABLE,
    "error": REASON_MEASUREMENT_ERROR,
    "not_collected": REASON_NOT_COLLECTED,
}


# --------------------------------------------------------------------------
# Policy (explicit, immutable, versioned; never tuned at run time)
# --------------------------------------------------------------------------

def _check_percentage(name: str, value: object) -> None:
    if type(value) not in (int, float):  # also rejects bool
        raise TypeError(f"{name} must be an int or float percentage")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if not 0 <= value <= 100:
        raise ValueError(f"{name} must be between 0 and 100 inclusive")


@dataclass(frozen=True)
class DiagnosticPolicy:
    """Thresholds (percent) for the four rules, plus a version label.

    ``version`` labels the threshold set. The report embeds the whole policy, so
    thresholds are always visible even if an override reuses a label. Make an
    override with ``dataclasses.replace(DEFAULT_POLICY, version="custom-1",
    memory_warning_available_percent=15.0)``; the default is never modified.

    Memory rules fire when available memory is at or BELOW the threshold, disk
    rules when used space is at or ABOVE it. Critical thresholds must be at
    least as severe as the warning ones.
    """

    version: str = DEFAULT_POLICY_VERSION
    memory_warning_available_percent: float = 10.0
    memory_critical_available_percent: float = 5.0
    disk_warning_used_percent: float = 90.0
    disk_critical_used_percent: float = 95.0

    def __post_init__(self) -> None:
        if type(self.version) is not str or not self.version:
            raise ValueError("version must be a non-empty string")
        _check_percentage("memory_warning_available_percent", self.memory_warning_available_percent)
        _check_percentage("memory_critical_available_percent", self.memory_critical_available_percent)
        _check_percentage("disk_warning_used_percent", self.disk_warning_used_percent)
        _check_percentage("disk_critical_used_percent", self.disk_critical_used_percent)
        if self.memory_critical_available_percent > self.memory_warning_available_percent:
            raise ValueError("memory critical threshold must not exceed the warning threshold")
        if self.disk_critical_used_percent < self.disk_warning_used_percent:
            raise ValueError("disk critical threshold must not be below the warning threshold")


DEFAULT_POLICY = DiagnosticPolicy()


# --------------------------------------------------------------------------
# Report data structures (frozen dataclasses holding only immutable values)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class DriveProfile:
    """Capacity facts for one reported drive. ``None`` means unknown, never 0."""

    path: str                    # snapshot path, e.g. "disk.drives[1]"
    mountpoint: str
    total_bytes: int | None
    free_bytes: int | None


@dataclass(frozen=True)
class HardwareProfile:
    """Hardware capacity derived only from the validated snapshot.

    ``completeness`` is ``"complete"`` when nothing is missing and ``"partial"``
    otherwise; ``missing`` lists the snapshot paths of every unknown item.
    Unknown values stay ``None``. The profile describes capacity; it never
    decides whether the machine is healthy.
    """

    logical_cpu_count: int | None
    logical_cpu_count_source: str | None   # snapshot path the count came from
    total_memory_bytes: int | None
    drives: tuple[DriveProfile, ...]
    completeness: str
    missing: tuple[str, ...]


@dataclass(frozen=True)
class DiagnosticEvidence:
    """One observation supporting a finding.

    ``metric_path`` is a snapshot path for a raw measurement. For a derived
    value it is a stable derived name and ``inputs`` lists the snapshot paths it
    was computed from. ``threshold``/``comparison`` are set only on the value the
    rule actually compared (``"<="`` or ``">="``).
    """

    metric_path: str
    observed_value: int | float
    unit: str
    threshold: int | float | None = None
    comparison: str | None = None
    inputs: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiagnosticFinding:
    """A rule condition that was met. ``resource_id`` is the memory/drive identity
    (the drive's mountpoint is snapshot data and is not repeated in ``message``)."""

    rule_id: str
    severity: str
    resource_type: str
    resource_id: str
    resource_path: str
    message: str
    evidence: tuple[DiagnosticEvidence, ...]
    superseded_rule_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkippedRule:
    """A rule instance that could not be tested, and why."""

    rule_id: str
    resource_type: str
    resource_id: str | None
    resource_path: str
    reason_code: str
    missing_paths: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class DiagnosticReport:
    """Outcome of ``diagnose_snapshot``. See the module docstring for semantics."""

    schema_version: int
    snapshot_observed_at: str | None
    status: str
    policy_version: str
    policy: DiagnosticPolicy
    profile: HardwareProfile | None
    findings: tuple[DiagnosticFinding, ...]
    skipped_rules: tuple[SkippedRule, ...]
    rules_evaluated: int
    validation_issues: tuple[ValidationIssue, ...]

    @property
    def rules_skipped(self) -> int:
        return len(self.skipped_rules)

    @property
    def rules_applicable(self) -> int:
        return self.rules_evaluated + len(self.skipped_rules)


# --------------------------------------------------------------------------
# Hardware profile
# --------------------------------------------------------------------------

def _ok_value(leaf: dict) -> int | None:
    return leaf["value"] if leaf["status"] == "ok" else None


def _build_profile(snapshot: dict) -> HardwareProfile:
    """Derive the profile from a VALIDATED snapshot (direct indexing is safe)."""
    missing: list[str] = []

    cpu_count, cpu_source = None, None
    for path, leaf in (("cpu.logical_cpu_count.value", snapshot["cpu"]["logical_cpu_count"]),
                       ("system.logical_cpu_count.value", snapshot["system"]["logical_cpu_count"])):
        value = _ok_value(leaf)
        if value is not None:
            cpu_count, cpu_source = value, path
            break
    if cpu_count is None:
        missing.append("cpu.logical_cpu_count.value")

    total_memory = _ok_value(snapshot["memory"]["total_bytes"])
    if total_memory is None:
        missing.append("memory.total_bytes.value")

    drives = []
    for index, drive in enumerate(snapshot["disk"]["drives"]):
        path = f"disk.drives[{index}]"
        total, free = _ok_value(drive["total_bytes"]), _ok_value(drive["free_bytes"])
        if total is None:
            missing.append(f"{path}.total_bytes.value")
        if free is None:
            missing.append(f"{path}.free_bytes.value")
        drives.append(DriveProfile(path, drive["mountpoint"], total, free))
    if not drives:
        missing.append("disk.drives")

    return HardwareProfile(
        logical_cpu_count=cpu_count,
        logical_cpu_count_source=cpu_source,
        total_memory_bytes=total_memory,
        drives=tuple(drives),
        completeness="partial" if missing else "complete",
        missing=tuple(missing),
    )


# --------------------------------------------------------------------------
# Rule machinery
# --------------------------------------------------------------------------

class _RuleSkip(Exception):
    """The one expected rule-level failure: a required input cannot be used."""

    def __init__(self, reason_code: str, missing_paths: tuple[str, ...], message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.missing_paths = missing_paths
        self.message = message


@dataclass(frozen=True)
class _Family:
    """A critical/warning rule pair for one kind of resource."""

    resource_type: str
    critical_rule: str
    warning_rule: str
    comparison: str          # "<=" (value at or below) or ">=" (value at or above)
    derived_metric: str      # suffix for the derived-percentage evidence name
    critical_message: str
    warning_message: str


_MEMORY_FAMILY = _Family(
    "memory", RULE_MEMORY_CRITICAL, RULE_MEMORY_WARNING, "<=", "available_percent_of_total",
    "Available physical memory is at or below the critical headroom threshold relative to "
    "total memory. This is a heuristic headroom indicator, not proof of a performance problem.",
    "Available physical memory is at or below the warning headroom threshold relative to "
    "total memory. This is a heuristic headroom indicator, not proof of a performance problem.",
)
_DISK_FAMILY = _Family(
    "disk", RULE_DISK_CRITICAL, RULE_DISK_WARNING, ">=", "used_percent_of_total",
    "Used space on this logical volume is at or above the critical occupancy threshold. This is "
    "a heuristic occupancy indicator, not proof of a performance problem or of drive health.",
    "Used space on this logical volume is at or above the warning occupancy threshold. This is "
    "a heuristic occupancy indicator, not proof of a performance problem or of drive health.",
)


def _exact(threshold: int | float) -> Fraction:
    """Exact value of a policy threshold as written (0.1 means 1/10, not its binary float)."""
    return Fraction(threshold) if type(threshold) is int else Fraction(repr(threshold))


def _crossed(percent: Fraction, comparison: str, threshold: int | float) -> bool:
    limit = _exact(threshold)
    return percent <= limit if comparison == "<=" else percent >= limit


def _gather(*items: tuple[str, dict, int | None]) -> list[int]:
    """Collect required integer inputs: (snapshot path, leaf, already-known value).

    A known value (from the hardware profile) is used as is; otherwise the leaf must
    be "ok". Raises ``_RuleSkip`` naming every unusable input.
    """
    values: list[int] = []
    missing: list[tuple[str, str]] = []
    for path, leaf, known in items:
        if known is not None:
            values.append(known)
        elif leaf["status"] == "ok":
            values.append(leaf["value"])
        else:
            missing.append((path, leaf["status"]))
    if missing:
        detail = ", ".join(f"{path} ({status})" for path, status in missing)
        raise _RuleSkip(_REASON_BY_LEAF_STATUS[missing[0][1]],
                        tuple(path for path, _ in missing),
                        f"required measurement is not usable: {detail}")
    return values


def _require_capacity(total: int, path: str) -> None:
    if total == 0:
        raise _RuleSkip(REASON_ZERO_CAPACITY, (path,),
                        "total capacity is 0 bytes, so a percentage is undefined")


def _skips(family: _Family, resource_id: str | None, resource_path: str,
           skip: _RuleSkip) -> tuple[SkippedRule, SkippedRule]:
    return tuple(
        SkippedRule(rule_id, family.resource_type, resource_id, resource_path,
                    skip.reason_code, skip.missing_paths, skip.message)
        for rule_id in (family.critical_rule, family.warning_rule))


def _judge(family: _Family, policy_thresholds: tuple[int | float, int | float], *,
           resource_id: str, resource_path: str, percent: Fraction,
           derived_path: str, inputs: tuple[str, ...],
           raw_evidence: tuple[DiagnosticEvidence, ...]) -> tuple[DiagnosticFinding, ...]:
    """Apply the critical and warning thresholds; critical wins over warning."""
    critical_threshold, warning_threshold = policy_thresholds
    critical_hit = _crossed(percent, family.comparison, critical_threshold)
    warning_hit = _crossed(percent, family.comparison, warning_threshold)
    if not (critical_hit or warning_hit):
        return ()

    if critical_hit:
        rule_id, severity, threshold, message = (
            family.critical_rule, SEVERITY_CRITICAL, critical_threshold, family.critical_message)
        superseded = (family.warning_rule,) if warning_hit else ()
    else:
        rule_id, severity, threshold, message = (
            family.warning_rule, SEVERITY_WARNING, warning_threshold, family.warning_message)
        superseded = ()

    derived = DiagnosticEvidence(derived_path, float(percent), "percent",
                                 threshold, family.comparison, inputs)
    return (DiagnosticFinding(rule_id, severity, family.resource_type, resource_id,
                              resource_path, message, (derived, *raw_evidence), superseded),)


Outcome = tuple[tuple[DiagnosticFinding, ...], tuple[SkippedRule, ...], int]


def _evaluate_memory(snapshot: dict, profile: HardwareProfile, policy: DiagnosticPolicy) -> Outcome:
    memory = snapshot["memory"]
    available_path, total_path = "memory.available_bytes.value", "memory.total_bytes.value"
    try:
        available, total = _gather(
            (available_path, memory["available_bytes"], None),
            (total_path, memory["total_bytes"], profile.total_memory_bytes))
        _require_capacity(total, total_path)
    except _RuleSkip as skip:
        return (), _skips(_MEMORY_FAMILY, "memory", "memory", skip), 0

    findings = _judge(
        _MEMORY_FAMILY,
        (policy.memory_critical_available_percent, policy.memory_warning_available_percent),
        resource_id="memory", resource_path="memory",
        percent=Fraction(available * 100, total),
        derived_path=f"memory.{_MEMORY_FAMILY.derived_metric}",
        inputs=(available_path, total_path),
        raw_evidence=(DiagnosticEvidence(available_path, available, "bytes"),
                      DiagnosticEvidence(total_path, total, "bytes")))
    return findings, (), 2


def _evaluate_drive(index: int, drive: dict, drive_profile: DriveProfile,
                    policy: DiagnosticPolicy) -> Outcome:
    path = f"disk.drives[{index}]"
    used_path, total_path = f"{path}.used_bytes.value", f"{path}.total_bytes.value"
    mountpoint = drive["mountpoint"]
    try:
        used, total = _gather(
            (used_path, drive["used_bytes"], None),
            (total_path, drive["total_bytes"], drive_profile.total_bytes))
        _require_capacity(total, total_path)
    except _RuleSkip as skip:
        return (), _skips(_DISK_FAMILY, mountpoint, path, skip), 0

    findings = _judge(
        _DISK_FAMILY, (policy.disk_critical_used_percent, policy.disk_warning_used_percent),
        resource_id=mountpoint, resource_path=path,
        percent=Fraction(used * 100, total),
        derived_path=f"{path}.{_DISK_FAMILY.derived_metric}",
        inputs=(used_path, total_path),
        raw_evidence=(DiagnosticEvidence(used_path, used, "bytes"),
                      DiagnosticEvidence(total_path, total, "bytes")))
    return findings, (), 2


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def diagnose_snapshot(snapshot: object, policy: DiagnosticPolicy | None = None) -> DiagnosticReport:
    """Validate ``snapshot`` and, if valid, evaluate the rules under ``policy``.

    An invalid snapshot yields a ``"rejected"`` report carrying the validator's
    issues; no rule runs. Never modifies ``snapshot``. ``policy=None`` means
    ``DEFAULT_POLICY``. Raises ``TypeError`` if ``policy`` is not a
    ``DiagnosticPolicy`` (a caller bug); malformed snapshots never raise.
    """
    if policy is None:
        policy = DEFAULT_POLICY
    elif not isinstance(policy, DiagnosticPolicy):
        raise TypeError("policy must be a DiagnosticPolicy or None")

    validation = validate_snapshot(snapshot)
    if not validation.valid:
        return DiagnosticReport(
            schema_version=REPORT_SCHEMA_VERSION, snapshot_observed_at=None,
            status=STATUS_REJECTED, policy_version=policy.version, policy=policy, profile=None,
            findings=(), skipped_rules=(), rules_evaluated=0,
            validation_issues=tuple(validation.issues))

    profile = _build_profile(snapshot)
    findings: list[DiagnosticFinding] = []
    skipped: list[SkippedRule] = []
    evaluated = 0

    def collect(outcome: Outcome) -> None:
        nonlocal evaluated
        findings.extend(outcome[0])
        skipped.extend(outcome[1])
        evaluated += outcome[2]

    collect(_evaluate_memory(snapshot, profile, policy))
    drives = snapshot["disk"]["drives"]
    if drives:
        for index, drive in enumerate(drives):
            collect(_evaluate_drive(index, drive, profile.drives[index], policy))
    else:
        no_drives = _RuleSkip(
            REASON_NO_DRIVES, ("disk.drives",),
            f"no drives were reported (disk group status: {snapshot['disk']['status']})")
        skipped.extend(_skips(_DISK_FAMILY, None, "disk.drives", no_drives))

    return DiagnosticReport(
        schema_version=REPORT_SCHEMA_VERSION, snapshot_observed_at=snapshot["observed_at"],
        status=STATUS_PARTIAL if skipped else STATUS_COMPLETE,
        policy_version=policy.version, policy=policy, profile=profile,
        findings=tuple(findings), skipped_rules=tuple(skipped), rules_evaluated=evaluated,
        validation_issues=())
