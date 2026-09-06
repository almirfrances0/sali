"""Sali cognitive-cycle orchestration layer.

Ties the existing autonomy components (InitiativeEngine, ProactiveLoop, CommunicationDecisionEngine,
OpenLoopStore, CuriosityStore, ReflectionEngine, ...) into a periodic driver so the cycle
actually runs when Almir isn't around.

* `initiative_driver.InitiativeDriver` — periodic tick that calls `InitiativeEngine.generate_candidates`
  under a resource + next-wake gate. Was DORMANT before this turn.
* `decision_trace.DecisionTraceStore` — compact per-decision metadata (§29/§44), the substrate
  for the metrics endpoint and for measuring "is Sali improving?" (§37).
"""

from sali.cognitive.decision_trace import DecisionTraceStore, VALID_MODES
from sali.cognitive.initiative_driver import InitiativeDriver

__all__ = ["DecisionTraceStore", "InitiativeDriver", "VALID_MODES"]
