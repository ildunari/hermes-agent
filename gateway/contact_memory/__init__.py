"""Local, contact-scoped retrieval for trusted gateway routes.

The package deliberately exposes no model-selected namespace. Callers must build
``RetrievalScope`` from authenticated gateway routing metadata.
"""

from .broker import ContactMemoryBroker, RecallBundle, RetrievalScope
from .schema import RetrievalPrincipal

__all__ = [
    "ContactMemoryBroker",
    "RecallBundle",
    "RetrievalPrincipal",
    "RetrievalScope",
]
