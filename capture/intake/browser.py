"""Deterministic browser source port with callback-scoped original reads."""

from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from capture.tasks.snapshot import TaskSnapshot


class OriginalExpired(RuntimeError):
    """The borrow ended or its retained handle has been released."""


class RetainedOriginal:
    def __init__(self, raw: bytes):
        self._raw: bytes | None = raw

    @property
    def released(self) -> bool:
        return self._raw is None

    def read(self) -> bytes:
        raw = self._raw
        if raw is None:
            raise OriginalExpired('retained original has been released')
        return raw

    def release(self) -> None:
        self._raw = None


class BorrowedOriginal:
    def __init__(self, raw: bytes, handles: list[RetainedOriginal]):
        self._raw: bytes | None = raw
        self._handles = handles

    def read(self) -> bytes:
        raw = self._raw
        if raw is None:
            raise OriginalExpired('download callback has returned')
        return raw

    def retain(self) -> RetainedOriginal:
        handle = RetainedOriginal(self.read())
        self._handles.append(handle)
        return handle

    def _invalidate(self) -> None:
        self._raw = None


@dataclass(frozen=True)
class DownloadCompletion:
    download_id: str
    original: BorrowedOriginal


@dataclass(frozen=True)
class StartResult:
    download_id: str | None

    @property
    def accepted(self) -> bool:
        return self.download_id is not None


CompletionHandler = Callable[[DownloadCompletion], None]


class BrowserPort(Protocol):
    def subscribe(self, handler: CompletionHandler) -> None:
        """Install the completion recipient for this browser session."""
        ...

    def start(self, task: TaskSnapshot) -> StartResult:
        """Start serially. Completions can be delivered before this returns."""
        ...


@dataclass(frozen=True)
class ScriptedExport:
    download_id: str
    raw: bytes


@dataclass(frozen=True)
class StartStep:
    task_id: str
    result: StartResult
    during_start: tuple[ScriptedExport, ...] = ()


class ScriptedBrowser:
    """Play source notifications in script order with an explicit borrow boundary."""

    def __init__(self, steps: Iterable[StartStep]):
        self._steps = deque(steps)
        accepted = [step.result.download_id for step in self._steps if step.result.accepted]
        if len(accepted) != len(set(accepted)):
            raise ValueError('script repeats an accepted download ID')
        self._handler: CompletionHandler | None = None
        self._starts: list[str] = []
        self._notified: set[str] = set()
        self._handles: dict[str, list[RetainedOriginal]] = {}
        self._starting = False
        self._stopped = False

    @property
    def starts(self) -> tuple[str, ...]:
        return tuple(self._starts)

    def subscribe(self, handler: CompletionHandler) -> None:
        if self._stopped or self._handler is not None:
            raise RuntimeError('browser session cannot accept another subscriber')
        self._handler = handler

    def start(self, task: TaskSnapshot) -> StartResult:
        if self._stopped or self._starting or self._handler is None:
            raise RuntimeError('browser session cannot start a download now')
        if not self._steps or self._steps[0].task_id != task.task_id:
            raise RuntimeError(f'unexpected download start for {task.task_id!r}')
        step = self._steps.popleft()
        self._starts.append(task.task_id)
        self._starting = True
        try:
            for export in step.during_start:
                self.deliver(export)
            return step.result
        finally:
            self._starting = False

    def deliver(self, export: ScriptedExport) -> None:
        handler = self._handler
        if self._stopped or handler is None:
            raise RuntimeError('browser session cannot deliver a notification now')
        if export.download_id in self._notified:
            raise ValueError(f'repeated completion for {export.download_id!r}')
        self._notified.add(export.download_id)
        handles: list[RetainedOriginal] = []
        self._handles[export.download_id] = handles
        borrowed = BorrowedOriginal(export.raw, handles)
        try:
            handler(DownloadCompletion(export.download_id, borrowed))
        finally:
            borrowed._invalidate()

    def active_handles(self, download_id: str) -> int:
        return sum(not handle.released for handle in self._handles.get(download_id, ()))

    def stop(self) -> None:
        self._stopped = True
        self._handler = None
        self._steps.clear()
