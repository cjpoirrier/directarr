from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

@dataclass
class Release:
    title: str
    url: str
    source: str
    size: Optional[int] = None
    publish_date: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    resolution: Optional[str] = None
    codec: Optional[str] = None

class SourceAdapter(ABC):
    @abstractmethod
    async def search(self, query: str) -> list[Release]:
        raise NotImplementedError
