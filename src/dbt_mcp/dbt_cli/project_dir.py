"""Per-call project directory resolution and validation (Part 1 contract).

Contract (normative):
- ``_resolve_project_dir(config, project_dir)`` picks the effective directory
  for a single tool call: an explicit non-empty ``project_dir`` argument wins
  over the startup-configured ``config.project_dir``; ``None``/empty/
  whitespace-only values fall back to the configured value. ``~`` and env
  vars (``$HOME``/``${X}``) are expanded and relative paths are made absolute
  via :meth:`pathlib.Path.resolve` against the server cwd. The RESOLVED value
  is validated at call time; failures raise :class:`InvalidParameterError`
  (never a bare string). When both the argument and the configured value are
  omitted/blank, raises ``InvalidParameterError("No project directory: ...)``.
  Never returns ``None``.
- ``_validate_project_dir(path)`` validates the RESOLVED value, in order:
  exists else ``"project_dir does not exist: <path>"``; is-dir else
  ``"project_dir is not a directory: <path>"``; ``<dir>/dbt_project.yml``
  exists else ``"project_dir is not a dbt project (missing dbt_project.yml):
  <path>"``. Symlinks are allowed (``resolve()`` follows them); the return
  value is always absolute post-resolution.

Cloud CLI contract: validation is IDENTICAL for ``DBT_CLOUD_CLI`` (a local
project dir is still required so manifests can switch per call); the only
Cloud-specific difference lives in the subprocess layer (Part 3), where
``cwd`` is ALWAYS ``None`` for ``DBT_CLOUD_CLI``. ``--state`` remains
rejected for Cloud CLI.

Purely additive: no validation runs at import/startup time, only when these
helpers are called (wired into tool calls in Part 3).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dbt_mcp.errors.common import InvalidParameterError

if TYPE_CHECKING:
    from dbt_mcp.config.config import DbtCliConfig

_NO_PROJECT_DIR_MESSAGE = (
    "No project directory: pass project_dir or set DBT_PROJECT_DIR"
)


def _validate_project_dir(path: str) -> str:
    """Validate a (possibly unexpanded/relative) candidate dir.

    Expands ``~``/env vars, resolves to an absolute path (following
    symlinks), then checks existence, is-dir, and ``dbt_project.yml``
    presence. Returns the resolved absolute path.
    """
    resolved = str(Path(os.path.expandvars(os.path.expanduser(path))).resolve())
    candidate = Path(resolved)
    if not candidate.exists():
        raise InvalidParameterError(f"project_dir does not exist: {resolved}")
    if not candidate.is_dir():
        raise InvalidParameterError(f"project_dir is not a directory: {resolved}")
    if not (candidate / "dbt_project.yml").is_file():
        raise InvalidParameterError(
            f"project_dir is not a dbt project (missing dbt_project.yml): {resolved}"
        )
    return resolved


def _resolve_project_dir(config: DbtCliConfig | Any, project_dir: str | None) -> str:
    """Resolve the effective project dir for one tool call.

    Explicit non-empty ``project_dir`` wins; ``None``/empty/whitespace-only
    falls back to ``config.project_dir`` (also blank-tolerant, since config
    becomes optional in Part 2). Both blank => ``InvalidParameterError``.
    The winner is expanded/resolved and validated via
    :func:`_validate_project_dir`; never returns ``None``.
    """
    candidate: str | None = None
    if isinstance(project_dir, str) and project_dir.strip():
        candidate = project_dir
    else:
        configured = getattr(config, "project_dir", None)
        if isinstance(configured, str) and configured.strip():
            candidate = configured
    if candidate is None:
        raise InvalidParameterError(_NO_PROJECT_DIR_MESSAGE)
    return _validate_project_dir(candidate)
