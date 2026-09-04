"""
Unified IM Hub - Adapter Base Contracts
Conforms strictly to docs/03-im-integration-v0.2.7.md
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from backend.app.im.models import IMCapabilities, IMIngestBatch, IMCommitReceipt, IMSourceStatus, IMMessageItem


class IMSourceAdapter(ABC):
    """Common base interface for all IM adapters (P1-IM-5)."""

    @property
    @abstractmethod
    def source(self) -> str:
        """'wechat' | 'wecom' | 'qq'"""
        pass

    @property
    @abstractmethod
    def capabilities(self) -> IMCapabilities:
        pass

    @abstractmethod
    async def get_status(self) -> IMSourceStatus:
        pass


class IMSourceReader(IMSourceAdapter):
    """Source reader for historical batch fetching."""

    @abstractmethod
    async def read_history(self, limit: int = 50, before_cursor: Optional[str] = None) -> List[IMMessageItem]:
        pass


class IMIngestSink(ABC):
    """Sink interface where IngestDriver commits normalized batches."""

    @abstractmethod
    async def commit(self, batch: IMIngestBatch) -> IMCommitReceipt:
        pass


class IMIngestDriver(IMSourceAdapter):
    """Driver interface for real-time or snapshot change ingestion."""

    @abstractmethod
    async def start(self, sink: IMIngestSink) -> None:
        pass

    @abstractmethod
    async def stop(self) -> None:
        pass
