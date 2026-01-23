import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List

import yaml


def _expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def _base_dir_from_config_path(config_path: Path) -> Path:
    """
    Determine the "project base dir" used to resolve relative paths.

    - If config is .../config/config.yaml, treat base as the parent of config/
    - Otherwise treat base as the directory containing the config file
    """
    config_dir = config_path.parent
    if config_dir.name.lower() == "config":
        return config_dir.parent
    return config_dir


def discover_config_path(explicit: Optional[str] = None) -> Path:
    candidates: List[Path] = []

    if explicit and str(explicit).strip():
        candidates.append(_expand_path(str(explicit).strip()))

    env_cfg = os.environ.get("RACKBRAIN_CONFIG", "").strip()
    if env_cfg:
        candidates.append(_expand_path(env_cfg))

    home = os.environ.get("RACKBRAIN_HOME", "").strip()
    if home:
        home_path = _expand_path(home)
        candidates.append(home_path / "config" / "config.yaml")

    # Repo-style default when running from within a checkout
    candidates.append(Path.cwd() / "config" / "config.yaml")

    # XDG-ish default
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        candidates.append(_expand_path(xdg) / "rackbrain" / "config.yaml")
    candidates.append(Path.home() / ".config" / "rackbrain" / "config.yaml")

    for path in candidates:
        try:
            if path.exists() and path.is_file():
                return path
        except OSError:
            continue

    tried = "\n".join([f"  - {p}" for p in candidates])
    raise FileNotFoundError(
        "RackBrain config not found. Provide `--config`, set $RACKBRAIN_CONFIG, or create one of:\n"
        f"{tried}\n\n"
        "Tip: copy `config/config.example.yaml` to `config/config.yaml`."
    )


def load_config(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config file must contain a YAML mapping: {path}")
    return data


def _resolve_path(base_dir: Path, maybe_path: Any) -> Any:
    if not isinstance(maybe_path, str):
        return maybe_path
    s = maybe_path.strip()
    if not s:
        return maybe_path
    p = Path(os.path.expandvars(os.path.expanduser(s)))
    if p.is_absolute():
        return str(p)
    return str((base_dir / p).resolve())


def normalize_config(config: Dict[str, Any], *, config_path: Path) -> Tuple[Dict[str, Any], Path]:
    """
    Normalize config for portability:
    - Resolve `rules.files` to absolute paths
    - Resolve logging/state paths relative to a stable base dir (not the caller's CWD)
    - Inject `paths.state_dir` if missing
    """
    home_override = os.environ.get("RACKBRAIN_HOME", "").strip()
    base_dir = _expand_path(home_override) if home_override else _base_dir_from_config_path(config_path)

    cfg: Dict[str, Any] = dict(config or {})

    # Internal metadata (safe to ignore)
    cfg.setdefault("__rackbrain", {})
    if isinstance(cfg["__rackbrain"], dict):
        cfg["__rackbrain"]["config_path"] = str(config_path)
        cfg["__rackbrain"]["base_dir"] = str(base_dir)

    # Rules
    rules = dict(cfg.get("rules", {}) or {})
    files = rules.get("files", []) or []
    if isinstance(files, list):
        rules["files"] = [_resolve_path(base_dir, f) for f in files]
    cfg["rules"] = rules

    # Logging
    logging_cfg = dict(cfg.get("logging", {}) or {})
    log_dir = os.environ.get("RACKBRAIN_LOG_DIR", "").strip() or logging_cfg.get("log_dir", "logs")
    logging_cfg["log_dir"] = _resolve_path(base_dir, log_dir)
    cfg["logging"] = logging_cfg

    # State / timers (used by polling + timer_store)
    paths_cfg = dict(cfg.get("paths", {}) or {})
    state_dir = (
        (paths_cfg.get("state_dir") if isinstance(paths_cfg.get("state_dir"), str) else "")
        or os.environ.get("RACKBRAIN_STATE_DIR", "").strip()
        or (os.path.join(os.environ.get("RACKBRAIN_HOME", "").strip(), "state") if os.environ.get("RACKBRAIN_HOME", "").strip() else "")
        or "state"
    )
    paths_cfg["state_dir"] = _resolve_path(base_dir, state_dir)
    cfg["paths"] = paths_cfg

    processing_cfg = dict(cfg.get("processing", {}) or {})
    timer_db_path = processing_cfg.get("timer_db_path") or os.environ.get("RACKBRAIN_TIMER_DB_PATH", "").strip()
    if not str(timer_db_path).strip():
        timer_db_path = os.path.join(str(paths_cfg["state_dir"]), "rackbrain_state.sqlite")
    processing_cfg["timer_db_path"] = _resolve_path(base_dir, str(timer_db_path))
    cfg["processing"] = processing_cfg

    return cfg, base_dir


def load_app_config(explicit_path: Optional[str] = None) -> Tuple[Dict[str, Any], Path, Path]:
    """
    Returns: (config_dict, config_path, base_dir)
    """
    config_path = discover_config_path(explicit_path)
    cfg = load_config(config_path)
    cfg, base_dir = normalize_config(cfg, config_path=config_path)
    return cfg, config_path, base_dir
