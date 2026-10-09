import copy

import pytest
from fastapi.testclient import TestClient

from backend.app import verification
from backend.app.db import CommandRun
from backend.app.main import PREFIX, create_app
from backend.tests import test_commands as ch
from backend.tests import test_execution as eh
from backend.tests import test_requirements as rh

setup = rh.setup
prepared = eh.prepared
ready = eh.ready
executable = ch.executable
KINDS = verification.KINDS


def route(ex, suffix=""):
    return "/projects/" + ex[1] + "/verification" + suffix


def state(ex):
    r = ex[0].get(PREFIX + route(ex))
    assert r.status_code == 200, r.json()
    return r.json()


def plan(ex, **changes):
    v = state(ex)
    old = v["plan"]
    body = {
        "task_id": "T1",
        "expected_revision": old["payload"]["revision"] if old else 0,
        "expected_index_hash": v["index"]["index_hash"],
        "files": ["README.md"],
        "criteria": {k: "核对当前工程对应要求及证据" for k in KINDS},
        "reason": "初次检查计划，合成非敏感资料",
        **changes,
    }
    return rh.post(ex[0], route(ex, "/plans"), body, changes.pop("_key", None))


def report(ex, p, **changes):
    body = {
        "plan_id": p["id"],
        "expected_hash": p["content_hash"],
        "kind": "tests",
        "status": "passed",
        "executed": True,
        "confirmed": True,
        "method": "隔离自动测试人工报告",
        "environment": "合成隔离测试环境",
        "responsible": "本机执行者",
        "reason": "实际测试后核对报告",
        "passed": 3,
        "total": 3,
        "failed": 0,
        "failures": [],
        "warnings": [],
        "evidence": ["合成测试核对记录"],
        **changes,
    }
    key = body.pop("_key", None)
    return rh.post(ex[0], route(ex, "/reports"), body, key)


def read(ex, id):
    return ex[0].get(PREFIX + route(ex, "/assets/" + id))


def test_empty_readonly_no_model(setup, monkeypatch):
    monkeypatch.setattr(
        verification.contexts.models.httpx, "Client", lambda *a, **k: pytest.fail("no external calls")
    )
    c = setup[0]
    v = c.get(PREFIX + route(setup))
    assert v.status_code == 200
    assert len(v.json()["matrix"]) == 7 and all(r["status"] == "pending" for r in v.json()["matrix"])
    with setup[3].state.sessions() as s:
        assert not verification.rows(s, setup[1])
    assert setup[4].calls == 0


def test_plan_versions_report_fail_then_pass_reopen(ready):
    p = plan(ready).json()
    assert p["current"] and p["payload"]["revision"] == 1
    fail = report(
        ready, p, status="failed", passed=2, failed=1, failures=["合成反例：预期通过，实际失败"]
    ).json()
    success = report(ready, p, _key="same-report").json()
    assert report(ready, p, _key="same-report").json() == success
    assert report(ready, p, _key="same-report", reason="另一份不同原因").status_code == 409
    v = state(ready)
    assert v["matrix"][0]["status"] == "passed" and v["matrix"][0]["source"] == "operator_report"
    assert read(ready, fail["id"]).json()["payload"]["status"] == "failed"
    with TestClient(create_app(ready[2], ready[4]), headers=dict(ready[0].headers)) as c:
        ex = (c, *ready[1:])
        assert state(ex) == v
    assert ready[4].calls == 0


def test_code_change_stales_old_report_and_blocks_new(ready):
    p = plan(ready).json()
    r = report(ready, p).json()
    root = ready[2].allowed_roots[0] / "demo"
    file = root / "README.md"
    file.write_text(file.read_text() + "\n外部代码更新")
    assert not read(ready, r["id"]).json()["current"]
    assert report(ready, p).status_code == 409
    p2 = plan(ready, reason="相关代码变化，保持原检查要求复测").json()
    assert p2["payload"]["previous_id"] == p["id"] and p2["payload"]["revision"] == 2
    assert all(m["status"] == "pending" for m in state(ready)["matrix"])
    assert read(ready, r["id"]).json()["payload"] == r["payload"]


def test_unselected_file_change_remains_current_and_criteria_change_stales(ready):
    p = plan(ready).json()
    r = report(ready, p).json()
    (ready[2].allowed_roots[0] / "demo" / "unused.py").write_text("x=2")
    assert read(ready, r["id"]).json()["current"]
    criteria = copy.deepcopy(p["payload"]["criteria"])
    criteria["tests"] = "保持全部反例并扩大本次测试范围"
    p2 = plan(ready, criteria=criteria, reason="新增反例扩大测试，不能删除首次失败").json()
    assert p2["payload"]["reason"] and not read(ready, r["id"]).json()["current"]


@pytest.mark.parametrize(
    "changes",
    [
        {"passed": 4},
        {"total": 0, "passed": 0},
        {"failed": 1, "total": 4},
        {"failures": ["失败不能隐藏"]},
        {"warnings": ["警告不能隐藏"]},
        {"status": "failed"},
        {"status": "warning"},
        {"status": "pending"},
        {"executed": False},
        {"confirmed": 1},
        {"evidence": []},
        {"source": "platform_run"},
        {"responsible": "password=supersecret123"},
        {"environment": "13812345678"},
        {"passed": True},
        {"total": None},
    ],
)
def test_bad_reports_rejected(ready, changes):
    p = plan(ready).json()
    r = report(ready, p, **changes)
    assert r.status_code in (409, 422), r.json()


def test_pending_na_warning_preserve_scope(ready):
    p = plan(ready).json()
    for status in ["pending", "not_applicable"]:
        r = report(
            ready,
            p,
            status=status,
            executed=False,
            passed=None,
            total=None,
            failed=None,
            evidence=[],
            reason="暂未执行或本阶段无对应工程依据",
        )
        assert r.status_code == 200, r.json()
    r = report(ready, p, kind="lint", status="warning", warnings=["工具链仍有已知依赖警告"])
    assert r.status_code == 200
    assert state(ready)["matrix"][2]["status"] == "warning"


@pytest.mark.parametrize(
    "changes",
    [
        {"criteria": {"tests": "只留一个检查"}},
        {"files": ["../README.md"]},
        {"files": ["README.md", "README.md"]},
        {"files": [".env"]},
        {"reason": "short"},
        {"expected_revision": 99},
        {"expected_index_hash": "0" * 64},
    ],
)
def test_bad_plans(ready, changes):
    if changes.get("reason") == "short":
        changes["reason"] = "x"
    assert plan(ready, **changes).status_code in (409, 422)


def test_current_secret_history_hidden(ready):
    p = plan(ready, reason="偶然普通标记historicalmarker123").json()
    ready[2].model_env.write_text("FACTORY_MODEL_KEY=historicalmarker123\n")
    ready[2].model_env.chmod(0o600)
    assert read(ready, p["id"]).status_code == 409


def test_cross_project_and_stale_hash(ready):
    p = plan(ready).json()
    assert report(ready, p, expected_hash="0" * 64).status_code == 409
    other = list(ready)
    other[1] = "00000000-0000-0000-0000-000000000000"
    assert read(other, p["id"]).status_code == 404
    assert ready[0].get(PREFIX + route(ready, "/assets/no-such")).status_code == 404


def test_real_recipe_import_is_supplemental_and_tamper_detected(executable):
    asset = ch.preview(executable)
    run = ch.wait(executable, ch.start(executable, asset).json())
    p = plan(executable, files=["sample.py"]).json()
    body = {"plan_id": p["id"], "expected_hash": p["content_hash"], "run_id": run["id"]}
    r = rh.post(executable[0], route(executable, "/imports"), body, "import-one")
    assert r.status_code == 200, r.json()
    auto = r.json()
    assert (
        auto["payload"]["source"] == "platform_run"
        and auto["payload"]["kind"] == "syntax"
        and auto["payload"]["passed"] == 1
    )
    v = state(executable)
    assert all(m["status"] == "pending" for m in v["matrix"]) and len(v["supplemental"]) == 1
    assert rh.post(executable[0], route(executable, "/imports"), body, "import-one").json() == auto
    assert not (executable[-1] / "SHOULD_NOT_EXECUTE").exists()
    result = executable[2].data_dir / "command-runs" / run["id"] / "result.json"
    result.write_text("{}")
    hist = read(executable, auto["id"]).json()
    assert not hist["current"] and any("证据异常" in i for i in hist["issues"])
    assert rh.post(executable[0], route(executable, "/imports"), body).status_code == 409


def test_failed_recipe_and_run_input_mismatch(executable):
    asset = ch.preview(executable, files=["bad.py"])
    run = ch.wait(executable, ch.start(executable, asset).json())
    wrong = plan(executable).json()
    body = {"plan_id": wrong["id"], "expected_hash": wrong["content_hash"], "run_id": run["id"]}
    assert rh.post(executable[0], route(executable, "/imports"), body).status_code == 409
    p = plan(executable, files=["bad.py"]).json()
    body.update(plan_id=p["id"], expected_hash=p["content_hash"])
    r = rh.post(executable[0], route(executable, "/imports"), body)
    assert r.status_code == 200, r.json()
    assert r.json()["payload"]["status"] == "failed" and r.json()["payload"]["failed"] == 1
    with executable[3].state.sessions() as s:
        row = s.get(CommandRun, run["id"])
        details = copy.deepcopy(row.details)
        details["checks"][0]["valid"] = True
        row.details = details
        s.commit()
    assert not read(executable, r.json()["id"]).json()["current"]


def test_missing_artifact_and_nonterminal_import(executable):
    asset = ch.preview(executable)
    run = ch.wait(executable, ch.start(executable, asset).json())
    p = plan(executable, files=["sample.py"]).json()
    body = {"plan_id": p["id"], "expected_hash": p["content_hash"], "run_id": run["id"]}
    file = executable[2].data_dir / "command-runs" / run["id"] / "result.json"
    file.unlink()
    assert rh.post(executable[0], route(executable, "/imports"), body).status_code == 409
    with executable[3].state.sessions() as s:
        row = s.get(CommandRun, run["id"])
        row.state = "interrupted"
        s.commit()
    assert rh.post(executable[0], route(executable, "/imports"), body).status_code == 409


def test_requirement_change_and_new_execution_preparation_stale(ready):
    p = plan(ready).json()
    text = copy.deepcopy(ready[-1]["payload"]["text"])
    text["risks"] = "合成准备风险修改，保留原记录"
    assert eh.save(ready, text).status_code == 200
    assert not read(ready, p["id"]).json()["current"]


def test_new_secret_cannot_use_historical_plan(ready):
    p = plan(ready, reason="非敏感初始标记nowsecretmarker321").json()
    ready[2].model_env.write_text("FACTORY_MODEL_KEY=nowsecretmarker321\n")
    ready[2].model_env.chmod(0o600)
    assert report(ready, p).status_code == 409


def test_plan_idempotency_and_read_no_mutation(ready):
    v = state(ready)
    b = {
        "task_id": "T1",
        "expected_revision": 0,
        "expected_index_hash": v["index"]["index_hash"],
        "files": ["README.md"],
        "criteria": {k: "保留检查要求与所有反例证据" for k in KINDS},
        "reason": "合成幂等计划核对",
    }
    a = rh.post(ready[0], route(ready, "/plans"), b, "plan-key").json()
    assert rh.post(ready[0], route(ready, "/plans"), b, "plan-key").json() == a
    with ready[3].state.sessions() as s:
        before = [r.id for r in verification.rows(s, ready[1])]
    assert state(ready)["plan"]["id"] == a["id"]
    assert read(ready, a["id"]).status_code == 200
    with ready[3].state.sessions() as s:
        assert [r.id for r in verification.rows(s, ready[1])] == before


def test_unauthenticated_and_unknown_task(ready):
    assert ready[0].get(PREFIX + route(ready), headers={"Authorization": "Bearer invalid"}).status_code == 401
    assert ready[0].get(PREFIX + route(ready), params={"task_id": "T9999"}).status_code == 409


def test_log_retention_does_not_invalidate_verified_artifact(executable):
    asset = ch.preview(executable)
    run = ch.wait(executable, ch.start(executable, asset).json())
    p = plan(executable, files=["sample.py"]).json()
    body = {"plan_id": p["id"], "expected_hash": p["content_hash"], "run_id": run["id"]}
    result = rh.post(executable[0], route(executable, "/imports"), body).json()
    assert result["payload"]["run_hash_version"] == 2
    with executable[3].state.sessions() as s:
        row = s.get(CommandRun, run["id"])
        details = copy.deepcopy(row.details)
        details["logs"] = []
        details["log_status"] = "expired"
        row.details = details
        s.commit()
    assert read(executable, result["id"]).json()["current"]


def test_legacy_hash_version_preserves_payload_and_hash(executable):
    from backend.app.db import RequirementRecord
    from backend.app.scanner import digest

    asset = ch.preview(executable)
    run = ch.wait(executable, ch.start(executable, asset).json())
    p = plan(executable, files=["sample.py"]).json()
    body = {"plan_id": p["id"], "expected_hash": p["content_hash"], "run_id": run["id"]}
    result = rh.post(executable[0], route(executable, "/imports"), body).json()
    with executable[3].state.sessions() as s:
        row = s.get(RequirementRecord, result["id"])
        payload = copy.deepcopy(row.payload)
        payload.pop("run_hash_version")
        payload["run_hash"] = verification.imported(
            s, executable[2], executable[1], verification.owned(s, executable[1], p["id"]), run["id"], 1
        )[3]
        row.payload = payload
        s.commit()
    historical = read(executable, result["id"]).json()
    assert (
        historical["payload"] == payload
        and historical["content_hash"] == digest(payload)
        and historical["current"]
    )
