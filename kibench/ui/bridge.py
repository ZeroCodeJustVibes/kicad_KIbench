"""Run coroutines on the background loop and deliver results on the Qt GUI thread."""
from __future__ import annotations

import logging
import traceback
from typing import Any, Callable, Coroutine, Optional

from PySide6.QtCore import QObject, Signal, Slot

from ..runner import get_runner

log = logging.getLogger(__name__)


class AsyncBridge(QObject):
    _done = Signal(object, object, object, object)     # ok_cb, err_cb, result, exc

    def __init__(self, parent=None):
        super().__init__(parent)
        self._done.connect(self._deliver)
        self.runner = get_runner()

    def run(self, coro: Coroutine, on_ok: Optional[Callable[[Any], None]] = None,
            on_err: Optional[Callable[[BaseException], None]] = None):
        fut = self.runner.submit(coro)

        def cb(f):
            try:
                res, exc = f.result(), None
            except BaseException as e:  # delivered to on_err on the GUI thread
                res, exc = None, e
                if on_err is None:
                    log.error("background task failed: %s", "".join(traceback.format_exception(e)))
            self._done.emit(on_ok, on_err, res, exc)
        fut.add_done_callback(cb)
        return fut

    def call_soon(self, fn: Callable[[], None]) -> None:
        """Run fn on the GUI thread (safe from any thread)."""
        self._done.emit(lambda _r: fn(), None, None, None)

    @Slot(object, object, object, object)
    def _deliver(self, ok, err, res, exc):
        if exc is not None:
            if err:
                err(exc)
            return
        if ok:
            ok(res)
