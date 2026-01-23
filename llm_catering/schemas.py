"""Schema helpers for LLM catering datasets."""

from __future__ import absolute_import

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class RawTicketRecord:
    """Represents one JSONL record from the audit exporter."""

    payload: Dict[str, Any]


@dataclass
class LLMTicketExample:
    """Normalized record for LLM use."""

    id: Optional[str]
    created: Optional[str]
    updated: Optional[str]
    sn: Optional[str]
    source_links: List[str] = field(default_factory=list)
    text: Dict[str, Any] = field(default_factory=dict)
    signals: Dict[str, Any] = field(default_factory=dict)
    labels: Dict[str, Optional[str]] = field(default_factory=dict)

    def to_dict(self):
        return {
            "id": self.id,
            "created": self.created,
            "updated": self.updated,
            "sn": self.sn,
            "source_links": self.source_links,
            "text": self.text,
            "signals": self.signals,
            "labels": self.labels,
        }
