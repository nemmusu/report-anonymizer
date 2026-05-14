"""Smoke tests for hardware detection and VRAM estimation."""
from __future__ import annotations

from anonymize import hardware as hw


def test_report_does_not_crash() -> None:
    rep = hw.report()
    assert isinstance(rep, hw.HardwareReport)
    assert isinstance(rep.short(), str)


def test_estimate_vram_scales_with_layers() -> None:
    a = hw.estimate_vram_mb(
        model_size_bytes=4_000_000_000,
        n_gpu_layers=0,
        ctx_size=16384,
    )
    b = hw.estimate_vram_mb(
        model_size_bytes=4_000_000_000,
        n_gpu_layers=99,
        ctx_size=16384,
    )
    assert b > a


def test_compatibility_levels() -> None:
    c_ok = hw.compatibility(
        model_size_bytes=2_000_000_000,
        n_gpu_layers=99,
        ctx_size=16384,
        cache_type_k="f16",
        cache_type_v="f16",
        available_vram_mb=24_000,
    )
    assert c_ok.level in ("ok", "tight")
    c_oom = hw.compatibility(
        model_size_bytes=80_000_000_000,
        n_gpu_layers=99,
        ctx_size=131072,
        cache_type_k="f16",
        cache_type_v="f16",
        available_vram_mb=8_000,
    )
    assert c_oom.level == "likely_oom"
    c_cpu = hw.compatibility(
        model_size_bytes=2_000_000_000,
        n_gpu_layers=0,
        ctx_size=16384,
        cache_type_k="f16",
        cache_type_v="f16",
        available_vram_mb=0,
    )
    assert c_cpu.level == "cpu_fallback"
