"""Rich server profile management for llama.cpp.

A ``ServerProfile`` is the full set of parameters required to launch a
``llama-server`` process. Profiles can ``extends:`` another profile (deep
merge with override). They can also reference a Hugging Face repo+filename
so the GUI can auto-download the model on first ``Use``.

Resolution order (most specific wins):
    1. ``config/server_profiles.yml``        (built-in, shipped)
    2. ``~/.config/document-anonymizer/server.yml``  (user global)
    3. ``<project_output>/.anon/server.yml``         (per-project override)

The profile is rendered to a final command-line via :func:`render_command`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

import yaml

from ._paths import models_dir as _models_dir, user_config_dir as _user_config_dir


CONFIG_DIR = _user_config_dir()
USER_PROFILES_PATH = CONFIG_DIR / "server.yml"
USER_PREFS_PATH = CONFIG_DIR / "preferences.yml"
MODELS_DIR = _models_dir()
DEFAULT_BINARY = os.environ.get("LLAMA_SERVER_BIN", "llama-server")
# Default Docker image for the "docker" deployment mode. The
# ``ggml-org/llama.cpp`` repo publishes ``server-cuda`` (CUDA),
# ``server`` (CPU) and ``server-vulkan`` variants. We default to
# the CUDA one since the curated presets are sized for GPUs;
# users on CPU-only hardware can switch the image in the preset
# editor.
DEFAULT_DOCKER_IMAGE = os.environ.get(
    "LLAMA_DOCKER_IMAGE", "ghcr.io/ggml-org/llama.cpp:server-cuda"
)
# Allowed deployment modes for ``ServerProfile.deployment_mode``.
# ``local_binary`` keeps the existing behaviour (Popen the binary);
# ``docker`` spawns a ``docker run`` of an llama.cpp image; and
# ``external`` skips any spawn, the user runs the server out of
# band and the GUI just connects via HTTP.
DEPLOYMENT_MODES: tuple[str, ...] = ("local_binary", "docker", "external")


@dataclass
class SamplingConfig:
    temperature: float = 0.3
    top_k: int = 20
    top_p: float = 0.8
    min_p: float = 0.0
    repeat_penalty: float = 1.0
    presence_penalty: float = 0.0


@dataclass
class ServerProfile:
    """Full superset of parameters for ``llama-server``.

    Most fields map 1:1 to a CLI flag. Optional fields default to ``None`` so
    they are not emitted unless explicitly set.
    """

    name: str
    description: str = ""
    is_builtin: bool = False
    source: str = "builtin"  # builtin | user | project

    # Runtime
    # ``deployment_mode`` picks how the GUI's Start button materialises
    # an actual llama-server process:
    #   * ``local_binary``, current default; the GUI ``Popen``s the
    #     ``binary`` path directly (requires llama-server installed
    #     locally).
    #   * ``docker``      , the GUI runs ``docker run`` on
    #     ``docker_image``, mounts the models directory at
    #     ``/models`` and exposes ``host:port``. If the image is
    #     already cached locally (``docker image inspect``) it is
    #     not re-pulled. Newbie-friendly: install Docker and click
    #     Start.
    #   * ``external``    , bring-your-own-server. The GUI never
    #     spawns anything; Start is a no-op and only ``Test`` runs
    #     the health probe against ``host:port``.
    deployment_mode: str = "local_binary"
    binary: str = DEFAULT_BINARY
    docker_image: str = DEFAULT_DOCKER_IMAGE
    docker_gpu: bool = True
    host: str = "127.0.0.1"
    port: int = 8080

    # Model
    model: str = ""
    model_repo: Optional[str] = None
    model_filename: Optional[str] = None
    # Vision/multimodal projector, kept on the dataclass for
    # round-trip compatibility with old user/project YAMLs that may
    # still carry these fields, but the pipeline is text-only and
    # ``render_command`` no longer emits ``--mmproj``.
    mmproj: Optional[str] = None
    mmproj_repo: Optional[str] = None
    mmproj_filename: Optional[str] = None

    # Performance
    parallel: int = 4
    ctx_size: int = 16384
    n_gpu_layers: int = 99
    threads: Optional[int] = None
    batch_size: int = 8192
    ubatch_size: int = 512
    flash_attn: bool = True
    mmap: bool = True
    no_warmup: bool = True
    cache_prompt: bool = True

    # Cache quantization
    cache_type_k: str = "f16"  # f16 | q8_0 | q4_0
    cache_type_v: str = "f16"

    # Long context
    rope_scaling: Optional[str] = None  # none | linear | yarn
    rope_scale: Optional[float] = None
    rope_freq_base: Optional[float] = None
    yarn_orig_ctx: Optional[int] = None
    override_kv: list[str] = field(default_factory=list)

    # Chat template
    chat_template: Optional[str] = None  # e.g. chatml
    template_file: Optional[str] = None
    jinja: bool = True

    # Misc
    no_webui: bool = True
    verbose: bool = False
    extra_args: list[str] = field(default_factory=list)

    # Sampling (sent in API requests, not CLI)
    sampling: SamplingConfig = field(default_factory=SamplingConfig)

    # ---- properties -----------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"

    @property
    def health_url(self) -> str:
        return f"http://{self.host}:{self.port}/health"

    @property
    def model_path(self) -> Path:
        """Resolved on-disk path of the main model file.

        Resolution order:

        1. The ``model:`` field literally, when the file exists at that
           path (covers the legacy flat ``MODELS_DIR/<file>.gguf``
           layout).
        2. The per-repo location ``MODELS_DIR/<repo_safe>/<filename>``
           when ``model_repo`` + ``model_filename`` are set.  This is
           where new downloads land so two repos that share a generic
           filename (notably ``mmproj-BF16.gguf``) don't collide.
        """
        configured = (
            Path(os.path.expanduser(self.model)) if self.model else Path("")
        )
        if configured and configured.exists():
            return configured
        if self.model_repo and self.model_filename:
            from .hf_models import expected_path_for

            return expected_path_for(self.model_repo, self.model_filename)
        return configured

    def is_model_present(self) -> bool:
        path = self.model_path
        if not path or str(path) == "":
            return False
        return Path(path).exists()

    # ---- serialization --------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["sampling"] = asdict(self.sampling)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ServerProfile":
        if not d.get("name"):
            raise ValueError("ServerProfile requires a 'name'")
        clean = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        sampling = clean.pop("sampling", None)
        prof = cls(**clean)
        if isinstance(sampling, dict):
            prof.sampling = SamplingConfig(
                **{k: v for k, v in sampling.items() if k in SamplingConfig.__dataclass_fields__}
            )
        return prof

    def clone(self, *, name: Optional[str] = None) -> "ServerProfile":
        d = self.to_dict()
        if name is not None:
            d["name"] = name
        d["is_builtin"] = False
        d["source"] = "user"
        return self.__class__.from_dict(d)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if (
            k in out
            and isinstance(out[k], dict)
            and isinstance(v, dict)
        ):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _resolve_extends(
    raw_profiles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Resolve ``extends:`` references between profiles."""
    by_name = {p.get("name"): p for p in raw_profiles if isinstance(p, dict)}
    resolved: dict[str, dict[str, Any]] = {}

    def resolve(name: str, stack: tuple[str, ...] = ()) -> dict[str, Any]:
        if name in resolved:
            return resolved[name]
        if name in stack:
            raise ValueError(f"extends cycle detected: {' -> '.join(stack + (name,))}")
        raw = dict(by_name.get(name) or {})
        parent_name = raw.pop("extends", None)
        if parent_name:
            parent = resolve(parent_name, stack + (name,))
            merged = _deep_merge(parent, raw)
            merged["name"] = name
        else:
            merged = raw
        resolved[name] = merged
        return merged

    out: list[dict[str, Any]] = []
    for raw in raw_profiles:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        out.append(resolve(raw["name"]))
    return out


_LEGACY_MODEL_PREFIXES: tuple[str, ...] = (
    "~/.local/share/document-anonymizer/models/",
    "~\\.local\\share\\document-anonymizer\\models\\",
)


def _reroot_legacy_model_path(value: Any) -> Any:
    """Re-root a hard-coded ``~/.local/share/document-anonymizer/models/...``
    path onto the cross-platform :func:`_paths.models_dir` location.

    This is needed for the built-in ``server_profiles.yml`` catalog whose
    ``model:`` field currently embeds the legacy XDG path. On Windows
    ``~`` expands to ``%USERPROFILE%`` but ``.local/share/...`` is XDG
    convention and the file ends up nowhere useful. We rewrite the path
    transparently so the same YAML works on Linux/macOS/Windows without
    touching the catalog file.

    Non-matching strings (and non-strings) pass through unchanged.
    """
    if not isinstance(value, str) or not value:
        return value
    for prefix in _LEGACY_MODEL_PREFIXES:
        if value.startswith(prefix):
            subpath = value[len(prefix):].lstrip("/\\")
            return str(_models_dir() / subpath)
    return value


def _load_yaml_profiles(
    path: Path, *, source_label: str
) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    profiles = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(profiles, list):
        return []
    out: list[dict[str, Any]] = []
    for p in profiles:
        if not isinstance(p, dict):
            continue
        p = dict(p)
        p.setdefault("source", source_label)
        if "model" in p:
            p["model"] = _reroot_legacy_model_path(p.get("model"))
        if "mmproj" in p:
            p["mmproj"] = _reroot_legacy_model_path(p.get("mmproj"))
        out.append(p)
    return out


def builtin_profiles_path() -> Path:
    """Path to the shipped ``config/server_profiles.yml``."""
    return Path(__file__).resolve().parent.parent / "config" / "server_profiles.yml"


def project_profiles_path(project_dir: Path) -> Path:
    return Path(project_dir) / ".anon" / "server.yml"


def _strip_stale_model_fields(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop ``model`` / ``model_repo`` / ``model_filename`` from a
    user-scope row when the path no longer exists on disk.

    These fields land in the user-scope file when an older wizard ran
    ``save_user_profile`` (full-profile snapshot) before sparse
    overrides were introduced. Once the upstream builtin's model
    changes, those snapshots keep pinning the obsolete file path and
    the user is stranded on a model they have to manually un-pin.

    Strip them only when the path doesn't exist, that guarantees we
    never overwrite a deliberate custom path the user typed in by
    hand. Deployment fields (mode / binary / docker_image / docker_gpu)
    are preserved unconditionally.
    """
    cleaned: list[dict[str, Any]] = []
    for row in rows:
        model_path = row.get("model")
        if isinstance(model_path, str) and model_path:
            try:
                if not Path(model_path).expanduser().exists():
                    row = {
                        k: v for k, v in row.items()
                        if k not in {"model", "model_repo", "model_filename"}
                    }
            except Exception:
                pass
        cleaned.append(row)
    return cleaned


def load_profiles(
    *, project_dir: Optional[Path] = None
) -> list[ServerProfile]:
    """Load and merge built-in + user + project profiles, in order.

    Profiles with the same ``name`` are overridden by the next layer.
    Returns a list of fully-resolved :class:`ServerProfile`.
    """
    user_rows = _strip_stale_model_fields(
        _load_yaml_profiles(USER_PROFILES_PATH, source_label="user")
    )
    # Legacy alias: the Windows installer used to seed a profile named
    # ``installer-default`` that extended ``default``. That produced
    # two entries in the wizard's preset list (``default`` + the
    # alias) for what is the same model, only with the installer's
    # deployment-mode override. Rename it back to ``default`` at load
    # time so the deep-merge below collapses the override onto the
    # builtin row and the user sees a single ``default`` entry.
    # New-style installers already write ``name: default``, so this
    # rename is a no-op for them.
    for row in user_rows:
        if not isinstance(row, dict):
            continue
        if row.get("name") == "installer-default":
            row["name"] = "default"
            row.pop("extends", None)
            # Translate the installer's ``llama_path`` into the
            # standard ``binary`` field so the profile carries the
            # exact path of the bundled llama-server.exe (Setup may
            # write either key depending on its version).
            llama_path = row.pop("llama_path", None)
            if isinstance(llama_path, str) and llama_path and not row.get("binary"):
                row["binary"] = llama_path
    layers: list[list[dict[str, Any]]] = [
        _load_yaml_profiles(builtin_profiles_path(), source_label="builtin"),
        user_rows,
    ]
    if project_dir:
        layers.append(_strip_stale_model_fields(
            _load_yaml_profiles(
                project_profiles_path(project_dir), source_label="project"
            )
        ))

    # Layered merge by name BEFORE resolving ``extends:``. Earlier the
    # order was reversed, ``_resolve_extends`` deduplicated multiple
    # ``default`` rows by name (last wins) and the builtin's row was
    # discarded before the merge step ever saw it. With sparse user
    # overrides that meant fields the user-scope dropped (model_repo,
    # model_filename, model) never came back from the builtin.
    by_name: dict[str, dict[str, Any]] = {}
    for layer in layers:
        for row in layer:
            name = row.get("name")
            if not name:
                continue
            prev = by_name.get(name) or {}
            by_name[name] = _deep_merge(prev, row)
    unique_rows = list(by_name.values())

    resolved = _resolve_extends(unique_rows)

    profiles: list[ServerProfile] = []
    for r in resolved:
        try:
            profiles.append(ServerProfile.from_dict(r))
        except Exception:
            continue
    profiles.sort(key=lambda p: p.name)
    return profiles


def get_profile(
    name: str, *, project_dir: Optional[Path] = None
) -> Optional[ServerProfile]:
    for p in load_profiles(project_dir=project_dir):
        if p.name == name:
            return p
    return None


def _read_prefs() -> dict[str, Any]:
    if not USER_PREFS_PATH.exists():
        return {}
    try:
        data = yaml.safe_load(USER_PREFS_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_prefs(data: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    USER_PREFS_PATH.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def get_default_profile_name() -> Optional[str]:
    """Name of the user's preferred default preset, or ``None`` if unset."""
    name = _read_prefs().get("default_profile")
    return str(name) if isinstance(name, str) and name else None


def set_default_profile_name(name: Optional[str]) -> None:
    """Persist ``name`` as the user's preferred default preset.

    Pass ``None`` to clear the preference and fall back to the legacy
    behaviour (the preset literally named ``default``, or the first one
    found).
    """
    prefs = _read_prefs()
    if name:
        prefs["default_profile"] = name
    else:
        prefs.pop("default_profile", None)
    _write_prefs(prefs)


def get_default_profile(
    *, project_dir: Optional[Path] = None
) -> Optional[ServerProfile]:
    """Resolve the active default preset, honouring the user preference.

    Resolution order:
      1. ``preferences.yml: default_profile`` if set and the preset exists.
      2. The preset literally named ``default``.
      3. The first profile returned by :func:`load_profiles`.
    """
    profiles = load_profiles(project_dir=project_dir)
    by_name = {p.name: p for p in profiles}
    pref = get_default_profile_name()
    if pref and pref in by_name:
        return by_name[pref]
    if "default" in by_name:
        return by_name["default"]
    return profiles[0] if profiles else None


def save_user_profile(profile: ServerProfile) -> None:
    """Save a profile to the user-global config (creates/updates)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if USER_PROFILES_PATH.exists():
        try:
            data = yaml.safe_load(USER_PROFILES_PATH.read_text(encoding="utf-8")) or {}
            existing = list(data.get("profiles") or [])
        except Exception:
            existing = []
    out = [p for p in existing if p.get("name") != profile.name]
    payload = profile.to_dict()
    payload.pop("source", None)
    payload.pop("is_builtin", None)
    out.append(payload)
    USER_PROFILES_PATH.write_text(
        yaml.safe_dump(
            {"version": 1, "profiles": out}, sort_keys=False, allow_unicode=True
        ),
        encoding="utf-8",
    )


_DEPLOYMENT_OVERRIDE_KEYS = (
    "deployment_mode",
    "binary",
    "docker_image",
    "docker_gpu",
)


def save_user_deployment_override(
    name: str,
    *,
    deployment_mode: Optional[str] = None,
    binary: Optional[str] = None,
    docker_image: Optional[str] = None,
    docker_gpu: Optional[bool] = None,
) -> None:
    """Write a SPARSE user-scope override carrying only deployment-mode
    related fields.

    ``save_user_profile`` writes the entire profile (including model
    paths, ctx_size, sampling, …) which freezes a snapshot of the
    builtin at the time of the call. When the upstream builtin changes
    (e.g. the default model file is bumped) that snapshot keeps
    pinning the old definition and the user is stranded on the old
    config until they manually delete the user-scope file.

    This helper writes only the deployment-mode fields the wizard
    actually decides on. The profile loader's deep-merge layers it
    over the builtin so model_repo / model_filename / model / sampling
    keep tracking upstream.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if USER_PROFILES_PATH.exists():
        try:
            data = yaml.safe_load(USER_PROFILES_PATH.read_text(encoding="utf-8")) or {}
            existing = list(data.get("profiles") or [])
        except Exception:
            existing = []
    # Strip any prior FULL override for this name so we don't keep
    # leaking the snapshotted model paths.
    out = [p for p in existing if p.get("name") != name]
    payload: dict[str, Any] = {"name": name}
    if deployment_mode is not None:
        payload["deployment_mode"] = deployment_mode
    if binary is not None:
        payload["binary"] = binary
    if docker_image is not None:
        payload["docker_image"] = docker_image
    if docker_gpu is not None:
        payload["docker_gpu"] = docker_gpu
    # Don't write a row that says nothing more than the name.
    if len(payload) > 1:
        out.append(payload)
    USER_PROFILES_PATH.write_text(
        yaml.safe_dump(
            {"version": 1, "profiles": out}, sort_keys=False, allow_unicode=True
        ),
        encoding="utf-8",
    )


def save_project_profile(profile: ServerProfile, project_dir: Path) -> None:
    """Save a profile to the per-project override."""
    target = project_profiles_path(project_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if target.exists():
        try:
            data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
            existing = list(data.get("profiles") or [])
        except Exception:
            existing = []
    out = [p for p in existing if p.get("name") != profile.name]
    payload = profile.to_dict()
    payload.pop("source", None)
    payload.pop("is_builtin", None)
    out.append(payload)
    target.write_text(
        yaml.safe_dump(
            {"version": 1, "profiles": out}, sort_keys=False, allow_unicode=True
        ),
        encoding="utf-8",
    )


def delete_user_profile(name: str) -> bool:
    if not USER_PROFILES_PATH.exists():
        return False
    try:
        data = yaml.safe_load(USER_PROFILES_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return False
    existing = list(data.get("profiles") or [])
    out = [p for p in existing if p.get("name") != name]
    if len(out) == len(existing):
        return False
    USER_PROFILES_PATH.write_text(
        yaml.safe_dump(
            {"version": 1, "profiles": out}, sort_keys=False, allow_unicode=True
        ),
        encoding="utf-8",
    )
    return True


def delete_project_profile(name: str, project_dir: Path) -> bool:
    """Counterpart to :func:`delete_user_profile` for the per-project
    override file (``<project>/.anon/server.yml``)."""
    target = project_profiles_path(project_dir)
    if not target.exists():
        return False
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except Exception:
        return False
    existing = list(data.get("profiles") or [])
    out = [p for p in existing if p.get("name") != name]
    if len(out) == len(existing):
        return False
    target.write_text(
        yaml.safe_dump(
            {"version": 1, "profiles": out}, sort_keys=False, allow_unicode=True
        ),
        encoding="utf-8",
    )
    return True


def render_command(profile: ServerProfile) -> list[str]:
    """Build the exact ``llama-server`` invocation for ``profile``.

    The order of flags follows what llama.cpp expects (the few flags that
    must come last - like ``--override-kv`` and ``extra_args`` - are appended
    at the end).
    """
    cmd: list[str] = [profile.binary]
    # Use the resolved ``model_path`` so the launched process sees the
    # actual on-disk location: that's the per-repo subdirectory for
    # newly downloaded files, or the legacy flat path for older installs.
    resolved_model = profile.model_path
    if resolved_model and str(resolved_model):
        cmd += ["-m", str(resolved_model)]
    elif profile.model:
        cmd += ["-m", os.path.expanduser(profile.model)]
    cmd += ["--host", profile.host, "--port", str(profile.port)]
    cmd += ["--ctx-size", str(profile.ctx_size)]
    cmd += ["--parallel", str(max(1, profile.parallel))]
    cmd += ["--n-gpu-layers", str(profile.n_gpu_layers)]
    if profile.threads:
        cmd += ["-t", str(profile.threads)]
    cmd += ["-b", str(profile.batch_size), "-ub", str(profile.ubatch_size)]
    if profile.flash_attn:
        cmd += ["-fa", "on"]
    if profile.mmap:
        cmd += ["--mmap"]
    if profile.no_warmup:
        cmd += ["--no-warmup"]
    if profile.cache_prompt:
        cmd += ["--cache-prompt"]
    if profile.no_webui:
        cmd += ["--no-webui"]
    if profile.verbose:
        cmd += ["--verbose"]

    if profile.cache_type_k and profile.cache_type_k != "f16":
        cmd += ["-ctk", profile.cache_type_k]
    if profile.cache_type_v and profile.cache_type_v != "f16":
        cmd += ["-ctv", profile.cache_type_v]

    if profile.rope_scaling:
        cmd += ["--rope-scaling", profile.rope_scaling]
    if profile.rope_scale is not None:
        cmd += ["--rope-scale", str(profile.rope_scale)]
    if profile.rope_freq_base is not None:
        cmd += ["--rope-freq-base", str(profile.rope_freq_base)]
    if profile.yarn_orig_ctx is not None:
        cmd += ["--yarn-orig-ctx", str(profile.yarn_orig_ctx)]

    if profile.chat_template:
        cmd += ["--chat-template", profile.chat_template]
    if profile.template_file:
        cmd += ["--chat-template-file", os.path.expanduser(profile.template_file)]
    if profile.jinja:
        cmd += ["--jinja"]

    # ``--mmproj`` intentionally not emitted: the pipeline is text-only
    # (``LLMClient`` drops the image_url path), so a vision projector
    # would cost extra VRAM for nothing.

    for ov in profile.override_kv or []:
        cmd += ["--override-kv", ov]

    for x in profile.extra_args or []:
        if isinstance(x, str) and x:
            cmd.append(x)

    return cmd


__all__ = [
    "ServerProfile",
    "SamplingConfig",
    "MODELS_DIR",
    "CONFIG_DIR",
    "USER_PROFILES_PATH",
    "USER_PREFS_PATH",
    "DEFAULT_BINARY",
    "DEFAULT_DOCKER_IMAGE",
    "DEPLOYMENT_MODES",
    "get_default_profile",
    "get_default_profile_name",
    "set_default_profile_name",
    "load_profiles",
    "get_profile",
    "save_user_profile",
    "save_user_deployment_override",
    "save_project_profile",
    "delete_user_profile",
    "delete_project_profile",
    "render_command",
    "builtin_profiles_path",
    "project_profiles_path",
]
