"""Per-repo download path resolution + legacy fallback."""
from __future__ import annotations

from pathlib import Path

import pytest

from anonymize import hf_models


@pytest.fixture
def fake_models_dir(tmp_path, monkeypatch) -> Path:
    """Redirect ``hf_models.MODELS_DIR`` (and its callers in
    ``download_queue``) to a temporary directory."""
    monkeypatch.setattr(hf_models, "MODELS_DIR", tmp_path)
    from anonymize import download_queue as dq

    monkeypatch.setattr(dq, "MODELS_DIR", tmp_path)
    return tmp_path


def test_repo_models_dir_uses_double_underscore(fake_models_dir: Path) -> None:
    p = hf_models.repo_models_dir("unsloth/Qwen3.6-27B-GGUF")
    assert p == fake_models_dir / "unsloth__Qwen3.6-27B-GGUF"


def test_expected_path_uses_subdir_for_new_files(fake_models_dir: Path) -> None:
    p = hf_models.expected_path_for(
        "unsloth/gemma-4-E4B-it-GGUF", "mmproj-BF16.gguf"
    )
    assert p.parent.name == "unsloth__gemma-4-E4B-it-GGUF"
    assert p.name == "mmproj-BF16.gguf"


def test_expected_path_falls_back_to_flat_legacy(fake_models_dir: Path) -> None:
    """Files already on disk in the flat layout keep working without a
    forced re-download."""
    flat = fake_models_dir / "Qwen3.5-9B-UD-Q6_K_XL.gguf"
    flat.write_bytes(b"x")
    p = hf_models.expected_path_for(
        "unsloth/Qwen3.5-9B-GGUF", "Qwen3.5-9B-UD-Q6_K_XL.gguf"
    )
    assert p == flat


def test_two_repos_with_same_filename_dont_collide(fake_models_dir: Path) -> None:
    a = hf_models.expected_path_for(
        "unsloth/Qwen3.6-27B-GGUF", "mmproj-BF16.gguf"
    )
    b = hf_models.expected_path_for(
        "unsloth/gemma-4-E4B-it-GGUF", "mmproj-BF16.gguf"
    )
    assert a != b
    assert a.parent.name == "unsloth__Qwen3.6-27B-GGUF"
    assert b.parent.name == "unsloth__gemma-4-E4B-it-GGUF"


def test_local_models_lists_flat_and_subdirs(fake_models_dir: Path) -> None:
    """``local_models`` walks both the legacy flat layout and the
    per-repo subdirectories.  Vision projector files (``mmproj-*``)
    are filtered out: the pipeline is text-only and showing them in
    the Library tab would only confuse users."""
    (fake_models_dir / "Legacy.gguf").write_bytes(b"x")
    sub = fake_models_dir / "unsloth__gemma-4-E4B-it-GGUF"
    sub.mkdir()
    (sub / "model.gguf").write_bytes(b"x")
    (sub / "mmproj-BF16.gguf").write_bytes(b"x")  # filtered out

    paths = hf_models.local_models()
    # Use Path.as_posix() so the assertion is OS-agnostic: on Windows the
    # filesystem returns 'unsloth__...\\model.gguf' which would otherwise
    # never match the forward-slash literal below.
    rel = sorted(p.relative_to(fake_models_dir).as_posix() for p in paths)
    assert rel == [
        "Legacy.gguf",
        "unsloth__gemma-4-E4B-it-GGUF/model.gguf",
    ]


def test_delete_local_cleans_empty_subdir(fake_models_dir: Path) -> None:
    sub = fake_models_dir / "unsloth__some-repo"
    sub.mkdir()
    f = sub / "model.gguf"
    f.write_bytes(b"x")
    assert hf_models.delete_local(f) is True
    assert not f.exists()
    # The (now empty) per-repo subdirectory is also removed.
    assert not sub.exists()


def test_delete_local_keeps_subdir_with_other_files(fake_models_dir: Path) -> None:
    sub = fake_models_dir / "unsloth__some-repo"
    sub.mkdir()
    a = sub / "a.gguf"
    b = sub / "b.gguf"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    assert hf_models.delete_local(a) is True
    # b.gguf still there → subdir kept
    assert sub.exists()
    assert b.exists()


def test_download_queue_default_dst_uses_per_repo(fake_models_dir, monkeypatch) -> None:
    from anonymize import download_queue as dq

    monkeypatch.setattr(dq, "QUEUE_PATH", fake_models_dir / "downloads.yml")
    monkeypatch.setattr(dq, "CONFIG_DIR", fake_models_dir)
    q = dq.DownloadQueue.load()
    job = q.enqueue("unsloth/gemma-4-E4B-it-GGUF", "mmproj-BF16.gguf")
    assert "unsloth__gemma-4-E4B-it-GGUF" in job.dst
    assert job.dst.endswith("mmproj-BF16.gguf")
