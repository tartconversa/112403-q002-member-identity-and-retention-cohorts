"""Governed omnichannel membership repurchase analytics.

The public API is intentionally small so analysts can either use the CLI or
embed the deterministic calculation in tests and notebooks.
"""

from .analysis import AnalysisResult, run_analysis
from .campaigns import CampaignService, evaluate_recipient
from .io import InputData, load_input
from .models import (
    ConsentEvent,
    IdentityLink,
    Order,
    RepurchaseStatus,
)

__version__ = "1.0.0"

__all__ = [
    "AnalysisResult",
    "CampaignService",
    "ConsentEvent",
    "IdentityLink",
    "InputData",
    "Order",
    "RepurchaseStatus",
    "evaluate_recipient",
    "load_input",
    "run_analysis",
]
