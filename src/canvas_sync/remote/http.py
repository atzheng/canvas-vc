"""The only module that talks HTTP (CONTRACTS.md section 7)."""

from __future__ import annotations

from typing import Any, Literal

import httpx


class CanvasClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        course_id: int,
        mode: Literal["read", "write"],
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url
        self.course_id = course_id
        self.mode = mode
        self._token = token
        self._transport = transport

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """JSON; follows Link pagination, returns the full list."""
        raise NotImplementedError

    def get_one(self, path: str, params: dict[str, Any] | None = None) -> Any:
        raise NotImplementedError

    def post(self, path: str, data: Any = None, json: Any = None) -> Any:
        raise NotImplementedError

    def put(self, path: str, data: Any = None, json: Any = None) -> Any:
        raise NotImplementedError

    def delete(self, path: str, params: dict[str, Any] | None = None) -> Any:
        raise NotImplementedError

    def upload_file(
        self,
        folder_path: str,
        name: str,
        content: bytes,
        on_duplicate: Literal["overwrite"] = "overwrite",
    ) -> dict[str, Any]:
        """3-step upload."""
        raise NotImplementedError

    def download(self, url: str) -> bytes:
        raise NotImplementedError
