"""Windows-specific GPU detection tests for :mod:`anonymize.hardware`.

Covers the cascade introduced to fix the bug where the GUI launched
via ``pythonw.exe`` (spawned by the Inno Setup launcher) reported
``GPU: none`` even on a host with a working NVIDIA RTX 5090. The
contract this test file pins down:

1. ``_subprocess_kwargs()`` returns ``creationflags=CREATE_NO_WINDOW``
   on Windows and an empty dict elsewhere, so children of the GUI
   subsystem never flash a console.
2. ``_detect_nvidia()`` finds ``nvidia-smi.exe`` even when
   ``shutil.which`` returns ``None`` (legacy NVSMI path fallback).
3. ``_detect_gpu_powershell_cim()`` parses the
   ``Get-CimInstance Win32_VideoController`` CSV correctly and
   classifies NVIDIA / AMD / Intel by name keywords.
4. ``_detect_gpu_wmic_legacy()`` returns ``[]`` cleanly when wmic is
   missing (Win11 24H2+ ships without it).
5. ``detect_gpus()`` cascades: nvidia hit short-circuits the Windows
   fallbacks; nvidia miss triggers powershell; powershell miss
   triggers wmic.
6. ``HardwareReport.short()`` drops the "(0GB)" suffix when the
   Windows fallback could not read VRAM (``vram_total_mb=0``).

All probes are mocked - we never actually shell out, so this file is
safe to run on any OS (the tests that *behaviourally* require Windows
patch ``os.name`` directly).
"""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import patch

import pytest

from anonymize import hardware as hw


# ---------------------------------------------------------------------------
# _subprocess_kwargs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fake_os_name, expect_creationflags",
    [
        ("nt", True),
        ("posix", False),
    ],
)
def test_subprocess_kwargs_per_os(monkeypatch, fake_os_name, expect_creationflags):
    expected_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    # On Linux runners ``subprocess.CREATE_NO_WINDOW`` does not exist,
    # so ``hw._subprocess_kwargs`` would ``getattr`` it as ``0`` and
    # bail to ``{}`` even with ``os.name == "nt"`` faked. Plant the
    # constant on the ``subprocess`` module the unit-under-test sees so
    # the nt branch is reachable cross-platform.
    monkeypatch.setattr(
        hw.subprocess, "CREATE_NO_WINDOW", expected_no_window, raising=False
    )
    with patch.object(hw, "os") as os_mock:
        os_mock.name = fake_os_name
        kwargs = hw._subprocess_kwargs()
    if expect_creationflags:
        assert kwargs == {"creationflags": expected_no_window}
    else:
        assert kwargs == {}


# ---------------------------------------------------------------------------
# _detect_gpu_powershell_cim
# ---------------------------------------------------------------------------


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["powershell.exe"], returncode=returncode, stdout=stdout, stderr=""
    )


_PS_CSV_NVIDIA = (
    '"Name","AdapterRAM"\r\n'
    '"NVIDIA GeForce RTX 4090","4293918720"\r\n'
)

_PS_CSV_AMD = (
    '"Name","AdapterRAM"\r\n'
    '"AMD Radeon RX 7900 XT","4293918720"\r\n'
)

_PS_CSV_INTEL = (
    '"Name","AdapterRAM"\r\n'
    '"Intel(R) Arc(TM) A770 Graphics","4293918720"\r\n'
)


def test_powershell_cim_nvidia_classified():
    with patch.object(hw.os, "name", "nt"), \
         patch.object(hw.shutil, "which", return_value=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"), \
         patch.object(hw.subprocess, "run", return_value=_completed(_PS_CSV_NVIDIA)):
        gpus = hw._detect_gpu_powershell_cim()
    assert len(gpus) == 1
    assert gpus[0].backend == "nvidia"
    assert "GeForce RTX 4090" in gpus[0].name
    assert gpus[0].vram_total_mb == 0  # 4 GiB cap -> treated as unknown
    assert gpus[0].vram_free_mb == 0


def test_powershell_cim_amd_classified():
    with patch.object(hw.os, "name", "nt"), \
         patch.object(hw.shutil, "which", return_value=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"), \
         patch.object(hw.subprocess, "run", return_value=_completed(_PS_CSV_AMD)):
        gpus = hw._detect_gpu_powershell_cim()
    assert len(gpus) == 1
    assert gpus[0].backend == "amd"
    assert "Radeon" in gpus[0].name


def test_powershell_cim_intel_classified():
    with patch.object(hw.os, "name", "nt"), \
         patch.object(hw.shutil, "which", return_value=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"), \
         patch.object(hw.subprocess, "run", return_value=_completed(_PS_CSV_INTEL)):
        gpus = hw._detect_gpu_powershell_cim()
    assert len(gpus) == 1
    assert gpus[0].backend == "intel"


def test_powershell_cim_empty_returns_empty_list():
    with patch.object(hw.os, "name", "nt"), \
         patch.object(hw.shutil, "which", return_value=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"), \
         patch.object(hw.subprocess, "run", return_value=_completed("")):
        assert hw._detect_gpu_powershell_cim() == []


def test_powershell_cim_garbage_returns_empty_list():
    garbage = "this is not csv at all\nzzz"
    with patch.object(hw.os, "name", "nt"), \
         patch.object(hw.shutil, "which", return_value=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"), \
         patch.object(hw.subprocess, "run", return_value=_completed(garbage)):
        assert hw._detect_gpu_powershell_cim() == []


def test_powershell_cim_skipped_on_non_windows():
    with patch.object(hw.os, "name", "posix"):
        assert hw._detect_gpu_powershell_cim() == []


# ---------------------------------------------------------------------------
# _detect_gpu_wmic_legacy
# ---------------------------------------------------------------------------


def test_wmic_legacy_handles_missing_binary():
    """Win11 24H2+ no longer ships wmic; the call must degrade silently."""
    with patch.object(hw.os, "name", "nt"), \
         patch.object(
             hw.subprocess, "run",
             side_effect=FileNotFoundError("wmic"),
         ):
        assert hw._detect_gpu_wmic_legacy() == []


def test_wmic_legacy_skipped_on_non_windows():
    with patch.object(hw.os, "name", "posix"):
        assert hw._detect_gpu_wmic_legacy() == []


def test_wmic_legacy_parses_csv_when_available():
    wmic_csv = (
        "Node,AdapterRAM,Name\r\n"
        "MYHOST,4293918720,NVIDIA GeForce RTX 5090\r\n"
    )
    with patch.object(hw.os, "name", "nt"), \
         patch.object(hw.subprocess, "run", return_value=_completed(wmic_csv)):
        gpus = hw._detect_gpu_wmic_legacy()
    assert len(gpus) == 1
    assert gpus[0].backend == "nvidia"
    assert "RTX 5090" in gpus[0].name


# ---------------------------------------------------------------------------
# _detect_nvidia (legacy NVSMI fallback)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Exercises the Windows-only legacy NVSMI fallback paths: "
    "``%WINDIR%`` is not expanded by POSIX ``os.path.expandvars`` and "
    "``pathlib.Path`` on Linux instantiates ``PosixPath`` (not "
    "``WindowsPath``) for the backslash-prefixed install locations, "
    "so the mocked ``Path.exists`` lookup observed under the patch does "
    "not faithfully reproduce the production code path on non-Windows "
    "runners. The behaviour itself is covered by the smoke build run on "
    "the ``windows-latest`` CI job.",
)
def test_detect_nvidia_uses_legacy_nvsmi_path_when_which_returns_none():
    """``shutil.which("nvidia-smi")`` can return ``None`` even when the
    binary is installed (e.g. PATHEXT mangled by the launcher's env).
    The detector must then probe the well-known install paths."""
    nvsmi_csv = "NVIDIA GeForce RTX 5090, 32768, 32000\n"

    def fake_exists(self):  # noqa: ANN001 - signature matches Path.exists
        return str(self).lower().endswith("nvidia-smi.exe")

    with patch.object(hw.os, "name", "nt"), \
         patch.object(hw.shutil, "which", return_value=None), \
         patch("anonymize.hardware.Path.exists", new=fake_exists), \
         patch.object(
             hw.subprocess, "run",
             return_value=_completed(nvsmi_csv),
         ) as run_mock:
        gpus = hw._detect_nvidia()

    assert len(gpus) == 1
    assert gpus[0].backend == "nvidia"
    assert gpus[0].vram_total_mb == 32768
    # Confirm the binary path actually used was one of the fallbacks
    invoked = run_mock.call_args[0][0]
    assert invoked[0].lower().endswith("nvidia-smi.exe")


def test_detect_nvidia_returns_empty_when_binary_unreachable():
    """Neither shutil.which nor any fallback path resolves -> []."""
    with patch.object(hw.os, "name", "nt"), \
         patch.object(hw.shutil, "which", return_value=None), \
         patch("anonymize.hardware.Path.exists", lambda self: False):
        assert hw._detect_nvidia() == []


# ---------------------------------------------------------------------------
# detect_gpus cascade
# ---------------------------------------------------------------------------


def test_cascade_short_circuits_when_nvidia_smi_returns_a_gpu():
    """When ``_detect_nvidia`` succeeds, the Windows fallbacks must
    NOT be invoked."""
    fake_gpu = hw.GPU(
        name="NVIDIA GeForce RTX 5090",
        backend="nvidia",
        vram_total_mb=32768,
        vram_free_mb=32000,
    )
    ps_calls = {"n": 0}
    wmic_calls = {"n": 0}

    def spy_ps():
        ps_calls["n"] += 1
        return [hw.GPU(name="ghost", backend="unknown", vram_total_mb=0, vram_free_mb=0)]

    def spy_wmic():
        wmic_calls["n"] += 1
        return []

    with patch.object(hw.os, "name", "nt"), \
         patch.object(hw, "_detect_nvidia", return_value=[fake_gpu]), \
         patch.object(hw, "_detect_amd", return_value=[]), \
         patch.object(hw, "_detect_metal", return_value=[]), \
         patch.object(hw, "_detect_gpu_powershell_cim", side_effect=spy_ps), \
         patch.object(hw, "_detect_gpu_wmic_legacy", side_effect=spy_wmic):
        out = hw.detect_gpus()

    assert out == [fake_gpu]
    assert ps_calls["n"] == 0
    assert wmic_calls["n"] == 0


def test_cascade_falls_through_to_powershell_when_nvidia_empty():
    fake_gpu = hw.GPU(
        name="AMD Radeon RX 7900 XT", backend="amd",
        vram_total_mb=0, vram_free_mb=0,
    )
    wmic_calls = {"n": 0}

    def spy_wmic():
        wmic_calls["n"] += 1
        return []

    with patch.object(hw.os, "name", "nt"), \
         patch.object(hw, "_detect_nvidia", return_value=[]), \
         patch.object(hw, "_detect_amd", return_value=[]), \
         patch.object(hw, "_detect_metal", return_value=[]), \
         patch.object(hw, "_detect_gpu_powershell_cim", return_value=[fake_gpu]), \
         patch.object(hw, "_detect_gpu_wmic_legacy", side_effect=spy_wmic):
        out = hw.detect_gpus()

    assert out == [fake_gpu]
    assert wmic_calls["n"] == 0  # PowerShell hit -> no wmic


def test_cascade_falls_through_to_wmic_when_powershell_also_empty():
    fake_gpu = hw.GPU(
        name="NVIDIA GeForce RTX 4090", backend="nvidia",
        vram_total_mb=0, vram_free_mb=0,
    )
    with patch.object(hw.os, "name", "nt"), \
         patch.object(hw, "_detect_nvidia", return_value=[]), \
         patch.object(hw, "_detect_amd", return_value=[]), \
         patch.object(hw, "_detect_metal", return_value=[]), \
         patch.object(hw, "_detect_gpu_powershell_cim", return_value=[]), \
         patch.object(hw, "_detect_gpu_wmic_legacy", return_value=[fake_gpu]):
        out = hw.detect_gpus()
    assert out == [fake_gpu]


def test_cascade_does_not_run_windows_fallbacks_off_windows():
    """On Linux/macOS the Windows-only probes must never be called."""
    ps_calls = {"n": 0}
    wmic_calls = {"n": 0}

    with patch.object(hw.os, "name", "posix"), \
         patch.object(hw, "_detect_nvidia", return_value=[]), \
         patch.object(hw, "_detect_amd", return_value=[]), \
         patch.object(hw, "_detect_metal", return_value=[]), \
         patch.object(
             hw, "_detect_gpu_powershell_cim",
             side_effect=lambda: ps_calls.__setitem__("n", ps_calls["n"] + 1) or [],
         ), \
         patch.object(
             hw, "_detect_gpu_wmic_legacy",
             side_effect=lambda: wmic_calls.__setitem__("n", wmic_calls["n"] + 1) or [],
         ):
        out = hw.detect_gpus()

    assert out == []
    assert ps_calls["n"] == 0
    assert wmic_calls["n"] == 0


# ---------------------------------------------------------------------------
# HardwareReport.short() VRAM-zero handling
# ---------------------------------------------------------------------------


def test_short_drops_vram_suffix_when_total_is_zero():
    rep = hw.HardwareReport(
        gpus=[hw.GPU(
            name="NVIDIA GeForce RTX 5090",
            backend="nvidia",
            vram_total_mb=0,
            vram_free_mb=0,
        )],
    )
    s = rep.short()
    assert "GPU: NVIDIA GeForce RTX 5090" in s
    assert "0GB" not in s
    assert "(0GB free)" not in s


def test_short_keeps_vram_suffix_when_total_is_known():
    rep = hw.HardwareReport(
        gpus=[hw.GPU(
            name="NVIDIA GeForce RTX 5090",
            backend="nvidia",
            vram_total_mb=32768,
            vram_free_mb=30000,
        )],
    )
    s = rep.short()
    assert "32GB" in s
    assert "free" in s
