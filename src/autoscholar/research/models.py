from dataclasses import asdict, dataclass
from typing import Any, Literal, cast

SourceType = Literal["web", "paper"]


@dataclass(frozen=True, slots=True)
class SearchResult:
    source_type: SourceType
    provider: str
    title: str
    url: str
    content: str
    authors: tuple[str, ...] = ()
    year: int | None = None
    external_id: str | None = None
    relevance: float | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["authors"] = list(self.authors)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SearchResult":
        return cls(
            source_type=cast(SourceType, payload["source_type"]),
            provider=str(payload["provider"]),
            title=str(payload["title"]),
            url=str(payload["url"]),
            content=str(payload["content"]),
            authors=tuple(str(item) for item in payload.get("authors", [])),
            year=int(payload["year"]) if payload.get("year") is not None else None,
            external_id=(
                str(payload["external_id"]) if payload.get("external_id") is not None else None
            ),
            relevance=(
                float(payload["relevance"]) if payload.get("relevance") is not None else None
            ),
        )


@dataclass(frozen=True, slots=True)
class SearchResponse:
    provider: str
    source_type: SourceType
    query: str
    results: tuple[SearchResult, ...]
    cache_hit: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "source_type": self.source_type,
            "query": self.query,
            "cache_hit": self.cache_hit,
            "results": [item.to_dict() for item in self.results],
        }
