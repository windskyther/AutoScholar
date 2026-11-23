"""Research search providers and normalized result models."""

from autoscholar.research.models import SearchResponse, SearchResult, SourceType
from autoscholar.research.providers import (
    SearchProviderError,
    SemanticScholarSearchProvider,
    TavilySearchProvider,
)
from autoscholar.research.service import RedisResearchCache, ResearchSearchService

__all__ = [
    "RedisResearchCache",
    "ResearchSearchService",
    "SearchProviderError",
    "SearchResponse",
    "SearchResult",
    "SemanticScholarSearchProvider",
    "SourceType",
    "TavilySearchProvider",
]
