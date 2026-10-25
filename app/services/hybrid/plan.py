"""The plan every answering layer produces -- see docs/assistant/HYBRID_ASSISTANT.md."""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Plan:
    """
    What to do for one message: tool calls (the agent's own tools), then how
    to word the answer. ``signature`` names the message's shape with the
    entities taken out ("want|<P>"), which is what the learner groups by.
    """

    steps: List[ToolCall]
    reply_kind: str
    layer: str
    rule: str
    signature: str
    language: Optional[str] = None
    confidence: float = 1.0
    # Learned patterns (L1) only: 'shadow' plans are compared, never answered with.
    pattern_status: Optional[str] = None

    def as_log(self) -> Dict[str, Any]:
        return {
            "steps": [asdict(s) for s in self.steps],
            "reply_kind": self.reply_kind,
            "rule": self.rule,
            "signature": self.signature,
        }

    @property
    def tool_names(self) -> List[str]:
        return [s.name for s in self.steps]
