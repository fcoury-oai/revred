"""Small, explicit data contracts shared by all review stages."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import hashlib
import re
from typing import Any


class EvidenceKind(str, Enum):
    OBSERVED = "observed"
    SOURCE_GROUNDED = "source_grounded"
    INFERRED = "inferred"
    HYPOTHETICAL = "hypothetical"
    UNKNOWN = "unknown"


class Assessment(str, Enum):
    CONFIRMED = "confirmed"
    PRE_EXISTING = "pre_existing"
    INTENTIONAL = "intentional"
    UNREACHABLE = "unreachable"
    SPECULATIVE = "speculative"
    DUPLICATE = "duplicate"
    NON_BLOCKING = "non_blocking"
    DISPROPORTIONATE = "disproportionate"
    HUMAN_REQUIRED = "human_required"


class Verdict(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    NON_BLOCKING = "non_blocking"
    HUMAN_REVIEW = "human_review"


def normalize_words(value: str) -> str:
    """Normalize finding labels without relying on unstable diff line numbers."""

    value = re.sub(r"\[P[0-3]\]", "", value, flags=re.IGNORECASE)
    value = re.sub(r"[^a-z0-9]+", " ", value.lower())
    return " ".join(value.split())


def stable_id(*parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return digest[:20]


@dataclass(frozen=True, slots=True)
class SourceAnchor:
    path: str
    line: int
    explanation: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SourceAnchor":
        return cls(
            path=str(value["path"]),
            line=int(value["line"]),
            explanation=str(value.get("explanation", "")),
        )


@dataclass(frozen=True, slots=True)
class Finding:
    title: str
    body: str
    path: str
    line_start: int
    line_end: int
    priority: int
    confidence: float = 0.0

    @property
    def finding_id(self) -> str:
        return stable_id(self.path, normalize_words(self.title))

    @property
    def priority_label(self) -> str:
        return f"P{self.priority}"

    def to_dict(self) -> dict[str, Any]:
        return {"finding_id": self.finding_id, **asdict(self)}


@dataclass(frozen=True, slots=True)
class Observation:
    finding_id: str
    changed_behavior: str
    reachable: str
    changed_from_base: str
    evidence_kind: EvidenceKind
    source_anchors: tuple[SourceAnchor, ...]
    realistic_trigger: str
    user_impact: str
    confidence: float
    uncertainties: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Observation":
        return cls(
            finding_id=str(value["finding_id"]),
            changed_behavior=str(value["changed_behavior"]),
            reachable=str(value["reachable"]),
            changed_from_base=str(value["changed_from_base"]),
            evidence_kind=EvidenceKind(value["evidence_kind"]),
            source_anchors=tuple(
                SourceAnchor.from_dict(anchor)
                for anchor in value["source_anchors"]
            ),
            realistic_trigger=str(value["realistic_trigger"]),
            user_impact=str(value["user_impact"]),
            confidence=float(value["confidence"]),
            uncertainties=tuple(str(item) for item in value["uncertainties"]),
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["evidence_kind"] = self.evidence_kind.value
        return result


@dataclass(frozen=True, slots=True)
class Challenge:
    finding_id: str
    assessment: Assessment
    root_cause: str
    rationale: str
    reachable: str
    changed_from_base: str
    impact: str
    evidence_kind: EvidenceKind
    source_anchors: tuple[SourceAnchor, ...]
    realistic_trigger: str
    user_impact: str
    impact_evidence_kind: EvidenceKind
    smallest_fix: str
    preserves_change_intent: bool
    confidence: float
    estimated_added_production_lines: int
    estimated_additional_production_files: int
    requires_new_dependency: bool
    requires_new_public_api: bool

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Challenge":
        return cls(
            finding_id=str(value["finding_id"]),
            assessment=Assessment(value["assessment"]),
            root_cause=str(value["root_cause"]),
            rationale=str(value["rationale"]),
            reachable=str(value["reachable"]),
            changed_from_base=str(value["changed_from_base"]),
            impact=str(value["impact"]),
            evidence_kind=EvidenceKind(value["evidence_kind"]),
            source_anchors=tuple(
                SourceAnchor.from_dict(anchor)
                for anchor in value["source_anchors"]
            ),
            realistic_trigger=str(value["realistic_trigger"]),
            user_impact=str(value["user_impact"]),
            impact_evidence_kind=EvidenceKind(value["impact_evidence_kind"]),
            smallest_fix=str(value["smallest_fix"]),
            preserves_change_intent=bool(value["preserves_change_intent"]),
            confidence=float(value["confidence"]),
            estimated_added_production_lines=int(
                value["estimated_added_production_lines"]
            ),
            estimated_additional_production_files=int(
                value["estimated_additional_production_files"]
            ),
            requires_new_dependency=bool(value["requires_new_dependency"]),
            requires_new_public_api=bool(value["requires_new_public_api"]),
        )

    @property
    def semantic_id(self) -> str:
        return stable_id(normalize_words(self.root_cause))

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["assessment"] = self.assessment.value
        result["evidence_kind"] = self.evidence_kind.value
        result["impact_evidence_kind"] = self.impact_evidence_kind.value
        result["semantic_id"] = self.semantic_id
        return result


@dataclass(frozen=True, slots=True)
class Decision:
    finding: Finding
    verdict: Verdict
    reason: str
    challenge: Challenge | None = None
    observation: Observation | None = None
    blocks_review: bool = False
    auto_fix_allowed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding": self.finding.to_dict(),
            "verdict": self.verdict.value,
            "reason": self.reason,
            "challenge": self.challenge.to_dict() if self.challenge else None,
            "observation": self.observation.to_dict() if self.observation else None,
            "blocks_review": self.blocks_review,
            "auto_fix_allowed": self.auto_fix_allowed,
        }


@dataclass(frozen=True, slots=True)
class Churn:
    production_added: int = 0
    production_deleted: int = 0
    production_files: tuple[str, ...] = ()
    test_added: int = 0
    test_deleted: int = 0
    test_files: tuple[str, ...] = ()
    other_added: int = 0
    other_deleted: int = 0
    other_files: tuple[str, ...] = ()
    dependency_files: tuple[str, ...] = ()
    public_api_additions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Snapshot:
    repo_root: str
    base_ref: str
    base_sha: str
    head_sha: str
    merge_base_sha: str
    patch_sha256: str
    changed_files: tuple[str, ...]
    dirty_paths: tuple[str, ...]
    untracked_paths: tuple[str, ...]
    original_churn: Churn = field(default_factory=Churn)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["original_churn"] = self.original_churn.to_dict()
        return result
