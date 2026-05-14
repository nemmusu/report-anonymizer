"""Manage a local ``llama-server`` (llama.cpp) process from the GUI/CLI.

Now profile-aware: the manager accepts a :class:`ServerProfile` and renders
the full CLI via :func:`render_command`. Backwards-compat ``ServerConfig``
shim is kept for the older code paths.

Process-lifecycle guarantees:

* ``start()`` spawns ``llama-server`` in a brand-new process group via
  :func:`anonymize._proc.spawn_new_process_group` so SIGTERM/SIGKILL
  (POSIX) or ``CTRL_BREAK_EVENT`` (Windows) can target the whole tree
  and reap children.
* ``stop()`` delegates to :func:`anonymize._proc.terminate_process_tree`,
  which sends the right soft signal first, then escalates to a hard
  kill after a timeout, then sweeps every descendant via psutil
  (paranoid fallback, in case the OS lost the session/group linkage).
* Every started instance is registered in a module-level ``_LIVE_MANAGERS``
  set; an ``atexit`` hook stops any still-running server when the host
  process exits, even if the GUI crashed before reaching ``closeEvent``.
"""
from __future__ import annotations

import atexit
import os
import re
import shutil
import subprocess
import threading
import time
import weakref
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

from ._proc import (
    find_pid_listening_on,
    spawn_new_process_group,
    terminate_process_tree,
)
from .server_profile import (
    DEFAULT_BINARY,
    ServerProfile,
    render_command,
)
from .server_doctor import Diagnosis, diagnose


# Track every live manager so atexit can clean up even if the GUI crashes.
_LIVE_MANAGERS: "weakref.WeakSet[ServerManager]" = weakref.WeakSet()


def _atexit_cleanup() -> None:
    for mgr in list(_LIVE_MANAGERS):
        try:
            if mgr.is_running():
                mgr.stop(timeout=3.0)
        except Exception:
            pass


atexit.register(_atexit_cleanup)


class _OrphanProc:
    """Minimal Popen-shaped adapter for an externally-spawned PID.

    Used by :meth:`ServerManager.stop` when the user started
    ``llama-server`` outside this app: we have a PID (from
    :func:`find_pid_listening_on`) but no :class:`subprocess.Popen`
    handle. ``terminate_process_tree`` only needs ``pid`` plus a few
    no-op shims (``send_signal``, ``terminate``, ``kill``, ``wait``)
    so we provide them as cross-platform os.kill / os.waitpid wrappers.
    """

    def __init__(self, pid: int) -> None:
        self.pid = int(pid)
        self.returncode: Optional[int] = None

    def send_signal(self, sig: int) -> None:
        os.kill(self.pid, sig)

    def terminate(self) -> None:
        if os.name == "nt":
            try:
                os.kill(self.pid, getattr(__import__("signal"), "SIGTERM", 15))
            except Exception:
                pass
        else:
            import signal as _signal
            os.kill(self.pid, _signal.SIGTERM)

    def kill(self) -> None:
        if os.name == "nt":
            try:
                os.kill(self.pid, getattr(__import__("signal"), "SIGTERM", 15))
            except Exception:
                pass
        else:
            import signal as _signal
            os.kill(self.pid, _signal.SIGKILL)

    def wait(self, timeout: Optional[float] = None) -> int:
        deadline = (
            time.monotonic() + timeout if timeout is not None else None
        )
        while True:
            try:
                os.kill(self.pid, 0)
            except ProcessLookupError:
                self.returncode = 0
                return 0
            except Exception:
                self.returncode = 0
                return 0
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(cmd=str(self.pid), timeout=timeout or 0.0)
            time.sleep(0.1)

    def poll(self) -> Optional[int]:
        try:
            os.kill(self.pid, 0)
            return None
        except ProcessLookupError:
            self.returncode = 0
            return 0
        except Exception:
            return None


@dataclass
class ServerConfig:
    """Backwards-compatible thin wrapper used by the original GUI status widget."""

    binary: str = DEFAULT_BINARY
    model_path: str = ""
    host: str = "127.0.0.1"
    port: int = 8080
    ctx_size: int = 16384
    n_gpu_layers: int = 99
    extra_args: list[str] = field(default_factory=lambda: ["--jinja"])

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"

    @property
    def health_url(self) -> str:
        return f"http://{self.host}:{self.port}/health"

    def to_profile(self) -> ServerProfile:
        return ServerProfile(
            name="adhoc",
            description="ad-hoc ServerConfig",
            binary=self.binary,
            model=self.model_path,
            host=self.host,
            port=self.port,
            ctx_size=self.ctx_size,
            n_gpu_layers=self.n_gpu_layers,
            extra_args=list(self.extra_args),
        )


class ServerManager:
    """Process supervisor for llama-server."""

    READY_RE = re.compile(
        r"(model loaded|all slots are idle|HTTP server listening)", re.I
    )

    def __init__(self, profile_or_config) -> None:
        if isinstance(profile_or_config, ServerProfile):
            self.profile = profile_or_config
        elif isinstance(profile_or_config, ServerConfig):
            self.profile = profile_or_config.to_profile()
        else:
            raise TypeError(f"unsupported config type: {type(profile_or_config)}")
        self._proc: Optional[subprocess.Popen] = None
        self._log: deque[str] = deque(maxlen=2000)
        self._reader_thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._returncode: Optional[int] = None
        # Async health-probe state. The UI poll loops in ServerPanel
        # and ServerStatusWidget used to call ``health(timeout=1.0)``
        # synchronously on the Qt thread; when the server is offline
        # the ``requests.get`` connect on Windows is not instantaneous
        # (loopback retries, occasional timeout-pegged waits) and the
        # event loop stuttered. ``health_nowait`` returns the last
        # cached probe result immediately and schedules a refresh on
        # a background thread, so the UI thread never blocks on I/O.
        self._health_cached: bool = False
        self._health_inflight: bool = False
        self._health_lock = threading.Lock()

    @property
    def config(self) -> ServerProfile:
        return self.profile

    # ---- public API ---------------------------------------------------------

    def is_running_externally(self) -> bool:
        return self._probe_health(timeout=1.0)

    def is_running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def health(self, timeout: float = 2.0) -> bool:
        return self._probe_health(timeout=timeout)

    def health_nowait(self, *, timeout: float = 1.0) -> bool:
        """Non-blocking variant of :meth:`health` for the Qt UI poll.

        Returns the last cached probe result instantly. If no probe
        is in flight, schedule one on a daemon thread so the next
        poll picks up the fresh value. This decouples the UI poll
        rate from the connect/timeout cost of a TCP probe against a
        port that's not listening (the offline case on Windows).
        """
        with self._health_lock:
            if not self._health_inflight:
                self._health_inflight = True
                t = threading.Thread(
                    target=self._refresh_health_bg,
                    args=(timeout,),
                    name="health-probe",
                    daemon=True,
                )
                t.start()
        return self._health_cached

    def _refresh_health_bg(self, timeout: float) -> None:
        try:
            ok = self._probe_health(timeout=timeout)
        except Exception:
            ok = False
        with self._health_lock:
            self._health_cached = ok
            self._health_inflight = False

    def start(self, *, wait_seconds: float = 90.0) -> bool:
        # Bring-your-own-server: never spawn anything; only wait for
        # the configured host:port to start answering. Useful when the
        # user is running llama-server in their own Docker container,
        # systemd service, or remote box.
        if self.profile.deployment_mode == "external":
            self._log.append(
                f"[external] expecting llama-server on "
                f"{self.profile.host}:{self.profile.port} (no process to spawn)"
            )
            return self._wait_ready(wait_seconds)

        if self.is_running_externally():
            self._ready.set()
            return True
        if self.is_running():
            return self._wait_ready(wait_seconds)

        if self.profile.deployment_mode == "docker":
            return self._start_docker(wait_seconds=wait_seconds)
        # Default: local binary.
        return self._start_local_binary(wait_seconds=wait_seconds)

    def _start_local_binary(self, *, wait_seconds: float) -> bool:
        binary = self._resolve_binary()
        if not Path(binary).exists() and shutil.which(binary) is None:
            self._log.append(
                f"[start error] llama-server binary not found: {binary}"
            )
            raise RuntimeError(f"llama-server binary not found: {binary}")
        if not self.profile.model:
            self._log.append("[start error] ServerProfile.model is empty")
            raise RuntimeError("ServerProfile.model is empty")
        if not self.profile.model_path.exists():
            self._log.append(
                f"[start error] GGUF model not found: {self.profile.model_path}"
            )
            raise RuntimeError(f"GGUF model not found: {self.profile.model_path}")

        cmd = render_command(self.profile)
        cmd[0] = binary
        env = os.environ.copy()
        with self._lock:
            self._ready.clear()
            self._log.clear()
            self._returncode = None
            self._proc = spawn_new_process_group(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                bufsize=1,
                text=True,
            )
            self._reader_thread = threading.Thread(
                target=self._reader_loop, daemon=True
            )
            self._reader_thread.start()
        _LIVE_MANAGERS.add(self)
        return self._wait_ready(wait_seconds)

    # ---- Docker deployment -----------------------------------------------

    def _docker_container_name(self) -> str:
        """Deterministic container name so ``stop()`` can find it
        even after a GUI restart (the previous run left it with the
        same preset)."""
        safe = "".join(
            c if c.isalnum() or c in "-_." else "_"
            for c in (self.profile.name or "preset")
        )
        return f"report-anonymizer-{safe}"

    def _start_docker(self, *, wait_seconds: float) -> bool:
        if shutil.which("docker") is None:
            raise RuntimeError(
                "Docker mode selected but the 'docker' CLI is not on "
                "PATH. Install Docker Desktop or the docker engine, "
                "then click Start again."
            )
        if not self.profile.docker_image:
            raise RuntimeError("ServerProfile.docker_image is empty")
        if not self.profile.model_path.exists():
            raise RuntimeError(f"GGUF model not found: {self.profile.model_path}")

        from .server_profile import MODELS_DIR

        models_root = MODELS_DIR.resolve()
        # Translate the host-side model path to the container-side
        # path. The whole models root is mounted at ``/models`` so
        # we can reuse all the per-repo subdirectories created by
        # ``hf_models.download_model``.
        try:
            rel = self.profile.model_path.resolve().relative_to(models_root)
        except ValueError:
            raise RuntimeError(
                "Docker mode requires the GGUF to live under "
                f"{models_root} so it can be mounted at /models inside "
                "the container; current model path is "
                f"{self.profile.model_path} which falls outside that "
                "tree."
            )
        container_model = f"/models/{rel.as_posix()}"

        # Make sure the image is cached locally; only pull when
        # ``docker image inspect`` says it doesn't exist. Streaming
        # the pull output to the manager log lets the GUI show
        # progress in the existing log pane.
        self._docker_ensure_image()

        # Stop any stale container with the same name (previous
        # session may have left it running if the host crashed).
        self._docker_kill_stale_container()

        cname = self._docker_container_name()
        cmd = self._render_docker_command(
            container_name=cname,
            container_model=container_model,
            host_models_root=models_root,
        )

        env = os.environ.copy()
        with self._lock:
            self._ready.clear()
            self._log.clear()
            self._returncode = None
            self._log.append("[docker] " + " ".join(cmd))
            self._proc = spawn_new_process_group(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                bufsize=1,
                text=True,
            )
            self._reader_thread = threading.Thread(
                target=self._reader_loop, daemon=True
            )
            self._reader_thread.start()
        _LIVE_MANAGERS.add(self)
        return self._wait_ready(wait_seconds)

    def _docker_ensure_image(self) -> None:
        """If the configured Docker image is not in the local cache,
        pull it; otherwise return immediately. Pull output is
        streamed into the manager log."""
        image = self.profile.docker_image
        try:
            inspect = subprocess.run(
                ["docker", "image", "inspect", image],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if inspect.returncode == 0:
                self._log.append(f"[docker] image {image!r} already cached, skipping pull")
                return
        except Exception:
            pass
        self._log.append(f"[docker] pulling {image} (first time only) …")
        try:
            pull = subprocess.Popen(
                ["docker", "pull", image],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
            )
        except Exception as e:
            raise RuntimeError(f"docker pull failed to start: {e}") from e
        if pull.stdout is not None:
            for line in pull.stdout:
                line = line.rstrip()
                if line:
                    self._log.append(f"[docker pull] {line}")
        rc = pull.wait()
        if rc != 0:
            raise RuntimeError(
                f"docker pull {image} returned exit code {rc}, see the "
                f"server log for details."
            )

    def _docker_kill_stale_container(self) -> None:
        """``docker stop`` any container left over from a previous
        session that uses our deterministic name. Best effort; if the
        container is already gone or the daemon is down we just log."""
        cname = self._docker_container_name()
        try:
            result = subprocess.run(
                ["docker", "ps", "-aq", "-f", f"name=^{cname}$"],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
            ids = (result.stdout or "").strip().splitlines()
            for cid in ids:
                if not cid:
                    continue
                self._log.append(f"[docker] removing stale container {cid}")
                subprocess.run(
                    ["docker", "rm", "-f", cid],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10.0,
                    check=False,
                )
        except Exception as e:
            self._log.append(f"[docker] stale-container sweep skipped: {e}")

    def _render_docker_command(
        self,
        *,
        container_name: str,
        container_model: str,
        host_models_root: Path,
    ) -> list[str]:
        """Build the ``docker run`` argv plus the trailing
        llama-server flags. The llama.cpp images use
        ``--server`` as the entry; everything after the image name
        is forwarded to the binary."""
        p = self.profile
        argv: list[str] = [
            "docker", "run", "--rm",
            "--name", container_name,
            "-p", f"{p.host}:{p.port}:{p.port}",
            "-v", f"{host_models_root}:/models:ro",
        ]
        if p.docker_gpu:
            argv += ["--gpus", "all"]
        argv.append(p.docker_image)
        # llama-server flags. We don't reuse render_command()
        # because that emits a host-side binary path and the
        # ``--mmap`` toggle which needs writable access to the model
        # mount; inside the container the binary is already entry-
        # pointed and the mount is read-only by design.
        argv += [
            "--host", "0.0.0.0",
            "--port", str(p.port),
            "-m", container_model,
            "-c", str(p.ctx_size),
            "--parallel", str(p.parallel),
            "-ngl", str(p.n_gpu_layers),
            "--cache-type-k", p.cache_type_k,
            "--cache-type-v", p.cache_type_v,
        ]
        # Modern llama-server takes ``--flash-attn on|off|auto`` (was
        # a bare boolean toggle in older builds). Emitting the flag
        # without a value made the next argv entry (``-b``) be parsed
        # as the value and the server crashed with "unknown value for
        # --flash-attn: '-b'". Use the explicit form on both sides.
        if p.flash_attn:
            argv += ["--flash-attn", "on"]
        else:
            argv += ["--flash-attn", "off"]
        if p.batch_size:
            argv += ["-b", str(p.batch_size)]
        if p.ubatch_size:
            argv += ["-ub", str(p.ubatch_size)]
        if p.no_warmup:
            argv.append("--no-warmup")
        # Modern llama-server has prompt caching enabled by default.
        # The legacy ``--cache-prompt`` toggle was removed and
        # ``--cache-reuse`` now takes an integer chunk-size
        # threshold, not a bool. We only need to emit anything when
        # the user explicitly disabled caching.
        if not p.cache_prompt:
            argv.append("--no-cache-prompt")
        if p.no_webui:
            argv.append("--no-webui")
        if p.chat_template:
            argv += ["--chat-template", p.chat_template]
        if p.threads:
            argv += ["-t", str(p.threads)]
        argv += list(p.extra_args or [])
        return argv

    def stop(self, *, timeout: float = 5.0) -> None:
        # External: nothing to stop on our side. The user owns the
        # server lifecycle (manual terminal, systemd, k8s, …); the
        # Stop button is documented as inactive in this mode.
        if self.profile.deployment_mode == "external":
            self._ready.clear()
            return

        # Docker: kill the named container we started (or any stale
        # one). We don't try to forward signals through Popen here
        # because ``docker run`` proxies them already, but the
        # container can still survive if the daemon dropped the
        # socket, explicit ``docker stop`` is the safe path.
        if self.profile.deployment_mode == "docker":
            cname = self._docker_container_name()
            with self._lock:
                self._proc = None
            # If the docker CLI is not on PATH at all there is nothing
            # to stop (the user is in this branch because the profile
            # was saved with deployment_mode="docker" on a machine
            # that has no Docker daemon). The early-return prevents a
            # storm of ``[docker] stop failed: [WinError 2]`` log
            # lines every time the GUI invokes stop() defensively
            # (Use / Restart / set_profile / shutdown / etc.).
            if shutil.which("docker") is None:
                self._ready.clear()
                return
            try:
                subprocess.run(
                    ["docker", "stop", "-t", str(int(timeout)), cname],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=timeout + 5.0,
                    check=False,
                )
            except FileNotFoundError:
                # Race: the CLI vanished between the which() check
                # and the spawn. Treat as a no-op, no log noise.
                pass
            except Exception as e:
                self._log.append(f"[docker] stop failed: {e}")
            self._ready.clear()
            return

        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is None:
            # The user may have started llama-server outside the GUI
            # (manual terminal launch). In that case we don't own a
            # subprocess.Popen handle but the user still expects the
            # Stop button to actually stop the server. Find whatever
            # PID is bound to the configured port and synthesise a
            # Popen-like wrapper so terminate_process_tree can do its
            # SIGTERM/SIGKILL + descendant sweep on it.
            pid = find_pid_listening_on(self.profile.host, self.profile.port)
            if pid is None:
                self._ready.clear()
                return
            terminate_process_tree(_OrphanProc(pid), timeout=timeout)
            self._ready.clear()
            return
        terminate_process_tree(proc, timeout=timeout)
        try:
            self._returncode = proc.returncode
        except Exception:
            pass
        self._ready.clear()

    def restart(self) -> bool:
        self.stop()
        time.sleep(0.5)
        return self.start()

    def tail(self, n: int = 200) -> list[str]:
        with self._lock:
            return list(self._log)[-n:]

    def diagnose_failure(self) -> Diagnosis:
        return diagnose(
            "\n".join(self.tail(200)),
            profile=self.profile,
            last_returncode=self._returncode,
        )

    def cmdline(self) -> list[str]:
        return render_command(self.profile)

    # ---- internals ----------------------------------------------------------

    def _resolve_binary(self) -> str:
        if Path(self.profile.binary).exists():
            return self.profile.binary
        which = shutil.which(self.profile.binary)
        if which:
            return which
        which = shutil.which("llama-server")
        if which:
            return which
        if Path(DEFAULT_BINARY).exists():
            return DEFAULT_BINARY
        return self.profile.binary

    def _probe_health(self, *, timeout: float) -> bool:
        try:
            r = requests.get(self.profile.health_url, timeout=timeout)
            return r.status_code == 200
        except Exception:
            return False

    def _reader_loop(self) -> None:
        proc = self._proc
        if not proc or not proc.stdout:
            return
        try:
            for line in proc.stdout:
                line = line.rstrip()
                self._log.append(line)
                if self.READY_RE.search(line):
                    self._ready.set()
        except Exception:
            pass
        finally:
            self._ready.set()
            try:
                self._returncode = proc.poll()
            except Exception:
                pass

    def _wait_ready(self, wait_seconds: float) -> bool:
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if self._probe_health(timeout=1.0):
                self._ready.set()
                return True
            if self._ready.wait(timeout=1.0):
                if self._probe_health(timeout=1.0):
                    return True
        return self._probe_health(timeout=1.0)


__all__ = ["ServerConfig", "ServerManager"]
