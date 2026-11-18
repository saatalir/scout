from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlparse

FIXTURES = Path(__file__).parent / "fixtures" / "browser"
BASE = "https://pages.test"


def _int_param(query: dict[str, list[str]], key: str, default: int) -> int:
    values = query.get(key)
    if not values or values[0] == "":
        return default
    return int(values[0])


def _float_param(query: dict[str, list[str]], key: str, default: float) -> float:
    values = query.get(key)
    if not values or values[0] == "":
        return default
    return float(values[0])


def make_routes(behaviour: dict[str, Callable[[Any], Awaitable[None]]] | None = None):
    """Return an async setup(context) for BrowserRunner(setup=...).

    Routes BASE/** to tests/fixtures/browser/*.html. Query parameters:
      ?delay=<seconds>   wait before answering
      ?status=<code>     answer with that HTTP status
      ?flaky=<n>         abort the first n requests to this path, then serve it
    """
    flaky_counts: dict[str, int] = {}

    async def setup(context) -> None:
        async def serve(route) -> None:
            url = route.request.url
            parsed = urlparse(url)
            path = parsed.path or "/"
            query = parse_qs(parsed.query)

            if behaviour:
                handler = behaviour.get(path)
                if handler is None and path.endswith(".html"):
                    handler = behaviour.get(path[:-len(".html")])
                if handler is None:
                    handler = behaviour.get(path + ".html")
                if handler is not None:
                    await handler(route)
                    return

            flaky = _int_param(query, "flaky", 0)
            if flaky:
                count = flaky_counts.get(path, 0)
                if count < flaky:
                    flaky_counts[path] = count + 1
                    await route.abort("failed")
                    return

            delay = _float_param(query, "delay", 0.0)
            if delay:
                await asyncio.sleep(delay)

            status_values = query.get("status")
            status = int(status_values[0]) if status_values and status_values[0] != "" else None

            name = path.lstrip("/")
            if name.endswith(".html"):
                file = FIXTURES / name
            else:
                file = FIXTURES / f"{name}.html"

            if file.is_file():
                body = file.read_text(encoding="utf-8")
                await route.fulfill(status=status or 200, content_type="text/html", body=body)
            elif status is not None:
                await route.fulfill(status=status, content_type="text/html", body=path)
            else:
                await route.fulfill(status=404, content_type="text/html", body="not found")

        await context.route(f"{BASE}/**", serve)

    return setup
