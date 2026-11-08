"""Shared bounded process boundary for parsers and untrusted schema validation."""

from __future__ import annotations

import multiprocessing
import sys

_IN_BOUNDED_WORKER = False


def in_bounded_worker() -> bool:
    return _IN_BOUNDED_WORKER


class WorkerError(ValueError):
    pass


def _execute(sender, function, args):
    global _IN_BOUNDED_WORKER
    _IN_BOUNDED_WORKER = True
    try:
        if sys.platform == "linux":
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        sender.send((True, function(*args)))
    except Exception:  # noqa: BLE001 — boundary redacts data-bearing exceptions and releases child
        sender.send((False, None))
    finally:
        sender.close()


def bounded(function, *args, timeout: float):
    """Function is a code-owned callable, never a name or expression from data."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_execute, args=(sender, function, args), daemon=True)
    process.start()
    sender.close()
    try:
        if not receiver.poll(timeout):
            raise WorkerError("worker time limit exceeded")
        ok, value = receiver.recv()
        if not ok:
            raise WorkerError("worker rejected input")
        return value
    except EOFError as exc:
        raise WorkerError("worker memory or process limit exceeded") from exc
    finally:
        receiver.close()
        if process.is_alive():
            process.terminate()
        process.join(timeout=1)
        if process.is_alive():
            process.kill()
            process.join()
