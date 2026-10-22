"""Threading wrapper around `jarvis.modules.daemon.client` (design doc
§1b/§3).

Every one of `daemon_client`'s functions is a synchronous, blocking call
- a real HTTP round-trip, and for `ensure_daemon_running()`'s auto-start
path, potentially the full ~15s startup timeout. Calling any of them
directly from the GUI thread would freeze the whole window for that
long. This module does not reimplement any of that logic - it runs the
exact function the caller gives it on a `QThreadPool` worker thread
(`DaemonCallWorker`, a `QRunnable`), and delivers the result back to the
GUI thread via a Qt signal, which is the standard, safe way to cross a
thread boundary in Qt (a `QRunnable` can't emit signals itself - it
doesn't inherit `QObject` - hence the small `WorkerSignals` companion
object below).

Two different outcomes a caller needs to be able to tell apart, both
already true of `daemon_client` itself, not invented here:

  - The read wrappers (`search`, `get_hierarchy`, `browse`, `related`,
    `query_db`) never raise - a daemon-unreachable failure comes back as
    an `ErrorResponse`, delivered on `succeeded` like any other result.
    Callers check `isinstance(result, ErrorResponse)` themselves, same
    as every other `daemon_client` caller already does.
  - The ingest wrappers (`run_ingest_filesystem`, `run_ingest_drive`) DO
    raise `DaemonError` on a daemon-unreachable failure - `failed`
    carries that exception across the thread boundary, since a raised
    exception can't cross threads via a return value the way `succeeded`
    delivers one.
"""

from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class WorkerSignals(QObject):
    succeeded = Signal(object)
    failed = Signal(object)  # the exception instance, not a message string


class DaemonCallWorker(QRunnable):
    """Runs `fn(*args, **kwargs)` on a `QThreadPool` worker thread. `fn`
    is expected to be one of `daemon_client`'s own functions (or a
    lambda closing over one, e.g. to pass a fixed query string) - this
    class doesn't know or care which; it has no daemon-specific logic of
    its own at all."""

    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception as exc:  # noqa: BLE001 - deliberately broad: whatever
            # goes wrong on this worker thread must reach the GUI thread via
            # `failed`, not vanish silently because it wasn't the exact
            # exception type expected.
            self.signals.failed.emit(exc)
            return
        self.signals.succeeded.emit(result)


# Real bug this exists to fix, found while testing: QThreadPool.start()
# transfers ownership of the *C++* QRunnable to the pool, but nothing
# guarantees the *Python* DaemonCallWorker object (and, critically, its
# `signals` QObject - the actual signal/slot connection lives on it, not
# on the QRunnable) survives that long if nothing in Python still holds
# a reference. A caller that doesn't capture run_daemon_call's return
# value - the documented, intended usage - could have its worker garbage
# collected before the pool thread ever runs it, silently dropping the
# callback. This module-level set is a plain, deliberate strong
# reference held for exactly as long as the call is in flight, removed
# the moment either signal fires - not a leak, just a lifetime the
# caller shouldn't have to manage themselves.
_in_flight: set[DaemonCallWorker] = set()


def run_daemon_call(
    pool: QThreadPool,
    fn: Callable[..., Any],
    *args: Any,
    on_success: Optional[Callable[[Any], None]] = None,
    on_error: Optional[Callable[[Exception], None]] = None,
    **kwargs: Any,
) -> DaemonCallWorker:
    """Build a `DaemonCallWorker` for `fn(*args, **kwargs)`, wire its
    signals to `on_success`/`on_error`, submit it to `pool`, and return
    it. Callers don't need to keep a reference to the return value for
    it to run to completion - see `_in_flight` above for why that's
    actually true, not just documented as true."""
    worker = DaemonCallWorker(fn, *args, **kwargs)
    _in_flight.add(worker)

    def _cleanup(_result: Any = None) -> None:
        _in_flight.discard(worker)

    if on_success is not None:
        worker.signals.succeeded.connect(on_success)
    if on_error is not None:
        worker.signals.failed.connect(on_error)
    worker.signals.succeeded.connect(_cleanup)
    worker.signals.failed.connect(_cleanup)

    pool.start(worker)
    return worker
