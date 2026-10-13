"""Self-learning (spec §17-19): memory & knowledge acquisition — procedures from repeated
evidence, lessons from failure, consolidation of raw activity into knowledge. Never retraining."""

from sali.learning.behavior import BehaviorProfile, BehaviorStore, classify_feedback
from sali.learning.candidates import LearningCandidateStore
from sali.learning.capability import CapabilityStore
from sali.learning.capability_acquisition import CapabilityAcquisitionStore
from sali.learning.daily import DailyConsolidation, DailySummary
from sali.learning.evidence import EvidenceLevel, derive_confidence, is_promotable
from sali.learning.experience import EvidenceState, ExperienceStore
from sali.learning.model import ConsolidationResult, LearnedProcedure
from sali.learning.service import LearningService
from sali.learning.skill_evolution import SkillProposalStore

__all__ = [
    "BehaviorProfile", "BehaviorStore", "CapabilityAcquisitionStore", "CapabilityStore",
    "ConsolidationResult", "DailyConsolidation", "DailySummary", "EvidenceLevel", "EvidenceState",
    "ExperienceStore", "LearnedProcedure", "LearningCandidateStore", "LearningService",
    "SkillProposalStore", "classify_feedback", "derive_confidence", "is_promotable",
]
