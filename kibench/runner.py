"""A background asyncio loop so network calls never block the UI thread."""
from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import Any, Coroutine


class AsyncRunner:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="kibench-async", daemon=True)
        self._thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro: Coroutine) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def run(self, coro: Coroutine, timeout: float = 60) -> Any:
        return self.submit(coro).result(timeout)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)


_runner = None


def get_runner() -> AsyncRunner:
    global _runner
    if _runner is None:
        _runner = AsyncRunner()
    return _runner
