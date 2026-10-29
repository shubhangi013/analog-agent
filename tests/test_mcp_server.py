import asyncio
import json
from pathlib import Path
from threading import Event
import time
from types import SimpleNamespace

from mcp import Client
import pytest

from agent import mcp_server


def test_vscode_mcp_configuration_is_workspace_relative() -> None:
    config = json.loads((mcp_server.ROOT / ".vscode" / "mcp.json").read_text(
        encoding="utf-8"
    ))
    server = config["servers"]["analog-circuit-engineer"]

    assert server["type"] == "stdio"
    assert server["command"] == "${workspaceFolder}\\.venv\\Scripts\\python.exe"
    assert server["args"] == ["-m", "agent.mcp_server"]
    assert server["env"]["PYTHONPATH"] == "${workspaceFolder}"


def test_mcp_lists_grounded_circuit_tools(tmp_path: Path) -> None:
    async def check() -> None:
        async with Client(mcp_server.create_server(tmp_path)) as client:
            result = await client.list_tools()
            tools = {tool.name: tool for tool in result.tools}
            assert set(tools) == {
                "optimize_circuit", "debug_circuit", "explain_circuit", "get_run_result",
            }
            schema = tools["optimize_circuit"].input_schema
            assert schema["properties"]["budget"]["minimum"] == 1
            assert schema["properties"]["budget"]["maximum"] == 500
            assert "spec" in schema["required"]

    asyncio.run(check())


def test_mcp_optimize_returns_structured_summary(
    tmp_path: Path, monkeypatch,
) -> None:
    expected = {
        "kind": "optimize", "run_id": "mcp-test", "status": "pass",
        "verified_success": True,
    }

    def fake(root: Path, spec: str, **kwargs):
        assert root == tmp_path
        assert spec == "gain >= 30 dB"
        assert kwargs["budget"] == 12
        return expected

    monkeypatch.setattr(mcp_server, "_launch_optimization", fake)

    async def check() -> None:
        async with Client(mcp_server.create_server(tmp_path)) as client:
            result = await client.call_tool(
                "optimize_circuit", {"spec": "gain >= 30 dB", "budget": 12},
            )
            assert not result.is_error
            assert result.structured_content == expected

    asyncio.run(check())


def test_get_run_result_cannot_traverse_outside_root(tmp_path: Path) -> None:
    async def check() -> None:
        async with Client(mcp_server.create_server(tmp_path)) as client:
            result = await client.call_tool("get_run_result", {"run_id": "..\\secret"})
            assert result.is_error

    asyncio.run(check())


def test_get_run_result_reads_only_compact_summary(tmp_path: Path) -> None:
    directory = tmp_path / "saved-run"
    directory.mkdir()
    (directory / "mcp_summary.json").write_text(
        '{"kind":"debug","run_id":"saved-run","status":"unresolved"}\n',
        encoding="utf-8",
    )

    async def check() -> None:
        async with Client(mcp_server.create_server(tmp_path)) as client:
            result = await client.call_tool("get_run_result", {"run_id": "saved-run"})
            assert not result.is_error
            assert result.structured_content["status"] == "unresolved"
            assert result.structured_content["summary_path"].endswith("mcp_summary.json")

    asyncio.run(check())


def test_background_optimization_returns_immediately_and_can_be_polled(
    tmp_path: Path, monkeypatch,
) -> None:
    started = Event()
    finish = Event()

    def fake(root: Path, spec: str, **kwargs):
        run_id = kwargs["run_id"]
        started.set()
        assert finish.wait(2)
        directory = root / run_id
        directory.mkdir(parents=True)
        return mcp_server._write_summary(directory, {
            "kind": "optimize",
            "run_id": run_id,
            "status": "pass",
            "verified_success": True,
            "budget": kwargs["budget"],
            "timeout_s": kwargs["timeout_s"],
        })

    monkeypatch.setattr(mcp_server, "_optimization", fake)
    launched = mcp_server._launch_optimization(
        tmp_path, "gain >= 30 dB", budget=12, timeout_s=30,
        run_id="background-run", netlist_path=None,
        intake="deterministic", objective=None,
    )

    assert launched["status"] == "running"
    assert launched["run_id"] == "background-run"
    assert started.wait(1)
    running = mcp_server._saved_summary(tmp_path, "background-run")
    assert running["status"] == "running"
    assert running["simulations_used"] == 0
    assert running["budget"] == 12
    with pytest.raises(ValueError, match="already active with different inputs"):
        mcp_server._launch_optimization(
            tmp_path, "gain >= 31 dB", budget=12, timeout_s=30,
            run_id="background-run", netlist_path=None,
            intake="deterministic", objective=None,
        )

    finish.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = mcp_server._saved_summary(tmp_path, "background-run")
        if result["status"] == "pass":
            break
        time.sleep(0.01)
    assert result["status"] == "pass"
    assert result["verified_success"] is True


def test_get_run_result_marks_abandoned_checkpoint_interrupted(tmp_path: Path) -> None:
    directory = tmp_path / "interrupted-run"
    directory.mkdir()
    (directory / "request.json").write_text('{"kind":"optimize"}\n', encoding="utf-8")

    result = mcp_server._saved_summary(tmp_path, "interrupted-run")

    assert result["status"] == "interrupted"
    assert "resume" in result["next_action"]


def test_optimization_wrapper_persists_summary_and_rejects_changed_resume(
    tmp_path: Path, monkeypatch,
) -> None:
    runs = 0

    class FakePlanner:
        @classmethod
        def from_env(cls, tracer):
            return object()

    class FakeOrchestrator:
        def __init__(self, task_id, run_id, **kwargs):
            self.targets = kwargs["targets"]
            self.objective = kwargs["objective"]
            self.verdict = "approve"
            self.export_summary = {"folder": str(tmp_path / run_id / "exports")}

        def run(self):
            nonlocal runs
            runs += 1
            return SimpleNamespace(
                outcome="pass", message="", simulations_used=7,
                cache_hits=1, design_id=3,
            )

    monkeypatch.setattr(mcp_server.LLMConfig, "from_env", lambda: None)
    monkeypatch.setattr(mcp_server, "Planner", FakePlanner)
    monkeypatch.setattr(mcp_server, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(mcp_server, "CachedSimulator", lambda memory, tracer: object())

    first = mcp_server._optimization(
        tmp_path, "gain >= 30 dB", budget=12, timeout_s=30,
        run_id="resume-me", netlist_path=None, intake="deterministic", objective=None,
    )
    second = mcp_server._optimization(
        tmp_path, "gain >= 30 dB", budget=12, timeout_s=30,
        run_id="resume-me", netlist_path=None, intake="deterministic", objective=None,
    )

    assert first["status"] == second["status"] == "pass"
    assert runs == 1
    assert first["approval_source"] == "automatic_mcp_tool"
    assert first["run_id_state"] == "completed_immutable"
    assert first["retry_requires_new_run_id"] is False
    assert Path(first["summary_path"]).is_file()
    with pytest.raises(ValueError, match="completed with budget 12"):
        mcp_server._optimization(
            tmp_path, "gain >= 30 dB", budget=20, timeout_s=60,
            run_id="resume-me", netlist_path=None, intake="deterministic", objective=None,
        )
    with pytest.raises(ValueError, match="completed with timeout 30"):
        mcp_server._optimization(
            tmp_path, "gain >= 30 dB", budget=12, timeout_s=60,
            run_id="resume-me", netlist_path=None, intake="deterministic", objective=None,
        )
    with pytest.raises(ValueError, match="different specification"):
        mcp_server._optimization(
            tmp_path, "gain >= 31 dB", budget=12, timeout_s=30,
            run_id="resume-me", netlist_path=None, intake="deterministic", objective=None,
        )
