"""Part 6 tests for per-call ``project_dir`` resolution (runtime folder).

Covers the Plan §6 checklist against
``src/dbt_mcp/dbt_cli/project_dir.py`` + the Part 3 threading in
``src/dbt_mcp/dbt_cli/tools.py``:

1. omitted => startup dir used
2. explicit wins over startup
3. empty/whitespace => fallback to startup
4. nonexistent => InvalidParameterError
5. file-not-dir => error
6. missing dbt_project.yml => error
7. ~/env-var/relative expansion (+ symlinks allowed)
8. startup project_dir=None + omitted => clear error, no crash
9. _get_manifest uses the resolved dir (via get_lineage_dev)

Plus integration coverage: two minimal fake dbt projects (tmp dirs with
dbt_project.yml); parse/list invoked against each, including concurrently
via threads, asserting per-call isolation (subprocess ``cwd`` + manifest
path never cross-contaminate).
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from dbt_mcp.config.config import DbtCliConfig
from dbt_mcp.dbt_cli.binary_type import BinaryType
from dbt_mcp.dbt_cli.project_dir import _resolve_project_dir, _validate_project_dir
from dbt_mcp.dbt_cli.tools import register_dbt_cli_tools
from dbt_mcp.errors.common import InvalidParameterError


def _make_project(parent: Path, name: str) -> Path:
    """Create a minimal fake dbt project dir (marker file only)."""
    proj = parent / name
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "dbt_project.yml").write_text(f"name: {name}\n")
    return proj


def _make_config(project_dir: str | None) -> DbtCliConfig:
    return DbtCliConfig(
        project_dir=project_dir,
        dbt_path="/path/to/dbt",
        dbt_cli_timeout=10,
        binary_type=BinaryType.DBT_CORE,
    )


def _write_manifest(proj: Path, root_id: str, child_id: str) -> None:
    target = proj / "target"
    target.mkdir(exist_ok=True)
    (target / "manifest.json").write_text(
        json.dumps(
            {
                "child_map": {root_id: [child_id]},
                "parent_map": {child_id: [root_id]},
            }
        )
    )


@pytest.fixture
def mock_process():
    class MockProcess:
        returncode = 0

        def communicate(self, timeout=None):
            return "command output", ""

    return MockProcess()


@pytest.fixture
def capture_popen(monkeypatch: MonkeyPatch):
    """Mock Popen capturing per-call ``cwd`` in a thread-safe list."""
    calls: list = []
    lock = threading.Lock()

    class MockProcess:
        returncode = 0

        def communicate(self, timeout=None):
            return "command output", ""

    def mock_popen(*args, **kwargs):
        with lock:
            calls.append(kwargs.get("cwd"))
        return MockProcess()

    monkeypatch.setattr("subprocess.Popen", mock_popen)
    return calls


def _register(tools_holder, config: DbtCliConfig):
    fastmcp, tools = tools_holder
    register_dbt_cli_tools(
        fastmcp,
        config,
        disabled_tools=set(),
        enabled_tools=None,
        enabled_toolsets=set(),
        disabled_toolsets=set(),
    )
    return tools


# --- 1. omitted => startup dir used -----------------------------------------


def test_omitted_project_dir_uses_startup_dir(tmp_path: Path):
    startup = _make_project(tmp_path, "startup")
    config = _make_config(str(startup))
    assert _resolve_project_dir(config, None) == str(startup.resolve())


# --- 2. explicit wins over startup -------------------------------------------


def test_explicit_project_dir_wins_over_startup(tmp_path: Path):
    startup = _make_project(tmp_path, "startup")
    other = _make_project(tmp_path, "other")
    config = _make_config(str(startup))
    assert _resolve_project_dir(config, str(other)) == str(other.resolve())


# --- 3. empty/whitespace => fallback -----------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", "\t\n "])
def test_blank_project_dir_falls_back_to_startup(tmp_path: Path, blank: str):
    startup = _make_project(tmp_path, "startup")
    config = _make_config(str(startup))
    assert _resolve_project_dir(config, blank) == str(startup.resolve())


# --- 4. nonexistent => InvalidParameterError ---------------------------------


def test_nonexistent_project_dir_raises(tmp_path: Path):
    startup = _make_project(tmp_path, "startup")
    config = _make_config(str(startup))
    missing = tmp_path / "does-not-exist"
    with pytest.raises(InvalidParameterError, match="does not exist"):
        _resolve_project_dir(config, str(missing))


# --- 5. file-not-dir => error -------------------------------------------------


def test_file_project_dir_raises_not_a_directory(tmp_path: Path):
    startup = _make_project(tmp_path, "startup")
    config = _make_config(str(startup))
    not_a_dir = tmp_path / "just-a-file.txt"
    not_a_dir.write_text("hello")
    with pytest.raises(InvalidParameterError, match="not a directory"):
        _resolve_project_dir(config, str(not_a_dir))


# --- 6. missing dbt_project.yml => error --------------------------------------


def test_dir_without_dbt_project_yml_raises(tmp_path: Path):
    startup = _make_project(tmp_path, "startup")
    config = _make_config(str(startup))
    bare = tmp_path / "bare-dir"
    bare.mkdir()
    with pytest.raises(InvalidParameterError, match="missing dbt_project.yml"):
        _resolve_project_dir(config, str(bare))


# --- 7. ~/env-var/relative expansion ------------------------------------------


def test_tilde_expansion(tmp_path: Path, monkeypatch: MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = _make_project(tmp_path, "homeproj")
    config = _make_config(None)
    assert _resolve_project_dir(config, "~/homeproj") == str(proj.resolve())


@pytest.mark.parametrize("template", ["$PART6_TEST_PROJ", "${PART6_TEST_PROJ}"])
def test_env_var_expansion(tmp_path: Path, monkeypatch: MonkeyPatch, template: str):
    proj = _make_project(tmp_path, "envproj")
    monkeypatch.setenv("PART6_TEST_PROJ", str(proj))
    config = _make_config(None)
    assert _resolve_project_dir(config, template) == str(proj.resolve())


def test_relative_path_resolves_against_server_cwd(
    tmp_path: Path, monkeypatch: MonkeyPatch
):
    proj = _make_project(tmp_path, "relproj")
    monkeypatch.chdir(tmp_path)
    config = _make_config(None)
    assert _resolve_project_dir(config, "relproj") == str(proj.resolve())


def test_symlink_is_allowed_and_resolved(tmp_path: Path):
    proj = _make_project(tmp_path, "realproj")
    link = tmp_path / "linkproj"
    link.symlink_to(proj, target_is_directory=True)
    config = _make_config(None)
    assert _resolve_project_dir(config, str(link)) == str(proj.resolve())


def test_validate_returns_absolute_resolved_path(
    tmp_path: Path, monkeypatch: MonkeyPatch
):
    proj = _make_project(tmp_path, "someproj")
    monkeypatch.chdir(tmp_path)
    resolved = _validate_project_dir("someproj")
    assert resolved == str(proj.resolve())
    assert Path(resolved).is_absolute()


# --- 8. startup None + omitted => clear error, no crash ------------------------


@pytest.mark.parametrize("omitted", [None, "", "   "])
def test_no_dir_anywhere_raises_clear_error(omitted):
    config = _make_config(None)
    with pytest.raises(InvalidParameterError, match="No project directory"):
        _resolve_project_dir(config, omitted)


# --- 9. _get_manifest uses the resolved dir ------------------------------------
# get_lineage_dev -> _get_manifest is the observable path: distinct manifests
# per project must yield distinct lineage.


def test_get_manifest_uses_resolved_dir(
    tmp_path: Path, mock_process, mock_fastmcp, monkeypatch: MonkeyPatch
):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: mock_process)
    proj_a = _make_project(tmp_path, "proj_a")
    proj_b = _make_project(tmp_path, "proj_b")
    _write_manifest(proj_a, "model.proj_a.root", "model.proj_a.child")
    _write_manifest(proj_b, "model.proj_b.root", "model.proj_b.child")

    tools = _register(mock_fastmcp, _make_config(str(proj_a)))

    lineage_a = tools["get_lineage_dev"](
        unique_id="model.proj_a.root",
        types=None,
        depth=5,
        project_dir=str(proj_a),
    )
    assert lineage_a["children"][0]["model_id"] == "model.proj_a.child"

    # Same tool, different per-call dir => reads the other manifest.
    lineage_b = tools["get_lineage_dev"](
        unique_id="model.proj_b.root",
        types=None,
        depth=5,
        project_dir=str(proj_b),
    )
    assert lineage_b["children"][0]["model_id"] == "model.proj_b.child"

    # Omitted => startup default (proj_a) manifest.
    lineage_default = tools["get_lineage_dev"](
        unique_id="model.proj_a.root", types=None, depth=5
    )
    assert lineage_default["children"][0]["model_id"] == "model.proj_a.child"


# --- Integration: two projects, sequential + concurrent isolation --------------


def test_parse_omitted_uses_startup_and_explicit_overrides(
    tmp_path: Path, capture_popen, mock_fastmcp
):
    proj_a = _make_project(tmp_path, "proj_a")
    proj_b = _make_project(tmp_path, "proj_b")
    tools = _register(mock_fastmcp, _make_config(str(proj_a)))

    tools["parse"]()
    assert capture_popen[-1] == str(proj_a.resolve())

    tools["parse"](project_dir=str(proj_b))
    assert capture_popen[-1] == str(proj_b.resolve())


def test_concurrent_calls_with_different_dirs_stay_isolated(
    tmp_path: Path, mock_fastmcp, monkeypatch: MonkeyPatch
):
    proj_a = _make_project(tmp_path, "proj_a")
    proj_b = _make_project(tmp_path, "proj_b")
    tools = _register(mock_fastmcp, _make_config(str(proj_a)))
    parse_tool = tools["parse"]
    list_tool = tools["ls"]

    resolved_a = str(proj_a.resolve())
    resolved_b = str(proj_b.resolve())

    def mock_popen(*args, **kwargs):
        # Echo this call's cwd back through stdout so each thread can assert
        # on its OWN call's resolution (no shared-list indexing races).
        cwd = kwargs.get("cwd")

        class EchoProcess:
            returncode = 0

            def communicate(self, timeout=None):
                return f"cwd={cwd}", ""

        return EchoProcess()

    monkeypatch.setattr("subprocess.Popen", mock_popen)

    def run_one(i: int) -> None:
        target = str(proj_a) if i % 2 == 0 else str(proj_b)
        expected = resolved_a if i % 2 == 0 else resolved_b
        tool = parse_tool if i % 3 else list_tool
        result = tool(project_dir=target)
        assert result == f"cwd={expected}", (
            f"call for {target} observed wrong cwd: {result}"
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(run_one, range(32)))


def test_cloud_cli_validates_dir_but_keeps_subprocess_cwd_none(
    tmp_path: Path, capture_popen, mock_fastmcp
):
    """Cloud CLI contract: identical validation, but subprocess cwd is ALWAYS
    None (the local dir only selects which manifest is read)."""
    proj_a = _make_project(tmp_path, "proj_a")
    proj_b = _make_project(tmp_path, "proj_b")
    config = DbtCliConfig(
        project_dir=str(proj_a),
        dbt_path="/path/to/dbt",
        dbt_cli_timeout=10,
        binary_type=BinaryType.DBT_CLOUD_CLI,
    )
    tools = _register(mock_fastmcp, config)

    tools["parse"](project_dir=str(proj_b))
    assert capture_popen[-1] is None

    # ...while bad dirs still fail validation for Cloud CLI too.
    with pytest.raises(InvalidParameterError, match="does not exist"):
        tools["parse"](project_dir=str(tmp_path / "nope"))
