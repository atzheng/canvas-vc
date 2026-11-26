"""FakeCanvas transport: routing, pagination, request log, course/sections/users/groups."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from tests.fake_canvas import assignments, discussion_topics, files, modules, pages

Handler = Callable[[httpx.Request], httpx.Response | None]


class FakeCanvas(httpx.BaseTransport):
    def __init__(self) -> None:
        self.log: list[httpx.Request] = []
        self.handlers: list[Handler] = []
        for module in (pages, discussion_topics, assignments, files, modules):
            module.register(self)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.log.append(request)
        for handler in self.handlers:
            response = handler(request)
            if response is not None:
                return response
        raise NotImplementedError(f"FakeCanvas: no handler for {request.method} {request.url.path}")
