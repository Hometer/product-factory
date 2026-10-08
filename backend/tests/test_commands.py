import copy
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app import commands
from backend.app.db import AuditEvent, CommandAsset, CommandRun, ModelBudget
from backend.app.main import PREFIX, create_app
from backend.app.patch_files import workspace_lock
from backend.tests import test_execution as eh
from backend.tests import test_planning as ph
from backend.tests import test_requirements as rh
from backend.tests.test_requirements import post

setup = rh.setup
prepared = ph.prepared
ready = eh.ready


def route(pid, suffix=""):
    return f"/projects/{pid}/commands{suffix}"


@pytest.fixture
def executable(ready):
    c, pid, settings, app, provider, plan, row = ready
    root = settings.allowed_roots[0] / "demo"
    samples = {
        "sample.py": "import os\nos.system('touch SHOULD_NOT_EXECUTE')\n",
        "bad.py": "def broken(:\n",
        "data.json": '{"count": 2}',
        "bad.json": '{"count": }',
        "index.html": '<!doctype html><html><head><link rel="stylesheet" href="/style.css"></head><body><h1>合成预览</h1></body></html>',
        "style.css": "body { background: #eff6ff; }",
    }
    for name, value in samples.items():
        (root / name).write_text(value)
    text = copy.deepcopy(row["payload"]["text"])
    text["steps"][0]["operation"] = "local_command"
    text["files"] = list(samples)
    response = eh.save(ready, text)
    assert response.status_code == 200, response.json()
    row = response.json()
    grant = eh.review(c, pid, row, "authorize")
    assert grant.status_code == 200, grant.json()
    yield c, pid, settings, app, provider, plan, row, grant.json(), root


def preview(executable, recipe="python_syntax", files=None, **changes):
    c, pid, *rest = executable
    row = executable[6]
    response = post(
        c,
        route(pid, "/preview"),
        {
            "execution_id": row["id"],
            "expected_hash": row["content_hash"],
            "recipe_id": recipe,
            "files": files or ["sample.py"],
            **changes,
        },
    )
    assert response.status_code == 200, response.json()
    return response.json()


def start(executable, asset, key=None, **changes):
    c, pid = executable[:2]
    return post(
        c,
        route(pid, "/start"),
        {
            "asset_id": asset["id"],
            "expected_hash": asset["content_hash"],
            "confirmed": True,
            "reason": "测试批准具体配方及合成快照",
            **changes,
        },
        key,
    )


def wait(executable, run, states=None):
    c, pid = executable[:2]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = c.get(PREFIX + route(pid, "/runs/" + run["id"]))
        assert result.status_code == 200, result.json()
        row = result.json()
        if row["state"] in (states or {"succeeded", "failed", "timed_out", "stopped", "interrupted"}):
            return row
        time.sleep(0.03)
    raise AssertionError("command did not finish: " + repr(row))


def stop(executable, run):
    c, pid = executable[:2]
    result = post(c, route(pid, "/runs/" + run["id"] + "/stop"), {"confirmed": True})
    assert result.status_code == 200, result.json()
    return wait(executable, run)


def free_port():
    for port in range(4200, 4300):
        if commands.available(port):
            return port
    raise AssertionError("no free controlled port")


def fake_runner(monkeypatch, tmp_path, source):
    runner = tmp_path / "synthetic_trusted_runner.py"
    runner.write_text(source)
    monkeypatch.setattr(commands, "RUNNER", runner)
    return runner


@pytest.mark.parametrize(
    "recipe,files,state,code",
    [
        ("python_syntax", ["sample.py"], "succeeded", 0),
        ("python_syntax", ["sample.py", "bad.py"], "failed", 1),
        ("json_validate", ["data.json"], "succeeded", 0),
        ("json_validate", ["data.json", "bad.json"], "failed", 1),
    ],
)
def test_actual_process_exit_artifact_and_no_project_execution(executable, recipe, files, state, code):
    c, pid, settings, app, provider, _, row, grant, root = executable
    before = {f.name: f.read_bytes() for f in root.iterdir() if f.is_file()}
    asset = preview(executable, recipe, files)
    assert asset["can_start"] and asset["acceptance"]["R1"]
    assert all("content" not in f for f in asset["files"])
    assert "SHOULD_NOT_EXECUTE" not in json.dumps(asset)
    run = wait(executable, start(executable, asset).json())
    assert run["state"] == state and run["details"]["exit_code"] == code
    assert run["details"]["authorization_id"] == grant["id"]
    assert run["details"]["pid"] > 0 and run["details"]["started_at"] and run["details"]["ended_at"]
    assert len(run["details"]["checks"]) == len(files)
    d = settings.data_dir / "command-runs" / run["id"]
    assert d.stat().st_mode & 0o777 == 0o700
    for name in ("runner.py", "input.json", "result.json"):
        assert (d / name).stat().st_mode & 0o777 == 0o600
    artifact = run["details"]["artifacts"][0]
    assert hashlib.sha256((d / artifact["path"]).read_bytes()).hexdigest() == artifact["sha256"]
    assert {f.name: f.read_bytes() for f in root.iterdir() if f.is_file()} == before
    assert not (root / "SHOULD_NOT_EXECUTE").exists()
    assert provider.calls == 0
    with app.state.sessions() as session:
        budget = session.get(ModelBudget, "phase2")
        assert budget is None or budget.spent_micro == 0


def test_replay_same_asset_and_restart_keep_record(executable):
    c, pid, settings, app, provider = executable[:5]
    a = preview(executable)
    first = start(executable, a, "same-confirmation-key")
    run = wait(executable, first.json())
    assert start(executable, a, "same-confirmation-key").json()["id"] == run["id"]
    assert start(executable, a).json()["id"] == run["id"]
    with app.state.sessions() as s:
        assert len(commands.rows(s, pid)) == 1
    with TestClient(create_app(settings, provider), headers=dict(c.headers)) as reopened:
        found = reopened.get(PREFIX + route(pid, "/runs/" + run["id"])).json()
        assert found == run
    assert provider.calls == 0


@pytest.mark.parametrize(
    "change,code",
    [
        ({"confirmed": False}, 422),
        ({"confirmed": 1}, 422),
        ({"confirmed": "true"}, 422),
        ({"expected_hash": "0" * 64}, 409),
        ({"reason": "api_key=sk-test-synthetic-never-real"}, 400),
        ({"argv": ["sh", "-c", "echo x"]}, 422),
    ],
)
def test_no_broad_or_implicit_confirmation(executable, change, code):
    a = preview(executable)
    result = start(executable, a, **change)
    assert result.status_code == code, result.json()
    assert "error" in result.json()


@pytest.mark.parametrize(
    "changes",
    [
        {"files": ["sample.py", "sample.py"]},
        {"files": ["../sample.py"]},
        {"files": [".env"]},
        {"files": ["index.html"]},
        {"timeout_seconds": 0},
        {"timeout_seconds": 31},
        {"timeout_seconds": True},
        {"port": 4300},
        {"recipe_id": "shell"},
        {"command": "npm test"},
    ],
)
def test_invalid_preview_does_not_execute(executable, changes):
    c, pid = executable[:2]
    row = executable[6]
    result = post(
        c,
        route(pid, "/preview"),
        {
            "execution_id": row["id"],
            "expected_hash": row["content_hash"],
            "recipe_id": "python_syntax",
            "files": ["sample.py"],
            **changes,
        },
    )
    assert result.status_code in {400, 409, 422}, result.json()
    with executable[3].state.sessions() as s:
        assert commands.rows(s, pid) == []


@pytest.mark.parametrize("kind", ["file", "directory", "revoke", "revision", "recipe"])
def test_stale_scope_blocks_before_process(executable, kind, monkeypatch, tmp_path):
    c, pid, _, app, _, plan, row, grant, root = executable
    a = preview(executable)
    if kind == "file":
        (root / "sample.py").write_text("x = 999\n")
    elif kind == "directory":
        root.rename(root.with_name("preserved"))
        root.mkdir()
        (root / "sample.py").write_text("x = 1\n")
    elif kind == "revoke":
        result = post(c, eh.api(pid, "/revoke"), {"authorization_id": grant["id"], "reason": "测试撤销范围"})
        assert result.status_code == 200, result.json()
    elif kind == "recipe":
        fake_runner(monkeypatch, tmp_path, "print('synthetic changed')")
    else:
        result = eh.save((c, pid, executable[2], app, executable[4], plan, row))
        assert result.status_code == 200
    result = start(executable, a)
    assert result.status_code == 409, result.json()
    with app.state.sessions() as s:
        assert not commands.rows(s, pid)


def test_real_service_security_health_and_stop(executable):
    port = free_port()
    asset = preview(executable, "static_preview", ["index.html", "style.css"], port=port)
    run = wait(executable, start(executable, asset).json(), {"serving", "failed"})
    assert run["state"] == "serving", run
    assert commands.health(port, run["id"], run["details"]["instance_id"])
    assert not commands.health(port, run["id"], "wrong-instance")
    response = httpx.get(f"http://127.0.0.1:{port}/")
    assert "合成预览" in response.text
    assert "script-src 'none'" in response.headers["content-security-policy"]
    assert "sandbox allow-same-origin" in response.headers["content-security-policy"]
    for path in ("/.env", "/sample.py", "/../input.json", "/%2e%2e/input.json"):
        assert httpx.get(f"http://127.0.0.1:{port}" + path).status_code == 404
    assert not preview(executable)["can_start"]
    final = stop(executable, run)
    assert final["state"] == "stopped" and final["details"]["exit_code"] is not None
    assert commands.available(port)
    assert start(executable, asset).json()["id"] == final["id"]


def test_port_conflict_preserves_unrelated_listener(executable):
    port = free_port()
    with socket.socket() as unrelated:
        unrelated.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        unrelated.bind(("127.0.0.1", port))
        unrelated.listen()
        a = preview(executable, "static_preview", ["index.html"], port=port)
        assert not a["can_start"]
        result = start(executable, a)
        assert result.status_code == 409 and result.json()["error"]["code"] == "PORT_IN_USE"
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            peer, _ = unrelated.accept()
            peer.close()


def test_running_service_stops_on_file_change(executable):
    a = preview(executable, "static_preview", ["index.html"], port=free_port())
    run = wait(executable, start(executable, a).json(), {"serving", "failed"})
    assert run["state"] == "serving"
    (executable[-1] / "index.html").write_text("external change")
    run = wait(executable, run)
    assert run["state"] == "failed" and run["details"]["error_code"] == "INPUT_STALE"
    assert (executable[-1] / "index.html").read_text() == "external change"


def test_real_timeout_process_group_terminated(executable, monkeypatch, tmp_path):
    fake_runner(monkeypatch, tmp_path, "import time\ntime.sleep(5)\n")
    a = preview(executable, timeout_seconds=1)
    run = wait(executable, start(executable, a).json())
    assert run["state"] == "timed_out" and run["details"]["error_code"] == "TIMEOUT"
    assert run["details"]["exit_code"] != 0
    with pytest.raises(ProcessLookupError):
        os.kill(run["details"]["pid"], 0)


@pytest.mark.parametrize(
    "source,expected",
    [
        (
            "import sys\nsys.stdout.write('api_key=sk-synthetic-');sys.stdout.flush();sys.stdout.write('split-secret\\n');sys.stdout.flush()",
            "OUTPUT_INVALID",
        ),
        ("import sys\nsys.stderr.write('x'*20000);sys.stderr.flush()", "OUTPUT_LIMIT"),
        ('print(\'{\\"kind\\":\\"done\\",\\"count\\":true}\')', "OUTPUT_INVALID"),
        ("print('[]')", "OUTPUT_INVALID"),
        ("raise RuntimeError('synthetic-private-password')", "VALIDATION_FAILED"),
    ],
)
def test_raw_output_filtered_and_bounded(executable, monkeypatch, tmp_path, source, expected):
    fake_runner(monkeypatch, tmp_path, source)
    a = preview(executable)
    run = wait(executable, start(executable, a).json())
    assert run["state"] == "failed" and run["details"]["error_code"] == expected, run
    assert len(run["details"]["logs"]) <= 50 and all(len(v["message"]) <= 160 for v in run["details"]["logs"])
    assert run["details"]["artifacts"]
    with executable[3].state.sessions() as s:
        all_data = json.dumps(
            [r.details for r in commands.rows(s)] + [r.details for r in s.scalars(select(AuditEvent))],
            default=str,
        )
    assert "split-secret" not in all_data and "synthetic-private-password" not in all_data
    assert "api_key=sk" not in json.dumps(run)


def test_minimal_environment_no_credentials(executable, monkeypatch, tmp_path):
    monkeypatch.setenv("SYNTHETIC_PRIVATE_CREDENTIAL", "should-not-be-inherited")
    fake_runner(
        monkeypatch,
        tmp_path,
        "import os,json,sys\nassert 'SYNTHETIC_PRIVATE_CREDENTIAL' not in os.environ\np=json.load(open('input.json'))\nprint(json.dumps({'kind':'check','index':0,'valid':True,'line':None,'column':None,'error_code':None}))\nprint(json.dumps({'kind':'done','count':1}))\n",
    )
    run = wait(executable, start(executable, preview(executable)).json())
    assert run["state"] == "succeeded", run


def test_lock_blocks_admission(executable):
    a = preview(executable)
    with workspace_lock(executable[2], executable[-1]):
        r = start(executable, a)
        assert r.status_code == 409, r.json()


def test_stop_before_start_and_reconcile_never_uses_saved_pid(executable, monkeypatch):
    c, pid, settings, app = executable[:4]
    app.state.command_manager.close()
    a = preview(executable)
    run = start(executable, a).json()
    result = post(c, route(pid, "/runs/" + run["id"] + "/stop"), {"confirmed": True})
    assert result.json()["details"]["stop_requested"]
    app.state.command_manager.execute(run["id"])
    final = wait(executable, run)
    assert final["state"] == "stopped" and final["details"]["pid"] is None
    with app.state.sessions() as s:
        row = s.get(CommandRun, run["id"])
        row.state = "running"
        row.details = {**row.details, "pid": os.getpid()}
        s.commit()
    monkeypatch.setattr(os, "killpg", lambda *a: pytest.fail("must not kill persisted PID"))
    commands.reconcile(app.state.sessions)
    assert wait(executable, run)["state"] == "interrupted"


def test_log_retention_keeps_business_asset(executable):
    c, pid, settings, app = executable[:4]
    run = wait(executable, start(executable, preview(executable)).json())
    with app.state.sessions() as s:
        row = s.get(CommandRun, run["id"])
        row.created_at = datetime.now(UTC) - timedelta(days=31)
        s.commit()
    public = wait(executable, run)
    assert public["details"]["logs"] == [] and public["details"]["log_status"] == "expired"
    commands.expire_logs(app.state.sessions)
    with app.state.sessions() as s:
        row = s.get(CommandRun, run["id"])
        assert row.details["logs"] == [] and row.details["checks"]
        assert s.get(CommandAsset, row.asset_id)
    assert (settings.data_dir / "command-runs" / run["id"] / "input.json").exists()


def test_platform_close_stops_owned_preview(executable):
    a = preview(executable, "static_preview", ["index.html"], port=free_port())
    run = wait(executable, start(executable, a).json(), {"serving", "failed"})
    assert run["state"] == "serving"
    executable[3].state.command_manager.close()
    assert wait(executable, run)["state"] == "stopped"
    assert commands.available(a["port"])


def test_actual_parent_death_stops_trusted_child(tmp_path):
    # An isolated real parent owns this child; no user projects or factory service are touched.
    runner = commands.RUNNER.resolve()
    port = free_port()
    source = "import os,json,subprocess,sys,time\nfrom pathlib import Path\np={'files':[{'path':'index.html','content':'synthetic'}],'port':int(sys.argv[2]),'parent_pid':os.getpid(),'run_id':'synthetic','instance_id':'synthetic'}\nPath('input.json').write_text(json.dumps(p))\nchild=subprocess.Popen([sys.executable,'-I','-B','-S','-u',sys.argv[1],'static_preview'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)\nprint(child.pid,flush=True)\ntime.sleep(10)\n"
    parent = subprocess.Popen(
        [sys.executable, "-c", source, str(runner), str(port)],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        child_pid = int(parent.stdout.readline())
        deadline = time.monotonic() + 3
        while commands.available(port) and time.monotonic() < deadline:
            time.sleep(0.03)
        assert not commands.available(port)
        parent.kill()
        parent.wait(timeout=2)
        deadline = time.monotonic() + 3
        while not commands.available(port) and time.monotonic() < deadline:
            time.sleep(0.03)
        assert commands.available(port) and child_pid != os.getpid()
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=2)
        parent.stdout.close()


def test_artifact_read_and_corruption_blocked(executable):
    c, pid, settings = executable[:3]
    run = wait(executable, start(executable, preview(executable)).json())
    result = c.get(PREFIX + route(pid, "/runs/" + run["id"] + "/artifact"))
    assert result.status_code == 200 and result.json()["exit_code"] == 0
    assert result.json()["checks"][0]["valid"]
    path = settings.data_dir / "command-runs" / run["id"] / "result.json"
    path.write_text('{"unapproved": true}')
    result = c.get(PREFIX + route(pid, "/runs/" + run["id"] + "/artifact"))
    assert result.status_code == 409 and result.json()["error"]["code"] == "ARTIFACT_CONFLICT"


def test_cross_project_records_not_accessible(executable):
    c, pid, settings = executable[:3]
    a = preview(executable)
    run = wait(executable, start(executable, a).json())
    p = c.post(
        PREFIX + "/projects/create-preview",
        json={
            "name": "另一个测试项目",
            "target_user": "测试",
            "core_task": "测试隔离",
            "path": str(settings.allowed_roots[0] / "other"),
        },
    ).json()
    other = post(
        c, "/projects/confirm", {"preview_id": p["preview_id"], "expected_hash": p["content_hash"]}
    ).json()["id"]
    for suffix in ("/assets/" + a["id"], "/runs/" + run["id"], "/runs/" + run["id"] + "/artifact"):
        assert c.get(PREFIX + route(other, suffix)).status_code == 404
    assert post(c, route(other, "/runs/" + run["id"] + "/stop"), {"confirmed": True}).status_code == 404


def test_durable_queued_command_blocks_patch(executable):
    c, pid, settings, app, provider, plan, row = executable[:7]
    text = copy.deepcopy(row["payload"]["text"])
    text["steps"].append({**text["steps"][0], "id": "E2", "operation": "code_write"})
    row = eh.save((c, pid, settings, app, provider, plan, row), text).json()
    assert eh.review(c, pid, row, "authorize").status_code == 200
    executable = (*executable[:6], row, executable[7], executable[8])
    app.state.command_manager.close()
    command = preview(executable)
    run = start(executable, command).json()
    assert run["state"] == "queued"
    patch = post(
        c,
        f"/projects/{pid}/patches/preview",
        {
            "execution_id": row["id"],
            "expected_hash": row["content_hash"],
            "files": [{"path": "sample.py", "content": "x = 2\n"}],
            "validation_plan": "测试补丁与命令互斥",
        },
    ).json()
    assert not patch["can_apply"]
    result = post(
        c,
        f"/projects/{pid}/patches/apply",
        {
            "patch_id": patch["id"],
            "expected_hash": patch["content_hash"],
            "confirmed": True,
            "reason": "测试应阻止运行中的写入",
        },
    )
    assert result.status_code == 409 and result.json()["error"]["code"] == "WORKSPACE_BUSY"
    app.state.command_manager.execute(run["id"])
    assert wait(executable, run)["state"] == "stopped"
    refreshed = c.get(PREFIX + f"/projects/{pid}/patches/assets/" + patch["id"]).json()
    assert refreshed["can_apply"], refreshed


def test_dependencies_remain_pending(executable):
    c, pid, settings, app, provider, plan = executable[:6]
    generated = post(
        c,
        eh.api(pid, "/plans/generate"),
        {"planning_id": plan["id"], "task_id": "T2", "expected_revision": 0},
    ).json()
    text = copy.deepcopy(generated["payload"]["text"])
    text["steps"][0]["operation"] = "local_command"
    text["files"] = ["sample.py"]
    row = eh.save((c, pid, settings, app, provider, plan, generated), text).json()
    assert eh.review(c, pid, row, "authorize").status_code == 200
    a = preview((*executable[:6], row, executable[7], executable[8]))
    assert not a["can_start"]
    assert start(executable, a).json()["error"]["code"] == "DEPENDENCIES_PENDING"


def test_service_revocation_stops_process(executable):
    c, pid = executable[:2]
    a = preview(executable, "static_preview", ["index.html"], port=free_port())
    run = wait(executable, start(executable, a).json(), {"serving", "failed"})
    assert run["state"] == "serving"
    assert (
        post(
            c, eh.api(pid, "/revoke"), {"authorization_id": executable[7]["id"], "reason": "测试运行中撤销"}
        ).status_code
        == 200
    )
    final = wait(executable, run)
    assert final["state"] == "failed" and final["details"]["error_code"] == "AUTHORIZATION_REQUIRED"


@pytest.mark.parametrize(
    "raw",
    [
        b'{"kind":"check","index":0,"valid":true,"line":1,"column":null,"error_code":null}',
        b'{"kind":"check","index":0,"valid":false,"line":null,"column":null,"error_code":null}',
        b'{"kind":"check","index":0,"valid":false,"line":-1,"column":1,"error_code":"PYTHON_SYNTAX"}',
        b'{"kind":"check","index":false,"valid":true,"line":null,"column":null,"error_code":null}',
        b'{"kind":"done","count":1,"raw":"synthetic-private"}',
    ],
)
def test_subprocess_protocol_rejects_contradictions(executable, raw):
    from backend.app.scanner import BusinessError

    a = preview(executable)
    with executable[3].state.sessions() as s:
        asset = s.get(CommandAsset, a["id"])
        with pytest.raises(BusinessError):
            commands.event(raw, asset, {"checks": [], "logs": []})


def test_group_signal_denied_uses_owned_child_fallback(executable, monkeypatch, tmp_path):
    fake_runner(monkeypatch, tmp_path, "import time\ntime.sleep(5)\n")

    def denied(*args):
        raise PermissionError("synthetic macOS exiting group race")

    monkeypatch.setattr(os, "killpg", denied)
    run = wait(executable, start(executable, preview(executable, timeout_seconds=1)).json())
    assert run["state"] == "timed_out" and run["details"]["exit_code"] is not None
    with pytest.raises(ProcessLookupError):
        os.kill(run["details"]["pid"], 0)


def test_unconfirmed_worker_result_preserves_occupancy(executable):
    c, pid, settings, app = executable[:4]
    app.state.command_manager.close()
    a = preview(executable)
    run = start(executable, a).json()
    app.state.command_manager.mark_unknown(run["id"])
    found = wait(executable, run)
    assert found["state"] == "interrupted" and found["details"]["health"] == "unknown"
    result = start(executable, preview(executable))
    assert result.status_code == 409 and result.json()["error"]["code"] == "WORKSPACE_BUSY"
