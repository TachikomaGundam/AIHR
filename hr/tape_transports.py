"""Tape transports: replay/record wrappers for the two HTTP stacks.

openai_compat uses requests.post(stream=True) and consumes the response via
.status_code / .text / .iter_lines(decode_unicode=True) / .close();
anthropic_compat injects an httpx-style client whose .stream(...) context
manager yields .status_code / .read() / .text / .iter_lines(). These wrappers
speak exactly those protocols - the SSE parse chains stay untouched, so a
replayed answer walks the identical verification and storage path as a live
one (same-codec-same-landing, the S1a lesson).

An exchange is recorded only when it completed cleanly (close/ctx-exit);
partial streams are simply not on the tape, and the next lookup misses -
fail-closed towards the re-record procedure, never a half-truth on tape.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Callable, Iterator

from .tape import ReplayEntry, TapeStore


class _LineResponseBase:
    def __init__(self, entry: ReplayEntry) -> None:
        self.status_code = entry.status
        self._lines = entry.sse_lines

    @property
    def text(self) -> str:
        return "\n".join(self._lines)

    def iter_lines(self, decode_unicode: bool = False) -> Iterator[str]:
        return iter(self._lines)


class ReplayRequestsResponse(_LineResponseBase):
    """requests.Response stand-in for the openai stack."""

    def close(self) -> None:
        return None


class ReplayHttpxClient:
    """httpx.Client stand-in for the anthropic stack."""

    def __init__(self, tape: Any) -> None:
        self._tape = tape

    class _StreamCtx:
        def __init__(self, entry: ReplayEntry) -> None:
            self._resp = _LineResponseBase(entry)

        def __enter__(self) -> "ReplayHttpxClient._StreamCtx":
            return self

        def __exit__(
            self,
            exc: type[BaseException] | None,
            val: BaseException | None,
            tb: TracebackType | None,
        ) -> None:
            return None

        def __getattr__(self, name: str) -> Any:
            return getattr(self.__dict__["_resp"], name)

        def read(self) -> bytes:
            return self._resp.text.encode("utf-8")

    def stream(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        headers: Any = None,
        timeout: Any = None,
        **_kw: Any,
    ) -> "ReplayHttpxClient._StreamCtx":
        return self._StreamCtx(self._tape.lookup(method, url, headers, json))


class RecordingHttpxClient:
    """Wraps the real httpx client, teeing completed SSE streams into the tape.

    Mirrors RecordingRequestsPost for the anthropic stack: the exchange is
    recorded only when the stream ran to completion inside the context;
    mid-stream death records nothing and the next lookup misses (fail-closed
    towards the re-record procedure)."""

    def __init__(self, tape: TapeStore, inner: Any) -> None:
        self._tape = tape
        self._inner = inner

    class _TeeCtx:
        def __init__(
            self,
            tape: TapeStore,
            method: str,
            url: str,
            headers: Any,
            body: Any,
            inner_ctx: Any,
        ) -> None:
            self._tape = tape
            self._req = (method, url, headers, body)
            self._inner = inner_ctx
            self._lines: list[str] = []
            self._resp: Any = None

        def __enter__(self) -> "RecordingHttpxClient._TeeCtx":
            self._resp = self._inner.__enter__()
            return self

        def __exit__(
            self,
            exc: type[BaseException] | None,
            val: BaseException | None,
            tb: TracebackType | None,
        ) -> None:
            if self._inner.__exit__(exc, val, tb):
                return
            if exc is None:
                self._tape.record(
                    *self._req,
                    status=int(self._resp.status_code),
                    sse_lines=self._lines,
                )

        def __getattr__(self, name: str) -> Any:
            return getattr(self.__dict__["_resp"], name)

        def iter_lines(self) -> Iterator[Any]:
            for line in self._resp.iter_lines():
                self._lines.append("" if line is None else str(line))
                yield line

        def read(self) -> bytes:
            return str(self._resp.text).encode("utf-8")

    def stream(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        headers: Any = None,
        timeout: Any = None,
        **kw: Any,
    ) -> "RecordingHttpxClient._TeeCtx":
        inner = self._inner.stream(
            method, url, json=json, headers=headers, timeout=timeout, **kw
        )
        return self._TeeCtx(self._tape, method, url, headers, json, inner)


def replay_post(tape: Any) -> Callable[..., ReplayRequestsResponse]:
    """requests.post stand-in backed by the tape (openai stack)."""

    def post(
        url: str,
        headers: Any = None,
        json: Any = None,
        stream: bool = False,
        timeout: Any = None,
        **_kw: Any,
    ) -> ReplayRequestsResponse:
        return ReplayRequestsResponse(tape.lookup("POST", url, headers, json))

    return post


class RecordingRequestsPost:
    """Wraps the real requests.post, teeing the SSE stream into the tape."""

    def __init__(self, tape: TapeStore, inner: Callable[..., Any]) -> None:
        self._tape = tape
        self._inner = inner

    def __call__(
        self,
        url: str,
        headers: Any = None,
        json: Any = None,
        stream: bool = False,
        timeout: Any = None,
        **kw: Any,
    ) -> Any:
        response = self._inner(
            url, headers=headers, json=json, stream=stream, timeout=timeout, **kw
        )
        return _TeeRequestsResponse(self._tape, "POST", url, headers, json, response)


class _TeeRequestsResponse:
    def __init__(
        self,
        tape: TapeStore,
        method: str,
        url: str,
        headers: Any,
        body: Any,
        inner: Any,
    ) -> None:
        self._tape = tape
        self._req = (method, url, headers, body)
        self._inner = inner
        self._lines: list[str] = []
        self._finished = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def text(self) -> str:
        body = str(self._inner.text)
        if not self._lines:
            self._lines = [body]  # error branch: the body is one payload, tee it
        return body

    def iter_lines(self, decode_unicode: bool = False) -> Iterator[Any]:
        for line in self._inner.iter_lines(decode_unicode=decode_unicode):
            self._lines.append("" if line is None else str(line))
            yield line
        self._finish()

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._tape.record(
            *self._req, status=int(self._inner.status_code), sse_lines=self._lines
        )

    def close(self) -> None:
        if not self._finished and int(getattr(self._inner, "status_code", 0)) >= 400:
            self._finish()  # error bodies are complete once .text was read
        self._inner.close()
