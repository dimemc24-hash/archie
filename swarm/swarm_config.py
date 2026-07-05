"""
swarm/swarm_config.py — profile-driven configuration resolution for the generic
Stage-3 swarm lane.

Pure Python (stdlib only). No network. Designed for zero-network unit tests.

Two layers of config:
  1. profiles/<name>.json (DO-side) — workspace, repo_url, swarm section
  2. .swarm.json at the TARGET repo root (self-describing) — scope, deps,
     verify, routes. The config travels with the code that reads it, so any
     repo becomes swarm-able by adding one file, and there is no cross-box
     configuration drift.

resolve_transport_plan() merges both into a single TransportPlan that
transport.sh consumes (via a --dry-run printout or real execution).

Legacy fallback: when no profile is given (the NextChapter default), returns
None for swarm-specific fields, signaling transport.sh to use the exact
hardcoded paths and the live runner — zero regression.

.swarm.json fallback: when a profile IS given but the target repo has no
.swarm.json, the resolver falls back to hardcoded defaults (matching the
NextChapter scope/deps/verify shape) AND emits a clear warning to stderr.
This prevents the "silent fallback danger" blindspot — a missing .swarm.json
must never mask a missing verify step or wrong deps and produce a false-green
pipeline.
"""
from __future__ import annotations

import json
import os
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


HARNESS = os.path.expanduser("~/harness")
# HARNESS_PROFILES_DIR override allows testing against the repo's profiles/ dir
# without touching the live ~/harness/profiles/ state.
PROFILES_DIR = os.environ.get("HARNESS_PROFILES_DIR", os.path.join(HARNESS, "profiles"))
SWARM_DIR = Path(__file__).resolve().parent
SWARM_REPOS_DIR = SWARM_DIR / "swarm-repos"  # kept for backward compat / drift tooling

# .swarm.json schema version. The runner only reads fields it knows about
# from this version. Older files (version < CURRENT) are accepted with a
# deprecation warning; newer files (version > CURRENT) are rejected to
# prevent silent misinterpretation (schema evolution blindspot).
SWARM_CONFIG_VERSION = 1

# Legacy defaults (NextChapter) — used when no profile is given.
LEGACY_REPO = os.path.join(HARNESS, "repo")
LEGACY_HETZNER_REMOTE = "hetzner-swarm:swarm/newchapter"
LEGACY_RUNNER = "live"  # ~/swarm/run_swarm.sh

# Hardcoded fallback defaults used when .swarm.json is absent in a profile repo.
# These mirror the NextChapter live runner's hardcoded behavior so that a repo
# without .swarm.json still gets a sensible (if potentially wrong-for-this-repo)
# swarm pass — BUT a warning is always emitted so the omission is never silent.
FALLBACK_SCOPE_PATTERN = r"\.(ts|tsx)$"
FALLBACK_EXCLUDE_PATTERN = r"(\.test\.|/__tests__/|^_harness/|\.d\.ts$)"
FALLBACK_LOCKFILE = "package-lock.json"
FALLBACK_DEPS_STEP = "npm ci --legacy-peer-deps"
FALLBACK_DEV_SERVER_CMD = "npm run dev"
FALLBACK_DEV_SERVER_HEALTH = "http://localhost:3000"
FALLBACK_VERIFY_CMD = "npx tsc --noEmit"


@dataclass
class RepoConfig:
    """Per-repo swarm configuration (from .swarm.json at the target repo root).

    When .swarm.json is absent, the resolver builds a RepoConfig from
    FALLBACK_* defaults and sets `used_fallback=True` so callers can emit a
    clear warning (silent-fallback blindspot).
    """
    name: str = ""
    hetzner_repo_path: str = ""
    scope_pattern: str = FALLBACK_SCOPE_PATTERN
    exclude_pattern: str = FALLBACK_EXCLUDE_PATTERN
    lockfile: str = FALLBACK_LOCKFILE
    deps_step: str = FALLBACK_DEPS_STEP
    dev_server_cmd: str = FALLBACK_DEV_SERVER_CMD
    dev_server_health: str = FALLBACK_DEV_SERVER_HEALTH
    verify_cmd: str = FALLBACK_VERIFY_CMD
    routes: str = ""
    used_fallback: bool = False  # True when .swarm.json was missing
    config_version: int = 0      # schema version from .swarm.json (0 = fallback)
    config_source: str = ""      # path to the .swarm.json that was loaded


@dataclass
class ProfileConfig:
    """DO-side profile configuration (from profiles/<name>.json)."""
    name: str = ""
    repo_url: str = ""
    workspace: str = ""
    notes: str = ""
    swarm: dict = field(default_factory=dict)


@dataclass
class TransportPlan:
    """Merged transport plan — what transport.sh needs to execute."""
    run_id: str = ""
    routes: str = ""
    waves: int = 3
    # DO-side
    do_repo: str = ""
    # Hetzner-side
    hetzner_repo_path: str = ""         # e.g. "swarm/archie"
    hetzner_remote: str = ""            # e.g. "hetzner-swarm:swarm/archie"
    runner: str = "live"                 # "live" or "generic"
    runner_script: str = ""             # SSH command to invoke
    repo_name: str = ""                 # for generic: the repo name
    config_path: str = ""               # for generic: ".swarm.json" (resolved at checkout)
    bootstrap: bool = False             # init repo on hetzner if missing
    # Resolved repo config (for generic runner)
    repo_config: Optional[RepoConfig] = None
    # Legacy flag
    is_legacy: bool = True
    # True when .swarm.json was missing and fallback defaults were used
    config_fallback: bool = False


def load_profile(profile: str | None) -> Optional[ProfileConfig]:
    """Load a DO-side profile. Returns None for no profile (legacy/NextChapter)."""
    if not profile:
        return None
    cfg_path = os.path.join(PROFILES_DIR, f"{profile}.json")
    if not os.path.isfile(cfg_path):
        raise FileNotFoundError(f"profile not found: {cfg_path}")
    data = json.load(open(cfg_path))
    return ProfileConfig(
        name=data.get("name", profile),
        repo_url=data.get("repo_url", ""),
        workspace=data.get("workspace", ""),
        notes=data.get("notes", ""),
        swarm=data.get("swarm", {}),
    )


def load_swarm_config(repo_root: str | Path) -> RepoConfig:
    """Load .swarm.json from the target repo root.

    Reads the self-describing config that travels with the code. Validates the
    schema version (schema evolution blindspot): older versions are accepted
    with a deprecation warning; newer versions are rejected.

    Raises FileNotFoundError if .swarm.json is absent (caller decides whether
    to fall back to hardcoded defaults).
    """
    cfg_path = Path(repo_root) / ".swarm.json"
    if not cfg_path.is_file():
        raise FileNotFoundError(f".swarm.json not found at {cfg_path}")
    data = json.load(open(cfg_path))
    version = data.get("version", 1)
    if version > SWARM_CONFIG_VERSION:
        raise ValueError(
            f".swarm.json version {version} is newer than supported "
            f"(max {SWARM_CONFIG_VERSION}) at {cfg_path}. "
            f"Upgrade the swarm runner or downgrade the config."
        )
    if version < SWARM_CONFIG_VERSION:
        warnings.warn(
            f".swarm.json version {version} is outdated (current is "
            f"{SWARM_CONFIG_VERSION}) at {cfg_path}. Some fields may use "
            f"defaults. Update the config when convenient.",
            DeprecationWarning,
            stacklevel=2,
        )
    name = data.get("name", "")
    return RepoConfig(
        name=name,
        hetzner_repo_path=data.get("hetzner_repo_path", f"swarm/{name}" if name else ""),
        scope_pattern=data.get("scope_pattern", FALLBACK_SCOPE_PATTERN),
        exclude_pattern=data.get("exclude_pattern", FALLBACK_EXCLUDE_PATTERN),
        lockfile=data.get("lockfile", FALLBACK_LOCKFILE),
        deps_step=data.get("deps_step", FALLBACK_DEPS_STEP),
        dev_server_cmd=data.get("dev_server_cmd", FALLBACK_DEV_SERVER_CMD),
        dev_server_health=data.get("dev_server_health", FALLBACK_DEV_SERVER_HEALTH),
        verify_cmd=data.get("verify_cmd", FALLBACK_VERIFY_CMD),
        routes=data.get("routes", ""),
        used_fallback=False,
        config_version=version,
        config_source=str(cfg_path),
    )


def fallback_repo_config(repo_name: str) -> RepoConfig:
    """Build a RepoConfig from hardcoded FALLBACK_* defaults.

    Used when .swarm.json is absent in a profile repo. The caller MUST emit a
    warning when using this — see resolve_transport_plan().
    """
    return RepoConfig(
        name=repo_name,
        hetzner_repo_path=f"swarm/{repo_name}" if repo_name else "",
        scope_pattern=FALLBACK_SCOPE_PATTERN,
        exclude_pattern=FALLBACK_EXCLUDE_PATTERN,
        lockfile=FALLBACK_LOCKFILE,
        deps_step=FALLBACK_DEPS_STEP,
        dev_server_cmd=FALLBACK_DEV_SERVER_CMD,
        dev_server_health=FALLBACK_DEV_SERVER_HEALTH,
        verify_cmd=FALLBACK_VERIFY_CMD,
        routes="",
        used_fallback=True,
        config_version=0,
        config_source="(fallback — no .swarm.json)",
    )


def load_repo_config(repo_name: str, repos_dir: Path | None = None) -> RepoConfig:
    """Load a per-repo swarm config.

    Resolution order (decision: option c — .swarm.json at repo root):
      1. Try .swarm.json at the DO-side workspace root (the profile's workspace).
      2. If absent, fall back to hardcoded defaults AND emit a warning.

    The legacy swarm-repos/<name>.json path is no longer the primary source;
    it remains available for the drift/audit tooling only.
    """
    # Try .swarm.json from the profile workspace
    workspace = os.path.expanduser(f"~/harness/workspaces/{repo_name}/repo")
    try:
        return load_swarm_config(workspace)
    except FileNotFoundError:
        pass
    # Fallback: hardcoded defaults with warning
    rc = fallback_repo_config(repo_name)
    _emit_fallback_warning(repo_name, rc.verify_cmd)
    return rc


def _emit_fallback_warning(repo_name: str, verify_cmd: str) -> None:
    """Emit a clear warning when falling back to hardcoded defaults.

    Addresses the 'silent fallback danger' blindspot: a missing .swarm.json
    must never mask a missing verify step or wrong deps and produce a
    false-green pipeline.
    """
    msg = (
        f"[swarm_config] WARNING: no .swarm.json found for repo '{repo_name}'. "
        f"Falling back to hardcoded defaults (scope=.ts/.tsx, deps=npm ci, "
        f"verify='{verify_cmd}'). These defaults may be WRONG for this repo. "
        f"Add a .swarm.json at the repo root to silence this warning and "
        f"ensure correct swarm behavior."
    )
    print(msg, file=sys.stderr)


def resolve_transport_plan(
    run_id: str,
    profile: str | None = None,
    routes: str = "",
    waves: int = 3,
    repos_dir: Path | None = None,
    profiles_dir: str | None = None,
    workspace_override: str | None = None,
) -> TransportPlan:
    """Resolve the full transport plan for a run.

    With no profile: returns the legacy NextChapter plan (live runner, hardcoded paths).
    With a profile: resolves the DO-side repo, Hetzner target, and runner config.

    For the generic runner, repo config is read from .swarm.json at the target
    repo root (workspace). If .swarm.json is absent, falls back to hardcoded
    defaults AND emits a clear warning (silent-fallback blindspot).
    """
    plan = TransportPlan(
        run_id=run_id,
        routes=routes,
        waves=waves,
    )

    prof = load_profile(profile) if profile else None

    if prof is None:
        # Legacy: NextChapter, live runner, exact hardcoded paths
        plan.do_repo = LEGACY_REPO
        plan.hetzner_repo_path = "swarm/newchapter"
        plan.hetzner_remote = LEGACY_HETZNER_REMOTE
        plan.runner = "live"
        plan.runner_script = f"bash $HOME/swarm/run_swarm.sh"
        plan.is_legacy = True
        return plan

    # Profile-driven
    plan.do_repo = os.path.expanduser(prof.workspace)
    plan.is_legacy = False

    swarm_cfg = prof.swarm
    if not swarm_cfg:
        # Profile exists but has no swarm section — fall back to legacy behavior
        # with the profile's workspace (so at least the DO-side repo is correct).
        plan.hetzner_repo_path = f"swarm/{prof.name}"
        plan.hetzner_remote = f"hetzner-swarm:swarm/{prof.name}"
        plan.runner = "live"
        plan.runner_script = f"bash $HOME/swarm/run_swarm.sh"
        plan.repo_name = prof.name
        return plan

    repo_name = swarm_cfg.get("repo_name", prof.name)
    hetzner_path = swarm_cfg.get("hetzner_repo_path", f"swarm/{repo_name}")
    runner = swarm_cfg.get("runner", "generic")
    bootstrap = swarm_cfg.get("bootstrap", True)

    plan.hetzner_repo_path = hetzner_path
    plan.hetzner_remote = f"hetzner-swarm:{hetzner_path}"
    plan.runner = runner
    plan.repo_name = repo_name
    plan.bootstrap = bootstrap

    if runner == "generic":
        plan.runner_script = (
            f"bash $HOME/swarm/generic/run_swarm_generic.sh "
            f"'{repo_name}' 'build/{run_id}' '{routes}' '{waves}'"
        )
        # Config is .swarm.json at the repo root — travels with the checkout
        plan.config_path = ".swarm.json"
        # Resolve the repo config from the DO-side workspace (for dry-run display)
        workspace = workspace_override or plan.do_repo
        try:
            plan.repo_config = load_swarm_config(workspace)
            plan.config_fallback = False
        except FileNotFoundError:
            # .swarm.json absent — fall back to hardcoded defaults + WARNING
            plan.repo_config = fallback_repo_config(repo_name)
            plan.config_fallback = True
            _emit_fallback_warning(repo_name, plan.repo_config.verify_cmd)
    else:
        plan.runner_script = f"bash $HOME/swarm/run_swarm.sh"

    return plan


def format_plan(plan: TransportPlan) -> str:
    """Format a transport plan as a human-readable string (for --dry-run)."""
    lines = [
        f"[transport] DRY-RUN PLAN for {plan.run_id}",
        f"  profile:     {'(none — legacy NextChapter)' if plan.is_legacy else plan.repo_name}",
        f"  runner:      {plan.runner}",
        f"  DO repo:     {plan.do_repo}",
        f"  Hetzner repo: {plan.hetzner_repo_path}",
        f"  Hetzner remote: {plan.hetzner_remote}",
        f"  routes:      '{plan.routes}'",
        f"  waves:       {plan.waves}",
        f"  bootstrap:   {plan.bootstrap}",
    ]
    if plan.runner == "generic":
        lines.append(f"  config:      {plan.config_path}")
        if plan.repo_config:
            rc = plan.repo_config
            if plan.config_fallback or rc.used_fallback:
                lines.append(f"  ⚠ FALLBACK:  no .swarm.json — using hardcoded defaults (may be WRONG)")
            lines.extend([
                f"  --- repo config ---",
                f"  scope:       {rc.scope_pattern}",
                f"  exclude:     {rc.exclude_pattern}",
                f"  lockfile:    {rc.lockfile or '(none)'}",
                f"  deps_step:   {rc.deps_step}",
                f"  dev_server:  {rc.dev_server_cmd or '(none — vision lane skipped)'}",
                f"  verify_cmd:  {rc.verify_cmd or '(none)'}",
            ])
            if rc.config_version:
                lines.append(f"  cfg_version: {rc.config_version}")
            if rc.config_source:
                lines.append(f"  cfg_source:  {rc.config_source}")
        else:
            lines.append(f"  config:      MISSING — {plan.config_path} not found")
    lines.append(f"  runner_cmd:  ssh hetzner-swarm \"{plan.runner_script}\"")
    return "\n".join(lines)


__all__ = [
    "RepoConfig", "ProfileConfig", "TransportPlan",
    "load_profile", "load_repo_config", "load_swarm_config",
    "fallback_repo_config", "resolve_transport_plan", "format_plan",
    "SWARM_CONFIG_VERSION",
]
