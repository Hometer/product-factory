import copy
import socket
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app import command_controls as controls
from backend.app import commands, recovery
from backend.app.db import CommandCheckpoint, CommandControl, CommandRun
from backend.app.main import PREFIX, create_app
from backend.app.requirements import locked
from backend.tests import test_commands as ch
from backend.tests import test_execution as eh
from backend.tests import test_recovery as rh
from backend.tests.test_requirements import post

setup = ch.setup
prepared = ch.prepared
ready = ch.ready
executable = ch.executable
parked = rh.parked


def route(pid, suffix=""):
    return f"/projects/{pid}/controls{suffix}"


def entries(ex):
    result = ex[0].get(PREFIX + route(ex[1]))
    assert result.status_code == 200, result.json()
    return result.json()["entries"]


def entry(ex, run):
    return next(e for e in entries(ex) if e["run"]["id"] == run["id"])


def action(ex, item, kind, key=None, **changes):
    return post(
        ex[0],
        route(ex[1], f"/runs/{item['run']['id']}/{kind}"),
        {
            "confirmed": True,
            "reason": "测试审阅控制、安全重复与零費范围",
            "expected_hash": item["expected_hash"],
            **changes,
        },
        key,
    )


def wait(ex, run, states):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        e = entry(ex, run)
        if e["run"]["state"] in states:
            return e
        time.sleep(0.025)
    raise AssertionError(e)


def live(ex, reopened):
    return (reopened, *ex[1:3], reopened.app, *ex[4:])


@pytest.mark.parametrize("kind,state", [("pause", "paused"), ("cancel", "cancelled")])
def test_queued_control_effective_without_process_and_restart_keeps_it(parked, monkeypatch, kind, state):
    ex, _, run = parked
    first = entry(ex, run)
    assert entries(ex) == entries(ex)
    response = action(ex, first, kind, "queued-control-key")
    assert response.status_code == 200, response.json()
    after = response.json()
    assert after["run"]["state"] == state and after["request"]["effect"] == "applied"
    assert after["request"]["process_started"] is False and after["run"]["details"]["pid"] is None
    assert action(ex, first, kind, "queued-control-key").json() == after
    monkeypatch.setattr(
        commands.subprocess, "Popen", lambda *a, **k: pytest.fail("must not start paused/cancelled")
    )
    with TestClient(create_app(ex[2], ex[4]), headers=dict(ex[0].headers)) as reopened:
        assert entry(live(ex, reopened), run) == after
        assert not rh.task(live(ex, reopened), run["id"])["can_resume"]
    assert ex[4].calls == 0


def test_continue_unexecuted_pause_same_id_and_cancel_wins(parked):
    ex, _, run = parked
    paused = action(ex, entry(ex, run), "pause").json()
    assert paused["can_continue"]
    continued = action(ex, paused, "continue", "continue-same-id-key")
    assert continued.status_code == 200
    assert continued.json()["run"]["id"] == run["id"] and continued.json()["attempt"] == 1
    assert action(ex, paused, "continue", "continue-same-id-key").json()["run"]["id"] == run["id"]
    cancelled = action(ex, entry(ex, run), "cancel").json()
    assert cancelled["run"]["state"] == "cancelled" and not cancelled["can_continue"]
    assert action(ex, paused, "continue").status_code == 409
    assert action(ex, cancelled, "retry").status_code == 409
    with TestClient(create_app(ex[2], ex[4]), headers=dict(ex[0].headers)) as reopened:
        assert entry(live(ex, reopened), run)["run"]["state"] == "cancelled"


def test_actual_static_pause_continue_new_attempt_cancel_and_port_released(executable):
    ex = executable
    asset = ch.preview(ex, "static_preview", ["index.html"], port=ch.free_port())
    run = ch.wait(ex, ch.start(ex, asset).json(), {"serving", "failed"})
    assert run["state"] == "serving"
    result = action(ex, entry(ex, run), "pause")
    assert result.status_code == 200 and result.json()["request"]["kind"] == "pause"
    paused = wait(ex, run, {"paused"})
    assert paused["request"]["effect"] == "applied" and paused["request"]["process_started"]
    assert paused["run"]["details"]["exit_code"] == -15 and commands.available(asset["port"])
    assert paused["can_continue"] and not rh.task(ex, run["id"])["can_resume"]
    resumed = action(ex, paused, "continue", "static-continue-key")
    assert resumed.status_code == 200, resumed.json()
    child = resumed.json()["run"]
    assert child["id"] != run["id"] and resumed.json()["parent_id"] == run["id"]
    assert resumed.json()["root_id"] == run["id"] and resumed.json()["attempt"] == 2
    assert action(ex, paused, "continue", "static-continue-key").json()["run"]["id"] == child["id"]
    running = wait(ex, child, {"serving", "failed"})
    assert running["run"]["state"] == "serving"
    assert not entry(ex, run)["can_continue"]
    assert action(ex, running, "cancel").status_code == 200
    cancelled = wait(ex, child, {"cancelled"})
    assert cancelled["request"]["effect"] == "applied" and commands.available(asset["port"])
    assert not cancelled["can_retry"] and len(entries(ex)) == 2
    assert ch.start(ex, asset).json()["id"] in {run["id"], child["id"]}
    assert ex[4].calls == 0


def test_failed_read_only_maximum_two_retries_preserves_every_result(executable):
    ex = executable
    run = ch.wait(ex, ch.start(ex, ch.preview(ex, "json_validate", ["bad.json"])).json())
    assert run["state"] == "failed"
    root = run["id"]
    facts = []
    for number in (2, 3):
        current = entry(ex, run)
        facts.append(copy.deepcopy(current["run"]))
        response = action(ex, current, "retry", f"retry-{number}-key")
        assert response.status_code == 200, response.json()
        assert response.json()["attempt"] == number and response.json()["root_id"] == root
        run = ch.wait(ex, response.json()["run"])
        assert run["state"] == "failed" and run["details"]["exit_code"] == 1
    last = entry(ex, run)
    assert not last["can_retry"] and action(ex, last, "retry").json()["error"]["code"] == "RETRY_LIMIT"
    assert len(entries(ex)) == 3 and all(e["cost_micro"] == 0 for e in entries(ex))
    for old in facts:
        assert entry(ex, old)["run"] == old
    with ex[3].state.sessions() as s:
        numbers = list(s.scalars(select(CommandControl.attempt).where(CommandControl.root_id == root)))
        assert sorted(numbers) == [1, 2, 3]
    assert ex[4].calls == 0


def test_parallel_retry_creates_single_linked_attempt(executable):
    ex = executable
    run = ch.wait(ex, ch.start(ex, ch.preview(ex, "json_validate", ["bad.json"])).json())
    snapshot = entry(ex, run)
    ex[3].state.command_manager.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda key: action(ex, snapshot, "retry", key), ["concurrent-retry-1", "concurrent-retry-2"]
            )
        )
    assert sorted(r.status_code for r in results) == [200, 409]
    assert len(entries(ex)) == 2 and sum(e["attempt"] == 2 for e in entries(ex)) == 1


@pytest.mark.parametrize("kind", ["pause", "cancel"])
def test_current_safe_check_finishes_before_control_effect(executable, monkeypatch, tmp_path, kind):
    ex = executable
    # Real trusted test runner delays the only read-only step, then emits the normal strict metadata protocol.
    source = "import json,time\nfrom pathlib import Path\ntime.sleep(0.6)\np=json.loads(Path('input.json').read_text())\nfor i,f in enumerate(p['files']): print(json.dumps({'kind':'check','index':i,'valid':True,'line':None,'column':None,'error_code':None}),flush=True)\nprint(json.dumps({'kind':'done','count':len(p['files'])}),flush=True)\n"
    ch.fake_runner(monkeypatch, tmp_path, source)
    run = ch.wait(ex, ch.start(ex, ch.preview(ex)).json(), {"running"})
    result = action(ex, entry(ex, run), kind)
    assert result.status_code == 200
    assert result.json()["request"]["effect"] == "pending"
    final = wait(ex, run, {"succeeded", "cancelled", "failed"})
    assert final["run"]["details"]["exit_code"] == 0 and final["run"]["details"]["checks"]
    assert final["run"]["state"] == ("succeeded" if kind == "pause" else "cancelled")
    assert final["request"]["effect"] == ("step_completed" if kind == "pause" else "applied")
    assert len(entries(ex)) == 1 and not final["can_continue"]


def test_request_after_intent_before_popen_creates_no_process(parked, monkeypatch):
    ex, _, run = parked
    manager = commands.Manager(ex[3].state.sessions, ex[2])
    original = commands.private_file
    intercepted = False

    def intercept(directory, name, raw):
        nonlocal intercepted
        original(directory, name, raw)
        if name == "input.json" and not intercepted:
            intercepted = True
            response = action(ex, entry(ex, run), "pause")
            assert response.status_code == 200

    monkeypatch.setattr(commands, "private_file", intercept)
    monkeypatch.setattr(
        commands.subprocess, "Popen", lambda *a, **k: pytest.fail("request must prevent launch")
    )
    manager.execute(run["id"])
    paused = entry(ex, run)
    assert paused["run"]["state"] == "paused" and paused["request"]["process_started"] is False
    # Private preparation happened, so continuation is a new associated attempt, not overwriting that directory.
    result = action(ex, paused, "continue")
    assert result.status_code == 200, result.json()
    assert result.json()["attempt"] == 2 and result.json()["parent_id"] == run["id"]


def test_cancel_pending_overrides_pause_and_unknown_never_fake_effective(parked):
    ex, _, run = parked
    with locked(ex[3].state.sessions) as s:
        row = s.get(CommandRun, run["id"])
        row.state = "running"
        row.details = {**row.details, "pid": 999999, "health": "unknown"}
        recovery.checkpoint(s, row, "launch_intent", "result_unknown")
    assert action(ex, entry(ex, run), "pause").status_code == 200
    paused_request = entry(ex, run)
    assert paused_request["request"]["effect"] == "pending"
    assert action(ex, paused_request, "cancel").status_code == 200
    commands.reconcile(ex[3].state.sessions, ex[2])
    current = entry(ex, run)
    assert current["run"]["state"] == "interrupted"
    assert current["request"]["effect"] == "unknown" and current["request"]["effective_at"] is None
    assert not current["can_retry"] and not rh.task(ex, run["id"])["can_resume"]
    assert ch.start(ex, ch.preview(ex)).status_code == 409
    with ex[3].state.sessions() as s:
        phases = [
            c.payload["phase"]
            for c in s.scalars(select(CommandCheckpoint).where(CommandCheckpoint.run_id == run["id"]))
        ]
        assert "pause_requested" in phases and "cancel_requested" in phases


@pytest.mark.parametrize("change", ["file", "authorization", "environment", "artifact"])
def test_retry_rechecks_scope_and_result(executable, monkeypatch, change):
    ex = executable
    run = ch.wait(ex, ch.start(ex, ch.preview(ex, "json_validate", ["bad.json"])).json())
    first = entry(ex, run)
    if change == "file":
        (ex[-1] / "bad.json").write_text('{"external":true}')
    elif change == "authorization":
        assert (
            post(
                ex[0],
                eh.api(ex[1], "/revoke"),
                {"authorization_id": ex[7]["id"], "reason": "测试撤销重试范围"},
            ).status_code
            == 200
        )
    elif change == "environment":
        monkeypatch.setattr(
            commands.execution,
            "source",
            lambda *a: (_ for _ in ()).throw(commands.BusinessError("ENVIRONMENT_STALE", "环境已过期", 409)),
        )
    else:
        (ex[2].data_dir / "command-runs" / run["id"] / "result.json").write_text("{}")
    assert not entry(ex, run)["can_retry"]
    assert action(ex, first, "retry").status_code == 409
    assert len(entries(ex)) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"confirmed": False},
        {"confirmed": 1},
        {"reason": "x"},
        {"reason": "sk-" + "x" * 40},
        {"expected_hash": "a" * 64},
    ],
)
def test_invalid_confirmation_secret_and_stale_hash_rejected(parked, changes):
    ex, _, run = parked
    result = action(ex, entry(ex, run), "pause", **changes)
    assert result.status_code in {409, 422}
    assert entry(ex, run)["run"]["state"] == "queued"


def test_cross_project_untrusted_origin_and_read_does_not_mutate(parked):
    ex, _, run = parked
    item = entry(ex, run)
    for kind in ("pause", "cancel", "retry", "continue"):
        response = post(
            ex[0],
            route("00000000-0000-0000-0000-000000000000", f"/runs/{run['id']}/{kind}"),
            {"confirmed": True, "reason": "隔离测试", "expected_hash": item["expected_hash"]},
        )
        assert response.status_code == 404
    response = ex[0].post(
        PREFIX + route(ex[1], f"/runs/{run['id']}/pause"),
        json={"confirmed": True, "reason": "明确测试", "expected_hash": item["expected_hash"]},
        headers={"Origin": "https://untrusted.invalid", "Idempotency-Key": "bad-origin-key"},
    )
    assert response.status_code == 403 and entries(ex) == entries(ex)


def test_success_is_terminal_and_cannot_retry(executable):
    ex = executable
    run = ch.wait(ex, ch.start(ex, ch.preview(ex)).json())
    item = entry(ex, run)
    for kind in ("pause", "cancel", "continue", "retry"):
        assert action(ex, item, kind).status_code == 409
    assert item["run"]["state"] == "succeeded" and len(entries(ex)) == 1


@pytest.mark.parametrize("kind,state", [("pause", "paused"), ("cancel", "cancelled")])
def test_startup_applies_persisted_queued_intent_before_dispatch(parked, monkeypatch, kind, state):
    ex, _, run = parked
    with locked(ex[3].state.sessions) as s:
        row = s.get(CommandRun, run["id"])
        controls.update_request(
            controls.initialize(s, row),
            {
                "kind": kind,
                "reason": "重启前已持久保存的明确请求",
                "requested_at": "2026-10-08T00:00:00Z",
                "effect": "pending",
                "effective_at": None,
                "process_started": None,
            },
        )
    monkeypatch.setattr(
        commands.subprocess, "Popen", lambda *a, **k: pytest.fail("pending intent must win startup")
    )
    with TestClient(create_app(ex[2], ex[4]), headers=dict(ex[0].headers)) as reopened:
        current = entry(live(ex, reopened), run)
        assert current["run"]["state"] == state
        assert current["request"]["effect"] == "applied" and current["request"]["process_started"] is False
    assert ex[4].calls == 0


def test_paused_static_continue_keeps_unrelated_port_listener(executable):
    ex = executable
    asset = ch.preview(ex, "static_preview", ["index.html"], port=ch.free_port())
    run = ch.wait(ex, ch.start(ex, asset).json(), {"serving", "failed"})
    assert run["state"] == "serving"
    action(ex, entry(ex, run), "pause")
    paused = wait(ex, run, {"paused"})
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", asset["port"]))
        listener.listen()
        assert not entry(ex, run)["can_continue"]
        assert action(ex, paused, "continue").status_code == 409
        assert not commands.available(asset["port"]) and listener.fileno() >= 0
    assert entry(ex, run)["can_continue"] and len(entries(ex)) == 1
