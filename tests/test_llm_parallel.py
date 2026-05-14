"""Parallel LLM client tests using the MockLLMClient."""
from __future__ import annotations

import threading

from anonymize.llm_client import ChatJob


def test_chat_many_returns_per_job_results(mock_llm) -> None:
    mock_llm.queue({"candidates": [{"value": "Acme", "category": "brand", "confidence": 0.95}]})
    mock_llm.queue({"candidates": [{"value": "Beta", "category": "brand", "confidence": 0.91}]})
    jobs = [ChatJob(system="s", user="u1", tag=1), ChatJob(system="s", user="u2", tag=2)]
    res = mock_llm.chat_many(jobs)
    assert len(res) == 2
    tags = sorted(r[0] for r in res)
    assert tags == [1, 2]


def test_stop_event_skips_remaining_jobs(mock_llm) -> None:
    ev = threading.Event()
    ev.set()
    jobs = [ChatJob(system="s", user="x", tag=i) for i in range(5)]
    res = mock_llm.chat_many(jobs, stop_event=ev)
    assert all(r[1] is None for r in res)
