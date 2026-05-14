from __future__ import annotations

from anonymize.server_doctor import diagnose


def test_detect_cuda_oom() -> None:
    log = "ggml_cuda_compute: CUDA error: out of memory"
    d = diagnose(log)
    assert d.cause == "cuda_oom"
    assert any(a.startswith("switch_preset:cpu_only") for a in d.suggested_actions)


def test_detect_file_not_found() -> None:
    log = "model file does not exist: /tmp/missing.gguf"
    d = diagnose(log)
    assert d.cause == "file_not_found"


def test_detect_port_in_use() -> None:
    log = "bind: EADDRINUSE address already in use"
    d = diagnose(log)
    assert d.cause == "port_in_use"


def test_unknown_returncode() -> None:
    d = diagnose("", last_returncode=137)
    assert d.cause == "unknown"
    assert "open_log" in d.suggested_actions
