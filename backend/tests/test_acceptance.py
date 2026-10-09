import copy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app import acceptance, models, planning, requirements
from backend.app.db import RequirementRecord
from backend.app.main import PREFIX, create_app
from backend.tests import test_commands as ch
from backend.tests import test_evaluations as ev
from backend.tests import test_execution as eh
from backend.tests import test_planning as ph
from backend.tests import test_requirements as rh
from backend.tests import test_verification as vh

setup = rh.setup
prepared = eh.prepared
ready = eh.ready
cloud_contract = ev.cloud_contract
executable = ch.executable


def route(ex, suffix=""):
    return "/projects/" + ex[1] + "/acceptance" + suffix


def state(ex, stage="S1"):
    r = ex[0].get(PREFIX + route(ex) + ("?stage_id=" + stage if stage else ""))
    assert r.status_code == 200, r.json()
    return r.json()


def declarations(status="pending"):
    return {
        k: dict(
            status=status,
            reason="隔离合成声明，不代表真实业务通过",
            evidence=["隔离合成检查定位"] if status != "pending" else [],
        )
        for k in ("scope", "secrets", "readme", "project_state")
    }


def package(ex, **changes):
    v = state(ex, changes.get("stage_id", "S1"))
    body = dict(
        stage_id=v["stage"]["id"],
        expected_revision=v["package"]["payload"]["revision"] if v["package"] else 0,
        expected_snapshot_hash=v["snapshot_hash"],
        steps=["打开同项目检查矩阵，核对每项状态与原来源"],
        limitations="隔离合成资料，真实模型与业务效果没有验证",
        reason="隔离合成提交，不冒充真实模型验收",
        declarations=declarations(),
        confirmed=True,
    )
    body.update(changes)
    key = body.pop("_key", None)
    return rh.post(ex[0], route(ex, "/packages"), body, key)


def decision(ex, p, **changes):
    v = state(ex, p["payload"]["stage"]["id"])
    body = dict(
        package_id=p["id"],
        expected_hash=p["content_hash"],
        previous_decision_id=v["decision"]["id"] if v["decision"] else None,
        outcome="deferred",
        confirmed=True,
        reason="隔离合成暂缓，不替代产品负责人业务判断",
    )
    body.update(changes)
    key = body.pop("_key", None)
    return rh.post(ex[0], route(ex, "/decisions"), body, key)


def read(ex, identifier):
    return ex[0].get(PREFIX + route(ex, "/assets/" + identifier))


def test_empty_readonly_and_auth(setup, monkeypatch):
    monkeypatch.setattr(models.httpx, "Client", lambda *a, **k: pytest.fail("no model request"))
    assert state(setup, "")["stages"] == []
    assert setup[0].get(PREFIX + route(setup) + "?stage_id=S1").status_code == 404
    with setup[3].state.sessions() as s:
        assert not acceptance.rows(s, setup[1])
    with TestClient(create_app(setup[2], setup[4])) as c:
        assert c.get(PREFIX + route(setup)).status_code == 401
    assert setup[4].calls == 0


def test_pending_package_full_matrix_block_and_append_history(ready):
    v = state(ready)
    assert {c["key"] for c in v["matrix"]} == set(vh.KINDS) | {
        "real_effect",
        "coverage",
        "evidence",
        "scope",
        "secrets",
        "readme",
        "project_state",
    }
    assert v["coverage"][0]["requirement_id"] == "R1"
    p = package(ready, _key="same-package").json()
    assert p["current"] and p["payload"]["revision"] == 1
    assert (
        package(
            ready, expected_revision=0, expected_snapshot_hash=v["snapshot_hash"], _key="same-package"
        ).json()
        == p
    )
    assert package(ready, _key="same-package", reason="不同内容不能重用标识").status_code == 409
    r = decision(ready, p, outcome="passed")
    assert r.status_code == 409 and r.json()["error"]["code"] == "GATE_BLOCKED"
    first = decision(ready, p, outcome="rejected").json()
    last = decision(ready, p).json()
    assert last["payload"]["previous_decision_id"] == first["id"]
    assert read(ready, first["id"]).json()["payload"]["outcome"] == "rejected"
    assert not read(ready, first["id"]).json()["current"]
    assert state(ready)["decision"]["id"] == last["id"]
    assert ready[4].calls == 0


def test_latest_report_and_first_failure_sources_both_kept(ready):
    vp = vh.plan(ready).json()
    fail = vh.report(ready, vp, status="failed", passed=2, failed=1, failures=["隔离合成首次反例"]).json()
    good = vh.report(ready, vp).json()
    p = package(ready).json()
    check = next(c for c in p["payload"]["matrix"] if c["key"] == "tests")
    assert check["source_id"] == good["id"] and check["passed"] == 3
    assert {fail["id"], good["id"]} <= {e["id"] for e in p["payload"]["evidence"]}
    assert all(ready[1] in e["href"] for e in p["payload"]["evidence"])
    assert decision(ready, p, outcome="passed").status_code == 409


@pytest.mark.parametrize("kind,status", [("lint", "failed"), ("build", "warning"), ("api", "not_applicable")])
def test_failed_warning_and_reasoned_na_retained(ready, kind, status):
    vp = vh.plan(ready).json()
    r = vh.report(
        ready,
        vp,
        kind=kind,
        status=status,
        executed=status != "not_applicable",
        passed=None,
        total=None,
        failed=None,
        failures=["隔离合成失败原因"] if status == "failed" else [],
        warnings=["隔离合成警告原因"] if status == "warning" else [],
        evidence=[] if status == "not_applicable" else ["隔离检查定位"],
    )
    assert r.status_code == 200, r.json()
    p = package(ready).json()
    item = next(c for c in p["payload"]["matrix"] if c["key"] == kind)
    assert item["status"] == status and item["responsible"]
    assert decision(ready, p, outcome="passed").status_code == 409


def test_code_changes_stale_original_decision_and_reopen(ready):
    vp = vh.plan(ready).json()
    vh.report(ready, vp)
    p = package(ready).json()
    d = decision(ready, p).json()
    root = ready[2].allowed_roots[0] / "demo"
    file = root / "README.md"
    file.write_text(file.read_text() + "\n隔离代码变化")
    for id in [p["id"], d["id"]]:
        assert not read(ready, id).json()["current"]
    assert decision(ready, p).json()["error"]["code"] == "ACCEPTANCE_STALE"
    p2 = package(ready).json()
    assert p2["payload"]["previous_id"] == p["id"]
    assert not p2["current"]  # its referenced old verification source is stale
    with TestClient(create_app(ready[2], ready[4]), headers=dict(ready[0].headers)) as c:
        ex = (c, *ready[1:])
        assert state(ex) == state(ready)


def test_new_report_invalidates_package_and_new_plan_keeps_first_failure(ready):
    vp = vh.plan(ready).json()
    fail = vh.report(ready, vp, status="failed", passed=2, failed=1, failures=["首次失败反例保留"]).json()
    p = package(ready).json()
    vh.report(ready, vp)
    assert not read(ready, p["id"]).json()["current"]
    p2 = package(ready).json()
    assert p2["current"]
    assert fail["id"] in {e["id"] for e in p2["payload"]["evidence"]}
    vh.plan(ready)
    assert not read(ready, p2["id"]).json()["current"]


def test_manual_mock_high_score_cannot_pass_real_effect(ready):
    vp = vh.plan(ready).json()
    for kind in vh.KINDS:
        assert vh.report(ready, vp, kind=kind).status_code == 200
    ep = ev.plan(ready, minimum_score=2.0).json()
    for s in ep["payload"]["samples"]:
        ev.score(ready, ep, sample_id=s["id"])
    p = package(ready, declarations=declarations("passed")).json()
    assert p["current"]
    assert decision(ready, p, outcome="passed").status_code == 409
    assert next(c for c in p["payload"]["matrix"] if c["key"] == "real_effect")["status"] == "pending"
    assert ready[4].calls == 0


def test_source_missing_or_hash_mismatch_is_anomaly(ready):
    vp = vh.plan(ready).json()
    r = vh.report(ready, vp).json()
    p = package(ready).json()
    with ready[3].state.sessions.begin() as s:
        row = s.get(RequirementRecord, r["id"])
        row.payload = {**row.payload, "reason": "外部原资产变化保留异常"}
    assert not read(ready, p["id"]).json()["current"]
    with ready[3].state.sessions.begin() as s:
        s.delete(s.get(RequirementRecord, r["id"]))  # isolated fixture, no user data
    assert not read(ready, p["id"]).json()["current"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("confirmed", False),
        ("confirmed", 1),
        ("expected_revision", True),
        ("steps", ["短"]),
        ("reason", "    "),
        ("declarations", {}),
        ("submitted_by", "executor"),
        ("evidence_ids", [str(uuid4())]),
    ],
)
def test_package_strict_and_forgery_rejected(ready, field, value):
    assert package(ready, **{field: value}).status_code == 422


@pytest.mark.parametrize(
    "field,value",
    [
        ("confirmed", False),
        ("confirmed", 1),
        ("reason", "    "),
        ("reviewer", "executor"),
        ("outcome", "automatic"),
    ],
)
def test_decision_strict_identity_and_confirmation(ready, field, value):
    p = package(ready).json()
    assert decision(ready, p, **{field: value}).status_code == 422


def test_conflict_and_decision_idempotence(ready):
    v = state(ready)
    p = package(ready).json()
    assert package(ready, expected_revision=0).status_code == 409
    assert package(ready, expected_snapshot_hash="b" * 64).status_code == 409
    d = decision(ready, p, _key="same-decision").json()
    assert decision(ready, p, previous_decision_id=None, _key="same-decision").json() == d
    assert decision(ready, p, previous_decision_id=None).status_code == 409
    assert decision(ready, p, expected_hash="b" * 64).status_code == 409
    assert v["snapshot_hash"] == state(ready)["snapshot_hash"]


def test_cross_project_and_unknown_stage(ready, tmp_path):
    p = package(ready).json()
    wrong = list(ready)
    wrong[1] = str(uuid4())
    assert read(wrong, p["id"]).status_code == 404
    assert ready[0].get(PREFIX + route(ready) + "?stage_id=S999").status_code == 404
    assert decision(ready, p, package_id=str(uuid4())).status_code == 404


@pytest.mark.parametrize(
    "text", ["password=long-secret-test-value", "客户邮箱 example@example.com", "联系方式 13800138000"]
)
def test_sensitive_text_rejected(ready, text):
    assert package(ready, reason=text).status_code == 409
    p = package(ready).json()
    assert decision(ready, p, reason=text).status_code == 409


def test_current_secret_protects_old_payload(ready):
    p = package(ready, reason="测试历史保护标记abcdefghijklmnop").json()
    env = ready[2].data_dir / "new-model.env"
    env.write_text("FACTORY_MODEL_KEY=abcdefghijklmnop\n")
    env.chmod(0o600)
    from dataclasses import replace

    with TestClient(
        create_app(replace(ready[2], model_env=env), ready[4]), headers=dict(ready[0].headers)
    ) as c:
        ex = (c, *ready[1:])
        r = read(ex, p["id"])
        assert r.status_code == 409 and "abcdefghijklmnop" not in r.text


def test_expired_environment_and_unconfirmed_plan_block(ready):
    with ready[3].state.sessions.begin() as s:
        env = planning.latest(s, ready[1], "environment")
        env.created_at = datetime.now(UTC) - timedelta(minutes=20)
    p = package(ready).json()
    assert decision(ready, p, outcome="passed").status_code == 409
    assert "环境检查已过期" in str(p["blockers"])


def test_dependencies_and_all_tasks_are_collected(ready):
    v = state(ready, "S2")
    assert v["tasks"][0]["id"] == "T2"
    assert "S1" in v["snapshot"]["dependencies"]
    assert any("S1" in x for x in v["blockers"])
    p = package(ready, stage_id="S2").json()
    assert decision(ready, p, outcome="passed").status_code == 409


@pytest.fixture
def all_green(cloud_contract, monkeypatch):
    """All-green isolated FakeProvider contract; never actual model or business acceptance."""
    ex, data = cloud_contract
    c, pid, settings, app, provider = ex
    monkeypatch.setattr(planning, "trusted_version", lambda name: "22.23.2" if name == "node" else "2.54.0")
    draft = requirements.draft_output
    with app.state.sessions() as s:
        d = draft(s, pid)
    base = rh.post(
        c,
        rh.req(pid, "/freeze"),
        dict(
            expected_revision=d["revision"],
            expected_hash=d["content_hash"],
            reason="隔离契约冻结，不等于正式验收",
        ),
    ).json()
    env = rh.post(c, ph.route(pid, "/environment"), {}).json()
    tech = rh.post(
        c,
        ph.route(pid, "/technical/generate"),
        dict(expected_revision=0, baseline_id=base["id"], environment_id=env["id"]),
    ).json()
    plan = ph.generate_plan((c, pid, settings, app, provider, base, env, tech))
    assert ph.confirm(c, pid, "plans", plan).status_code == 200
    ep = ev.plan(ex, samples=data, minimum_score=2.0).json()
    for sample in data:
        assert ev.score(ex, ep, sample_id=sample["id"]).status_code == 200
    assert ev.confirm(ex, ep).status_code == 200
    vp = vh.plan(ex).json()
    for kind in vh.KINDS:
        assert vh.report(ex, vp, kind=kind).status_code == 200
    yield ex, ep, vp


def test_all_green_contract_pass_and_score_revision_invalidates(all_green):
    ex, ep, _ = all_green
    p = package(ex, declarations=declarations("passed")).json()
    assert p["blockers"] == [], p
    d = decision(ex, p, outcome="passed")
    assert d.status_code == 200, d.json()
    assert d.json()["current"]
    ev.score(ex, ep, hard_errors=[dict(kind="incorrect_fact", reason="隔离新反例不能被平均抵消")])
    assert not read(ex, d.json()["id"]).json()["current"]
    assert not read(ex, p["id"]).json()["current"]
    assert decision(ex, p, outcome="passed").status_code == 409


def test_missing_delivery_declaration_blocks_even_all_green(all_green):
    ex, _, _ = all_green
    p = package(ex).json()
    assert p["blockers"] and all("声明" in b for b in p["blockers"])
    assert decision(ex, p, outcome="passed").status_code == 409


def test_hard_failure_after_confirmed_standard_cannot_pass(all_green):
    ex, ep, _ = all_green
    ev.score(ex, ep, hard_errors=[dict(kind="illegal_action", reason="隔离非法动作反例")])
    assert ev.confirm(ex, ep).status_code == 200
    p = package(ex, declarations=declarations("passed")).json()
    assert next(c for c in p["payload"]["matrix"] if c["key"] == "real_effect")["status"] == "failed"
    assert decision(ex, p, outcome="passed").status_code == 409


def test_multiple_tasks_cannot_drop_unreported_task(ready):
    plan = ready[-2]
    text = copy.deepcopy(plan["payload"]["text"])
    t = copy.deepcopy(text["tasks"][0])
    t["id"] = "T3"
    t["title"] = "第二任务全部检查不可删减"
    text["tasks"].append(t)
    new = ph.revise(ready[0], ready[1], plan, text)
    assert ph.confirm(ready[0], ready[1], "plans", new).status_code == 200
    v = state(ready)
    assert {t["id"] for t in v["tasks"]} == {"T1", "T3"}
    assert len([c for c in v["matrix"] if c["task_id"] == "T3"]) == 7


@pytest.mark.parametrize("change", ["draft", "execution", "planning"])
def test_requirement_execution_and_planning_versions_stale(ready, change):
    p = package(ready).json()
    if change == "draft":
        rh.save_complete(ready[0], ready[1])
    elif change == "execution":
        text = copy.deepcopy(ready[-1]["payload"]["text"])
        text["risks"] = "隔离执行准备新风险版本"
        vh.plan(ready)
        p = package(ready).json()
        assert eh.save(ready, text).status_code == 200
    else:
        text = copy.deepcopy(ready[-2]["payload"]["text"])
        text["stages"][0]["scope"] = "隔离范围新版本需要重新确认"
        ph.revise(ready[0], ready[1], ready[-2], text)
    assert not read(ready, p["id"]).json()["current"]
    assert decision(ready, p).status_code == 409


def test_original_platform_run_artifact_tamper_stales_package(executable):
    run = ch.wait(executable, ch.start(executable, ch.preview(executable)).json())
    vp = vh.plan(executable, files=["sample.py"]).json()
    imported = rh.post(
        executable[0],
        vh.route(executable, "/imports"),
        dict(plan_id=vp["id"], expected_hash=vp["content_hash"], run_id=run["id"]),
    )
    assert imported.status_code == 200
    p = package(executable).json()
    assert p["current"]
    target = executable[2].data_dir / "command-runs" / run["id"] / "result.json"
    target.write_text("{}")
    v = read(executable, p["id"]).json()
    assert not v["current"] and any("证据来源" in s for s in v["issues"])
    assert decision(executable, p).status_code == 409


def test_old_standard_not_permanent_block_after_reviewed_new_standard(all_green):
    ex, ep, _ = all_green
    first = package(ex, declarations=declarations("passed")).json()
    assert decision(ex, first, outcome="passed").status_code == 200
    ev.score(ex, ep, reason="隔离新评分重新核对依据")
    assert ev.confirm(ex, ep).status_code == 200
    second = package(ex, declarations=declarations("passed")).json()
    assert second["blockers"] == []
    assert decision(ex, second, outcome="passed").status_code == 200
    assert not read(ex, first["id"]).json()["current"]


def test_dependency_prior_pass_then_source_change_blocks_child(all_green):
    ex, ep, _ = all_green
    p = package(ex, declarations=declarations("passed")).json()
    assert decision(ex, p, outcome="passed").status_code == 200
    v = state(ex, "S2")
    assert not any("关联阶段 S1" in b for b in v["blockers"])
    ev.score(ex, ep, reason="隔离评分新版本使依赖失效")
    assert any("关联阶段 S1" in b for b in state(ex, "S2")["blockers"])


def test_passed_declaration_requires_locator_and_all_four(ready):
    d = declarations("passed")
    d["secrets"]["evidence"] = []
    assert package(ready, declarations=d).status_code == 422
    d = declarations()
    del d["scope"]
    assert package(ready, declarations=d).status_code == 422


def test_key_change_alone_does_not_claim_run_or_invalidate_package(ready):
    p = package(ready).json()
    ready[2].model_env.write_text("FACTORY_MODEL_KEY=non-sensitive-isolated-key\n")
    ready[2].model_env.chmod(0o600)
    assert read(ready, p["id"]).json()["current"]
    assert decision(ready, p, outcome="passed").status_code == 409


def test_concurrent_package_submission_one_revision(ready):
    from concurrent.futures import ThreadPoolExecutor

    v = state(ready)

    def submit(_):
        return package(ready, expected_revision=0, expected_snapshot_hash=v["snapshot_hash"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        rs = list(pool.map(submit, range(2)))
    assert sorted(r.status_code for r in rs) == [200, 409]
    assert len([a for a in state(ready)["history"] if a["kind"] == "acceptance_package"]) == 1


@pytest.mark.parametrize(
    "status,field,value",
    [
        ("not_applicable", "reason", "    "),
        ("not_applicable", "responsible", "  "),
        ("not_applicable", "method", "    "),
        ("not_applicable", "environment", "    "),
        ("passed", "responsible", "  "),
    ],
)
def test_legacy_report_without_meaningful_na_basis_or_owner_cannot_pass(ready, status, field, value):
    vp = vh.plan(ready).json()
    changes = dict(
        kind="api",
        status=status,
        executed=status != "not_applicable",
        passed=None,
        total=None,
        failed=None,
        evidence=[] if status == "not_applicable" else ["隔离实际核对证据"],
        **{field: value},
    )
    r = vh.report(ready, vp, **changes)
    assert r.status_code == 200  # existing report contract retained
    v = state(ready)
    check = next(c for c in v["matrix"] if c["key"] == "api")
    assert check["status"] == "pending" and "报告缺少" in check["reason"]
    assert r.json()["id"] in {e["id"] for e in v["evidence"]}
    p = package(ready).json()
    assert decision(ready, p, outcome="passed").status_code == 409
