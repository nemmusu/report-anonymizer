"""Text-only LLM client for llama.cpp / OpenAI-compatible servers.

Public surface:

* ``chat()`` accepts a list of messages and an explicit ``seed`` for
  reproducibility and self-consistency voting,
* ``vote()`` runs ``chat()`` ``n`` times with different seeds and
  returns the answer with the highest agreement ratio,
* ``chat_many()`` fires N requests in parallel using a thread pool
  to drive ``llama-server --parallel N`` slots concurrently.

Designed for the Qwen3.5 family: thinking is disabled by default
(otherwise the model burns the entire ``max_tokens`` budget inside
``<think>...</think>`` and never reaches the JSON answer).
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, Future, wait as futures_wait
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

import requests


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def cancel_llama_slots(base_url: str, *, timeout: float = 2.0) -> int:
    """Erase every active llama-server slot at ``base_url``.

    Returns the number of slots that were erased (0 on any error or on
    backends that don't expose ``/slots``, vLLM, OpenAI proxies, etc.).
    The base URL must be the server root (no ``/v1`` suffix); callers
    that hold an ``/v1`` URL should strip it before calling.

    This is the only reliable way to interrupt llama-server GPU
    inference without killing the server: closing the client socket
    only affects the kernel's send buffer; the GPU keeps running until
    the broken pipe is detected on the next write, which can be tens of
    seconds for a long completion.
    """
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    slots_url = f"{base}/slots"
    erased = 0
    try:
        r = requests.get(slots_url, timeout=timeout)
        if r.status_code != 200:
            return 0
        slots = r.json()
        if not isinstance(slots, list):
            return 0
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            sid = slot.get("id")
            state = slot.get("state")
            if sid is None or state is None or state == 0:
                continue
            try:
                requests.post(
                    f"{slots_url}/{sid}",
                    params={"action": "erase"},
                    timeout=timeout,
                )
                erased += 1
            except Exception:
                pass
    except Exception:
        return erased
    return erased


def parse_json_loose(text: str) -> dict | None:
    """Tolerant JSON object extraction.

    Tries strict ``json.loads`` first; falls back to extracting the first
    ``{...}`` block via regex if the model emitted leading prose.
    """
    if not text:
        return None
    text = text.strip()
    try:
        out = json.loads(text)
        if isinstance(out, dict):
            return out
    except Exception:
        pass
    m = _JSON_OBJECT_RE.search(text)
    if m:
        try:
            out = json.loads(m.group(0))
            if isinstance(out, dict):
                return out
        except Exception:
            return None
    return None


def _retry_user_prompt() -> str:
    return (
        "Your previous response was not valid JSON. "
        "Reply ONLY with a JSON object that matches the schema "
        "above: no markdown fences, no comments, no preamble."
    )


@dataclass
class ChatJob:
    """One pending request for ``chat_many()``."""

    system: str
    user: str
    seed: Optional[int] = None
    max_tokens: int = 2048
    temperature: Optional[float] = None
    json_mode: bool = True
    tag: Any = None  # opaque identifier preserved in the result


class LLMClient:
    """Minimal client for llama.cpp ``/v1/chat/completions`` (OpenAI-compatible)."""

    def __init__(
        self,
        base_url: str = "http://localhost:8080/v1",
        model: str = "qwen3.5-9b",
        timeout: int = 180,
        max_retries: int = 1,
        temperature: float = 0.3,
        top_p: float = 0.8,
        top_k: int = 20,
        min_p: float = 0.0,
        presence_penalty: float = 0.0,
        max_workers: int = 4,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.temperature = temperature
        self.top_p = top_p
        self.top_k = top_k
        self.min_p = min_p
        self.presence_penalty = presence_penalty
        self.max_workers = max(1, int(max_workers))
        self._session = requests.Session()
        self._healthy: bool | None = None

    # ---- transport ----------------------------------------------------------

    def _health_url(self) -> str:
        if self.base_url.endswith("/v1"):
            return self.base_url[:-3] + "/health"
        return self.base_url + "/health"

    def health(self, refresh: bool = False) -> bool:
        if self._healthy is not None and not refresh:
            return self._healthy
        try:
            r = self._session.get(self._health_url(), timeout=5)
            self._healthy = r.status_code == 200
        except Exception:
            self._healthy = False
        return self._healthy

    def _slots_base(self) -> str:
        if self.base_url.endswith("/v1"):
            return self.base_url[:-3]
        return self.base_url

    def abort_in_flight(self) -> None:
        """Force any in-flight ``/v1/chat/completions`` request to return immediately.

        Cooperative ``stop_event`` checks only fire *between* HTTP calls, once
        a chat POST is in flight we're stuck in ``recv()`` until llama-server
        finishes generating. To make Stop genuinely instant we use the same
        two-pronged trick the pretty-text-api project uses:

        1. ``POST /slots/<id>?action=erase`` for every active slot, tells
           llama-server to abort GPU inference on that slot, which makes the
           pending HTTP response return at once.
        2. Close the ``requests.Session`` so its connection pool drops the
           live sockets (a no-op for already-finished requests, a hard close
           for anything still streaming).

        Safe to call from any thread; safe on non-llama backends (the slots
        endpoint just 404s and we ignore it). Idempotent.
        """
        cancel_llama_slots(self._slots_base())
        try:
            self._session.close()
        except Exception:
            pass
        # Re-arm the session so a follow-up call (e.g. user resumes the
        # pipeline) doesn't blow up on a closed pool.
        try:
            self._session = requests.Session()
        except Exception:
            pass

    def _post_chat(
        self,
        messages: list[dict],
        json_mode: bool,
        max_tokens: int,
        seed: int | None,
        temperature: float | None = None,
    ) -> str:
        url = f"{self.base_url}/chat/completions"
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "min_p": self.min_p,
            "presence_penalty": self.presence_penalty,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if seed is not None:
            body["seed"] = seed
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        r = self._session.post(url, json=body, timeout=self.timeout)
        if r.status_code >= 400:
            err_msg = ""
            try:
                err_body = r.json()
                err_msg = (
                    err_body.get("error", {}).get("message")
                    or err_body.get("message")
                    or json.dumps(err_body)[:500]
                )
            except Exception:
                err_msg = r.text[:500]
            raise requests.exceptions.HTTPError(
                f"{r.status_code} {r.reason}: {err_msg}", response=r
            )
        data = r.json()
        choice = (data.get("choices") or [{}])[0]
        return (choice.get("message") or {}).get("content") or ""

    # ---- public API ---------------------------------------------------------

    def chat(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = True,
        max_tokens: int = 2048,
        seed: int | None = None,
        temperature: float | None = None,
        stop_event: Optional[threading.Event] = None,
    ) -> tuple[dict | None, str]:
        """Single chat round.

        Returns ``(parsed_json_or_None, raw_text)``. If ``json_mode`` is True
        and the first answer is not valid JSON, performs one retry asking for
        valid JSON explicitly (``self.max_retries`` controls additional rounds).

        ``stop_event`` is checked between retries; if set, returns
        ``(None, "")`` immediately.
        """
        if stop_event and stop_event.is_set():
            return None, ""
        messages_first = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        raw = ""
        for attempt in range(self.max_retries + 1):
            if stop_event and stop_event.is_set():
                return None, raw
            try:
                if attempt == 0:
                    raw = self._post_chat(
                        messages_first,
                        json_mode=json_mode,
                        max_tokens=max_tokens,
                        seed=seed,
                        temperature=temperature,
                    )
                else:
                    retry_messages = messages_first + [
                        {"role": "assistant", "content": raw or "(nessuna risposta)"},
                        {"role": "user", "content": _retry_user_prompt()},
                    ]
                    raw = self._post_chat(
                        retry_messages,
                        json_mode=json_mode,
                        max_tokens=max_tokens,
                        seed=seed,
                        temperature=temperature,
                    )
                if json_mode:
                    parsed = parse_json_loose(raw)
                    if parsed is not None:
                        return parsed, raw
                else:
                    return None, raw
            except requests.exceptions.HTTPError as e:
                if stop_event and stop_event.is_set():
                    return None, raw
                code = getattr(e.response, "status_code", 0)
                if code in (429, 503):
                    time.sleep(min(2 ** attempt, 8))
                    continue
                time.sleep(1.0)
            except requests.exceptions.RequestException:
                # Connection killed by ``abort_in_flight`` (Stop click)
                # surfaces here. Don't sleep on top of the user's stop
                # — return immediately so the worker exits within a
                # second instead of waiting another 1 s before noticing
                # the event flag.
                if stop_event and stop_event.is_set():
                    return None, raw
                time.sleep(1.0)
            except Exception:
                if stop_event and stop_event.is_set():
                    return None, raw
        return None, raw

    def chat_many(
        self,
        jobs: list[ChatJob],
        *,
        max_workers: Optional[int] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> list[tuple[Any, dict | None, str]]:
        """Fire ``jobs`` requests in parallel via a thread pool.

        Returns ``[(tag, parsed, raw), ...]`` in the same order as ``jobs``
        (independent of completion order). Cooperative cancellation honored
        via ``stop_event``.
        """
        if not jobs:
            return []
        workers = int(max_workers or self.max_workers)
        workers = max(1, workers)
        results: list[Optional[tuple[Any, dict | None, str]]] = [None] * len(jobs)

        def _run(idx: int, job: ChatJob) -> None:
            if stop_event and stop_event.is_set():
                results[idx] = (job.tag, None, "")
                return
            parsed, raw = self.chat(
                job.system,
                job.user,
                json_mode=job.json_mode,
                max_tokens=job.max_tokens,
                seed=job.seed,
                temperature=job.temperature,
                stop_event=stop_event,
            )
            results[idx] = (job.tag, parsed, raw)

        ex = ThreadPoolExecutor(max_workers=workers)
        try:
            futures: list[Future] = [
                ex.submit(_run, i, j) for i, j in enumerate(jobs)
            ]
            cancelled = False
            # Poll futures with a short timeout so Stop is honored
            # within ~0.5 s instead of having to wait for the next
            # future to complete (which can take minutes for a long
            # LLM generation). ``abort_in_flight`` will have closed
            # the underlying socket already, but the worker thread
            # still needs a moment to bail out of ``time.sleep`` /
            # parse retries; polling means we don't add another
            # blocking ``f.result()`` on top of that.
            pending: set[Future] = set(futures)
            while pending:
                if stop_event and stop_event.is_set():
                    cancelled = True
                    break
                done, pending = futures_wait(
                    pending, timeout=0.5, return_when=FIRST_COMPLETED
                )
                for f in done:
                    try:
                        f.result()
                    except Exception:
                        pass
        finally:
            # Default ``__exit__`` does ``shutdown(wait=True)`` which
            # waits for queued jobs to complete, even after Stop. Use
            # ``cancel_futures=True`` so Stop genuinely halts the rest
            # of the batch rather than blocking the user for minutes.
            ex.shutdown(
                wait=not bool(stop_event and stop_event.is_set()),
                cancel_futures=bool(stop_event and stop_event.is_set()),
            )
        # Fill any unset slots
        out: list[tuple[Any, dict | None, str]] = []
        for i, r in enumerate(results):
            if r is None:
                out.append((jobs[i].tag, None, ""))
            else:
                out.append(r)
        return out

    def vote(
        self,
        system: str,
        user: str,
        *,
        n: int = 3,
        max_tokens: int = 2048,
        base_seed: int = 1,
        json_mode: bool = True,
        agreement_key: str | None = None,
        stop_event: Optional[threading.Event] = None,
    ) -> tuple[dict | None, float, list[dict]]:
        """Run ``chat()`` ``n`` times **in parallel** with different seeds.

        Aggregates the answers and returns ``(majority_answer, agreement_ratio,
        all_answers)``. ``agreement_ratio`` is in ``[0, 1]``.

        If ``agreement_key`` is provided, the comparison is done on that key
        (e.g. ``"is_real_leak"`` for the critic). Otherwise the whole JSON is
        normalized to a sorted-key string for comparison.
        """
        n = max(1, n)
        jobs = [
            ChatJob(
                system=system,
                user=user,
                seed=base_seed + i * 1000003,
                max_tokens=max_tokens,
                json_mode=json_mode,
                tag=i,
            )
            for i in range(n)
        ]
        results = self.chat_many(jobs, stop_event=stop_event)
        answers: list[dict] = []
        signatures: list[str] = []
        for _tag, parsed, _raw in results:
            if parsed is None:
                continue
            answers.append(parsed)
            sig = (
                str(parsed.get(agreement_key, ""))
                if agreement_key
                else json.dumps(parsed, sort_keys=True, ensure_ascii=False)
            )
            signatures.append(sig)
        if not answers:
            return None, 0.0, []
        c = Counter(signatures)
        top_sig, top_count = c.most_common(1)[0]
        for ans, sig in zip(answers, signatures):
            if sig == top_sig:
                return ans, top_count / len(answers), answers
        return answers[0], 1.0 / len(answers), answers


def first_or_none(it: Iterable[Any]) -> Any | None:
    for x in it:
        return x
    return None


__all__ = ["LLMClient", "ChatJob", "parse_json_loose"]
