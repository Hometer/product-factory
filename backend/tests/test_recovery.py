import copy
import json
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app import commands, models, recovery
from backend.app.db import CommandAsset, CommandCheckpoint, CommandRun, RequirementRecord
from backend.app.main import PREFIX, create_app
from backend.app.requirements import locked
from backend.tests import test_commands as ch
from backend.tests import test_execution as eh
from backend.tests.test_requirements import post

setup = ch.setup
prepared = ch.prepared
ready = ch.ready
executable = ch.executable


def route(pid, suffix=""):
    return f"/projects/{pid}/recovery{suffix}"


def workspace(ex):
    response = ex[0].get(PREFIX + route(ex[1]))
    assert response.status_code == 200, response.json()
    return response.json()


def task(ex, identifier):
    return next(t for t in workspace(ex)["tasks"] if t["run"]["id"] == identifier)


def check(ex, run_id, key=None, **changes):
    return post(
        ex[0],
        route(ex[1], f"/runs/{run_id}/check"),
        {"confirmed": True, "reason": "测试核对持久检查点，不执行程序", **changes},
        key,
    )


def resume(ex, t, key=None, **changes):
    return post(
        ex[0],
        route(ex[1], f"/runs/{t['run']['id']}/resume"),
        {
            "confirmed": True,
            "reason": "测试已核对确定未执行及原授权",
            "checkpoint_id": t["checkpoint_id"],
            "expected_hash": t["expected_hash"],
            **changes,
        },
        key,
    )


@pytest.fixture
def parked(executable):
    ex = executable
    ex[3].state.command_manager.close()
    asset = ch.preview(ex)
    result = ch.start(ex, asset)
    assert result.status_code == 200, result.json()
    run = result.json()
    assert run["state"] == "queued"
    return ex, asset, run


@pytest.fixture
def interrupted(parked):
    ex, asset, run = parked
    commands.reconcile(ex[3].state.sessions, ex[2])
    return ex, asset, run


def test_real_restart_same_run_actual_process_once_and_persistent_checkpoints(parked):
    ex, asset, run = parked
    provider = ex[4]
    with TestClient(create_app(ex[2], provider), headers=dict(ex[0].headers)) as reopened:
        live = (reopened, *ex[1:3], reopened.app, *ex[4:])
        t = task(live, run["id"])
        assert t["verdict"] == "not_executed" and t["can_resume"]
        assert t["completed_steps"] == 0 and t["total_steps"] == 1
        assert not (ex[2].data_dir / "command-runs" / run["id"]).exists()
        assert len(workspace(live)["tasks"]) == 1
        result = resume(live, t, "restart-resume-key")
        assert result.status_code == 200, result.json()
        assert result.json()["run"]["id"] == run["id"]
        final = ch.wait(live, run)
        assert final["state"] == "succeeded" and final["details"]["exit_code"] == 0
        assert resume(live, t, "restart-resume-key").json()["run"]["id"] == run["id"]
        assert resume(live, t).status_code == 409
        assert ch.start(live, asset).json()["id"] == run["id"]
        done = task(live, run["id"])
        assert done["completed_steps"] == 1 and done["verdict"] == "completed"
        phases = [c["phase"] for c in done["checkpoints"]]
        assert phases.count("queued") == phases.count("resumed") == phases.count("launch_intent") == 1
        assert "finished" in phases
        snapshot = done
    with TestClient(create_app(ex[2], provider), headers=dict(ex[0].headers)) as reopened:
        live = (reopened, *ex[1:3], reopened.app, *ex[4:])
        assert task(live, run["id"]) == snapshot
    assert not (ex[-1] / "SHOULD_NOT_EXECUTE").exists() and provider.calls == 0


def test_check_idempotent_append_only_and_read_refresh_creates_nothing(interrupted):
    ex, _, run = interrupted
    before = task(ex, run["id"])
    assert workspace(ex) == workspace(ex)
    result = check(ex, run["id"], "same-review-key")
    assert result.status_code == 200
    assert check(ex, run["id"], "same-review-key").json() == result.json()
    after = result.json()
    assert after["checkpoint_count"] == before["checkpoint_count"] + 1
    assert after["checkpoints"][1:] == before["checkpoints"]
    assert resume(ex, before).status_code == 409
    assert after["can_resume"] and after["run"]["id"] == run["id"]
    assert ex[4].calls == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("pid", os.getpid()),
        ("started_at", "2026-10-08T00:00:00+00:00"),
        ("cwd", "/synthetic"),
        ("argv", ["synthetic"]),
        ("stdout_bytes", 1),
        ("checks", [{"path": "sample.py", "valid": True, "line": None, "column": None, "error_code": None}]),
    ],
)
def test_inconsistent_queued_trace_unknown_never_replayed(parked, monkeypatch, field, value):
    ex, _, run = parked
    with locked(ex[3].state.sessions) as s:
        row = s.get(CommandRun, run["id"])
        row.details = {**row.details, field: value}
    monkeypatch.setattr(os, "killpg", lambda *a: pytest.fail("must not target saved PID"))
    commands.reconcile(ex[3].state.sessions, ex[2])
    t = task(ex, run["id"])
    assert t["verdict"] == "result_unknown" and not t["can_resume"]
    assert t["run"]["details"]["error_code"] == "PROCESS_STOP_UNCONFIRMED"
    assert resume(ex, t).status_code == 409
    assert check(ex, run["id"]).json()["verdict"] == "result_unknown"
    assert ex[4].calls == 0


def test_launch_intent_without_pid_is_unknown_and_blocks_other_commands(parked):
    ex, _, run = parked
    with locked(ex[3].state.sessions) as s:
        row = s.get(CommandRun, run["id"])
        recovery.checkpoint(s, row, "launch_intent", "result_unknown")
    commands.reconcile(ex[3].state.sessions, ex[2])
    assert task(ex, run["id"])["verdict"] == "result_unknown"
    assert ch.start(ex, ch.preview(ex)).status_code == 409


def test_private_directory_or_link_prevents_false_not_executed(parked, tmp_path):
    ex, _, run = parked
    directory = ex[2].data_dir / "command-runs"
    directory.mkdir()
    (directory / run["id"]).symlink_to(tmp_path / "absent", target_is_directory=True)
    commands.reconcile(ex[3].state.sessions, ex[2])
    assert task(ex, run["id"])["verdict"] == "result_unknown"


def test_legacy_queued_without_journal_is_unknown(parked):
    ex, asset, run = parked
    with locked(ex[3].state.sessions) as s:
        legacy = CommandRun(
            id=str(uuid4()),
            project_id=ex[1],
            asset_id=asset["id"],
            state="queued",
            details=copy.deepcopy(run["details"]),
        )
        s.add(legacy)
        identifier = legacy.id
    commands.reconcile(ex[3].state.sessions, ex[2])
    assert task(ex, identifier)["verdict"] == "result_unknown"


def test_stop_request_survives_restart_and_wins_over_resume(parked):
    ex, _, run = parked
    post(ex[0], ch.route(ex[1], "/runs/" + run["id"] + "/stop"), {"confirmed": True})
    commands.reconcile(ex[3].state.sessions, ex[2])
    t = task(ex, run["id"])
    assert t["verdict"] == "stopped" and not t["can_resume"]
    assert resume(ex, t).status_code == 409
    assert check(ex, run["id"]).json()["run"]["details"]["stop_requested"]


@pytest.mark.parametrize("change", ["file", "runner", "grant", "execution", "environment"])
def test_resume_rechecks_changed_source_and_authorization(interrupted, monkeypatch, change):
    ex, _, run = interrupted
    original = task(ex, run["id"])
    assert original["can_resume"]
    if change == "file":
        (ex[-1] / "sample.py").write_text("external change")
    elif change == "runner":
        monkeypatch.setattr(commands, "program", lambda: ({"changed": True}, b""))
    elif change == "grant":
        assert (
            post(
                ex[0], eh.api(ex[1], "/revoke"), {"authorization_id": ex[7]["id"], "reason": "测试撤销原授权"}
            ).status_code
            == 200
        )
    elif change == "execution":
        text = copy.deepcopy(ex[6]["payload"]["text"])
        text["steps"][0]["instruction"] += " 新修订"
        assert eh.save(ex[:7], text).status_code == 200
    else:
        monkeypatch.setattr(
            commands.execution,
            "source",
            lambda *a: (_ for _ in ()).throw(commands.BusinessError("ENVIRONMENT_STALE", "环境已过期", 409)),
        )
    t = task(ex, run["id"])
    assert t["verdict"] == "not_executed" and not t["can_resume"] and t["blockers"]
    assert resume(ex, original).status_code == 409
    assert not (ex[2].data_dir / "command-runs" / run["id"]).exists()


@pytest.mark.parametrize(
    "change", [{"confirmed": False}, {"reason": "a"}, {"reason": "sk-" + "x" * 40}, {"confirmed": 1}]
)
def test_invalid_or_secret_confirmation_rejected(interrupted, change):
    ex, _, run = interrupted
    assert resume(ex, task(ex, run["id"]), **change).status_code == 422
    assert check(ex, run["id"], **change).status_code == 422


def test_cross_project_and_missing_origin_rejected(interrupted):
    ex, _, run = interrupted
    other = str(uuid4())
    assert ex[0].get(PREFIX + route(other)).status_code == 404
    for action in ("check", "resume"):
        assert (
            post(
                ex[0],
                route(other, f"/runs/{run['id']}/{action}"),
                {
                    "confirmed": True,
                    "reason": "明确核对",
                    "checkpoint_id": str(uuid4()),
                    "expected_hash": "a" * 64,
                }
                if action == "resume"
                else {"confirmed": True, "reason": "明确核对"},
            ).status_code
            == 404
        )
    response = ex[0].post(
        PREFIX + route(ex[1], f"/runs/{run['id']}/check"),
        json={"confirmed": True, "reason": "明确核对"},
        headers={"Origin": "https://untrusted.invalid", "Idempotency-Key": "invalid-origin-key"},
    )
    assert response.status_code == 403


def test_concurrent_resume_one_run_no_new_approval(interrupted):
    ex, _, run = interrupted
    original = task(ex, run["id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda key: resume(ex, original, key), ["resume-concurrent-1", "resume-concurrent-2"])
        )
    assert sorted(r.status_code for r in results) == [200, 409]
    t = task(ex, run["id"])
    assert t["run"]["state"] == "queued" and t["checkpoint_count"] == original["checkpoint_count"] + 1
    assert workspace(ex)["task_count"] == 1
    # Another platform restart before launch remains provably unexecuted.
    commands.reconcile(ex[3].state.sessions, ex[2])
    assert task(ex, run["id"])["can_resume"]


@pytest.mark.parametrize("corruption", ["bytes", "link", "directory_link", "missing", "metadata"])
def test_completed_result_conflict_not_reinterpreted_as_success(executable, tmp_path, corruption):
    ex = executable
    run = ch.wait(ex, ch.start(ex, ch.preview(ex)).json())
    t = task(ex, run["id"])
    assert t["verdict"] == "completed" and t["step_state"] == "succeeded"
    directory = ex[2].data_dir / "command-runs" / run["id"]
    result = directory / "result.json"
    if corruption == "bytes":
        result.write_text("{}")
    elif corruption == "missing":
        result.unlink()
    elif corruption == "metadata":
        with locked(ex[3].state.sessions) as s:
            row = s.get(CommandRun, run["id"])
            row.details = {**row.details, "exit_code": 9}
    elif corruption == "link":
        copy = tmp_path / "copied.json"
        copy.write_bytes(result.read_bytes())
        result.unlink()
        result.symlink_to(copy)
    else:
        moved = directory.with_name(run["id"] + "-moved")
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
    t = task(ex, run["id"])
    assert t["verdict"] == "conflict" and t["completed_steps"] == 0 and not t["can_resume"]
    assert check(ex, run["id"]).json()["verdict"] == "conflict"
    assert ch.wait(ex, run)["state"] == "succeeded"  # historical result retained


def test_external_unknown_keeps_reservation_no_calls_or_candidate_leak(executable):
    ex = executable
    with locked(ex[3].state.sessions) as s:
        budget = models.budget(s)
        budget.reserved_micro, budget.requests_reserved = 1234, 2
        item = RequirementRecord(
            id=str(uuid4()),
            project_id=ex[1],
            kind="run",
            payload={
                "state": "result_unknown",
                "attempts": [{"attempt": 1, "state": "unknown"}],
                "reserved_micro": 1234,
                "settled_micro": 0,
                "candidate": "PRIVATE_CANDIDATE_DO_NOT_EXPOSE",
            },
        )
        s.add(item)
    result = workspace(ex)
    assert result["analyses"][0]["reserved_micro"] == 1234
    assert not result["analyses"][0]["can_resume"]
    assert "PRIVATE_CANDIDATE" not in json.dumps(result)
    with ex[3].state.sessions() as s:
        assert models.budget(s).reserved_micro == 1234 and models.budget(s).requests_reserved == 2
    assert ex[4].calls == 0


def test_checkpoint_json_contains_no_input_content(executable):
    ex = executable
    run = ch.wait(ex, ch.start(ex, ch.preview(ex)).json())
    with ex[3].state.sessions() as s:
        rows = list(s.scalars(select(CommandCheckpoint).where(CommandCheckpoint.run_id == run["id"])))
        raw = json.dumps([c.payload for c in rows])
        asset = s.get(CommandAsset, run["asset_id"])
        assert asset.payload["files"][0]["content"] not in raw
        assert "SHOULD_NOT_EXECUTE" not in raw and len(rows) >= 4


def test_real_executor_process_crash_after_intent_never_replays(parked):
    import subprocess
    import sys

    ex, _, run = parked
    source = """
import os, sys, threading, time
from pathlib import Path
from types import SimpleNamespace
from fastapi.testclient import TestClient
from backend.app import commands, models
from backend.app.core import Settings
from backend.app.main import create_app, PREFIX
models.start_worker=lambda *a: (threading.Event(), SimpleNamespace(join=lambda **kw: None))
settings=Settings(Path(sys.argv[1]), (Path(sys.argv[2]),), 'test-only-local-token', hosts=('testserver',))
commands.subprocess.Popen=lambda *a, **kw: os._exit(73)
with TestClient(create_app(settings), headers={'Authorization':'Bearer test-only-local-token','Origin':'http://127.0.0.1:3000'}) as c:
    base=PREFIX+'/projects/'+sys.argv[3]+'/recovery'
    t=c.get(base).json()['tasks'][0]
    r=c.post(base+'/runs/'+sys.argv[4]+'/resume',json={'confirmed':True,'reason':'isolated crash injection after committed intent','checkpoint_id':t['checkpoint_id'],'expected_hash':t['expected_hash']},headers={'Idempotency-Key':'actual-process-crash-resume'})
    assert r.status_code==200, r.json()
    time.sleep(5)
"""
    child = subprocess.run(
        [sys.executable, "-c", source, str(ex[2].data_dir), str(ex[2].allowed_roots[0]), ex[1], run["id"]],
        capture_output=True,
        timeout=10,
    )
    assert child.returncode == 73, child.stderr.decode()
    commands.reconcile(ex[3].state.sessions, ex[2])
    t = task(ex, run["id"])
    assert t["verdict"] == "result_unknown" and t["run"]["details"]["pid"] is None
    assert resume(ex, t).status_code == 409 and t["completed_steps"] == 0
    assert not (ex[2].data_dir / "command-runs" / run["id"] / "result.json").exists()
    assert ex[4].calls == 0 and not (ex[-1] / "SHOULD_NOT_EXECUTE").exists()
