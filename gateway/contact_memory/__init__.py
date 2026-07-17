"""Local, contact-scoped retrieval for trusted gateway routes.

The package deliberately exposes no model-selected namespace. Callers must build
``RetrievalScope`` from authenticated gateway routing metadata.
"""

from .broker import ContactMemoryBroker, RecallBundle, RetrievalScope
from .extractor import ExtractionJob, ExtractorBackend, PostTurnExtractionRuntime
from .schema import RetrievalPrincipal

__all__ = [
    "ContactMemoryBroker",
    "ExtractionJob",
    "ExtractorBackend",
    "PostTurnExtractionRuntime",
    "RecallBundle",
    "RetrievalPrincipal",
    "RetrievalScope",
]
