from __future__ import annotations

from pathlib import Path

import yaml

from anonymize import download_queue as dq


def test_enqueue_and_persist(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(dq, "QUEUE_PATH", tmp_path / "downloads.yml")
    monkeypatch.setattr(dq, "CONFIG_DIR", tmp_path)
    q = dq.DownloadQueue.load()
    job = q.enqueue("repo/x", "model.gguf", dst=tmp_path / "model.gguf")
    assert (tmp_path / "downloads.yml").exists()
    again = dq.DownloadQueue.load()
    assert any(j.repo_id == "repo/x" for j in again.jobs)


def test_enqueue_dedupes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(dq, "QUEUE_PATH", tmp_path / "downloads.yml")
    monkeypatch.setattr(dq, "CONFIG_DIR", tmp_path)
    q = dq.DownloadQueue.load()
    q.enqueue("repo/y", "f.gguf")
    q.enqueue("repo/y", "f.gguf")
    assert len([j for j in q.jobs if j.repo_id == "repo/y"]) == 1


def test_resume_pending_after_crash(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(dq, "QUEUE_PATH", tmp_path / "downloads.yml")
    monkeypatch.setattr(dq, "CONFIG_DIR", tmp_path)
    q = dq.DownloadQueue.load()
    j = q.enqueue("r/z", "z.gguf")
    j.status = "running"
    q.update(j)
    again = dq.DownloadQueue.load()
    assert any(jj.status == "pending" and jj.repo_id == "r/z" for jj in again.jobs)
