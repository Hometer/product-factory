import copy
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app import evaluations, models
from backend.app.main import PREFIX, create_app
from backend.app.requirement_schemas import FIELD_LABELS
from backend.tests import test_requirements as rh

setup = rh.setup
DIMS = ["要求覆盖", "结论正确", "依据可追溯"]


def route(ex, suffix=""):
    return "/projects/" + ex[1] + "/evaluation" + suffix


def state(ex):
    r = ex[0].get(PREFIX + route(ex))
    assert r.status_code == 200, r.json()
    return r.json()


def samples():
    return [
        dict(
            id="S" + str(i),
            title="合成样例" + str(i),
            data_source="synthetic",
            source_ref="隔离合成资料",
            expected="覆盖要求且保留依据",
            mode="operator_report",
            model_source="mock",
            input_text="非敏感测试输入",
            output_text="合成结果内容",
            model="隔离Mock",
            prompt_version="合成提示一版",
            knowledge_version="无知识绑定",
        )
        for i in range(1, 6)
    ]


def plan(ex, **changes):
    v = state(ex)
    body = dict(
        expected_revision=v["plan"]["payload"]["revision"] if v["plan"] else 0,
        expected_basis_hash=v["basis_hash"],
        samples=samples(),
        dimensions=DIMS,
        minimum_score=None,
        context_id=None,
        reason="合成评测计划，未做云冒烟",
    )
    body.update(changes)
    key = body.pop("_key", None)
    return rh.post(ex[0], route(ex, "/plans"), body, key)


def score(ex, p, **changes):
    sid = changes.get("sample_id", "S1")
    v = state(ex)
    old = next((s for s in v["scores"] if s["payload"]["sample_id"] == sid), None)
    body = dict(
        plan_id=p["id"],
        expected_hash=p["content_hash"],
        sample_id=sid,
        previous_score_id=old["id"] if old else None,
        scores={d: 3 for d in DIMS},
        hard_errors=[],
        confirmed=True,
        reason="隔离合成评分，非用户正式判断",
    )
    body.update(changes)
    key = body.pop("_key", None)
    return rh.post(ex[0], route(ex, "/scores"), body, key)


def confirm(ex, p, **changes):
    body = dict(
        plan_id=p["id"],
        expected_hash=p["content_hash"],
        confirmed=True,
        reason="隔离契约确认，非真实业务验收",
        expected_score_ids=[
            s["id"]
            for s in state(ex)["scores"]
            if next(
                x for x in p.get("payload", {}).get("samples", []) if x["id"] == s["payload"]["sample_id"]
            )["verified_real"]
            and next(x for x in p["payload"]["samples"] if x["id"] == s["payload"]["sample_id"])[
                "data_source"
            ]
            != "synthetic"
        ],
    )
    body.update(changes)
    return rh.post(ex[0], route(ex, "/standards"), body)


def test_empty_readonly_never_calls_model(setup, monkeypatch):
    monkeypatch.setattr(models.httpx, "Client", lambda *a, **k: pytest.fail("no external request"))
    v = state(setup)
    assert v["plan"] is None and len(v["groups"]) == 4 and v["eligible_real_count"] == 0
    with setup[3].state.sessions() as s:
        assert not evaluations.rows(s, setup[1])
    assert setup[4].calls == 0
    assert confirm(setup, {"id": str(uuid4()), "content_hash": "a" * 64}).status_code == 404


def test_hard_error_cannot_be_averaged_out_and_history_preserved(setup):
    p = plan(setup).json()
    first = score(setup, p, hard_errors=[{"kind": "incorrect_fact", "reason": "合成错误事实反例"}]).json()
    v = state(setup)
    assert v["groups"][0]["mean"] == 3 and v["groups"][0]["status"] == "failed"
    assert confirm(setup, p).json()["error"]["code"] == "REAL_SAMPLES_PENDING"
    second = score(setup, p).json()
    assert second["payload"]["previous_score_id"] == first["id"]
    v = state(setup)
    assert first in v["history"] and second in v["history"] and v["groups"][0]["status"] == "pending"
    assert second["payload"]["reviewer"] == "local_operator"
    assert v["standard"] is None


def test_three_data_and_four_model_sources_stay_distinct(setup):
    data = samples()
    data[0].update(model_source="real_local", data_source="real_business")
    data[1].update(model_source="fallback_template", data_source="public")
    p = plan(setup, samples=data, minimum_score=2.0).json()
    assert [s["data_source"] for s in p["payload"]["samples"]][:3] == ["real_business", "public", "synthetic"]
    score(setup, p)
    v = state(setup)
    assert v["groups"][1]["total"] == 1 and v["groups"][3]["total"] == 1
    assert all(not s["verified_real"] for s in p["payload"]["samples"])
    assert v["eligible_real_count"] == 0 and confirm(setup, p).status_code == 409


@pytest.mark.parametrize(
    "changes",
    [
        {"scores": {d: True for d in DIMS}},
        {"scores": {d: 1.5 for d in DIMS}},
        {"scores": {d: "3" for d in DIMS}},
        {"scores": {d: 0 for d in DIMS}},
        {"scores": {d: 4 for d in DIMS}},
        {"scores": {}},
        {"scores": {DIMS[0]: 3}},
        {"scores": {**dict.fromkeys(DIMS, 3), "额外维度": 3}},
        {"confirmed": 1},
        {"confirmed": False},
        {"sample_id": "S9"},
        {"hard_errors": [{"kind": "privacy_leak", "reason": "短"}]},
        {"hard_errors": [{"kind": "illegal_action", "reason": "合成非法动作"}] * 2},
        {"reviewer": "delegate"},
        {"reason": "test-only-local-token"},
        {"reason": "联系 13800000000 核对"},
    ],
)
def test_strict_scores_and_privacy(setup, changes):
    p = plan(setup).json()
    r = score(setup, p, **changes)
    assert r.status_code in (422, 409), r.json()
    assert not state(setup)["scores"]


@pytest.mark.parametrize(
    "change",
    [
        {"samples": samples()[:4]},
        {"samples": samples() * 3},
        {"samples": [samples()[0]] * 5},
        {"dimensions": []},
        {"dimensions": ["一"]},
        {"dimensions": DIMS * 2},
        {"minimum_score": True},
        {"minimum_score": 0.5},
        {"minimum_score": 3.1},
        {"expected_revision": True},
        {"reason": "test-only-local-token"},
    ],
)
def test_invalid_plans(setup, change):
    assert plan(setup, **change).status_code in (422, 409)
    assert state(setup)["plan"] is None


def test_manual_cannot_forge_cloud_or_link_run(setup):
    for fields in (
        {"model_source": "real_cloud"},
        {"run_id": str(uuid4())},
        {"mode": "platform_run", "run_id": str(uuid4())},
    ):
        data = samples()
        data[0].update(fields)
        assert plan(setup, samples=data).status_code == 422


def test_idempotency_conflicts_and_reopen(setup):
    p = plan(setup, _key="plan-identity").json()
    v = state(setup)
    replay = dict(
        expected_revision=0,
        expected_basis_hash=v["basis_hash"],
        samples=samples(),
        dimensions=DIMS,
        minimum_score=None,
        context_id=None,
        reason="合成评测计划，未做云冒烟",
    )
    assert rh.post(setup[0], route(setup, "/plans"), replay, "plan-identity").json() == p
    first = score(setup, p, _key="score-identity").json()
    assert score(setup, p, previous_score_id=None, _key="score-identity").json() == first
    assert score(setup, p, previous_score_id=None).json()["error"]["code"] == "REVISION_CONFLICT"
    with TestClient(create_app(setup[2], setup[4]), headers=setup[0].headers) as c:
        assert c.get(PREFIX + route(setup)).json()["history"] == state(setup)["history"]


def test_new_plan_changes_standard_and_preserves_failed_scores(setup):
    p = plan(setup).json()
    s = score(setup, p, hard_errors=[{"kind": "unacceptable", "reason": "不能接受的合成反例"}]).json()
    newer = plan(setup, minimum_score=1.0, reason="合成降低标准记录理由，不能自动关闭失败").json()
    v = state(setup)
    old = next(r for r in v["history"] if r["id"] == s["id"])
    assert not old["current"] and old["payload"] == s["payload"] and not v["scores"]
    assert score(setup, p).json()["error"]["code"] == "EVALUATION_STALE"
    assert confirm(setup, newer).status_code == 409


@pytest.mark.parametrize("target", ["MODEL", "PROMPT_VERSION"])
def test_model_prompt_changes_stale_scores(setup, monkeypatch, target):
    p = plan(setup).json()
    score(setup, p)
    monkeypatch.setattr(models, target, "changed-version")
    assert not state(setup)["plan"]["current"]
    assert score(setup, p).json()["error"]["code"] == "EVALUATION_STALE"


def test_requirement_change_and_new_secret_protection(setup):
    p = plan(setup).json()
    score(setup, p)
    rh.save_complete(setup[0], setup[1])
    assert not state(setup)["plan"]["current"]
    p = plan(setup).json()
    key = p["payload"]["samples"][0]["output_text"]
    setup[2].model_env.write_text("FACTORY_MODEL_KEY=" + key + "\n")
    setup[2].model_env.chmod(0o600)
    r = setup[0].get(PREFIX + route(setup, "/assets/" + p["id"]))
    assert r.status_code == 409 and key not in r.text
    assert (
        rh.post(
            setup[0],
            route(setup, "/scores"),
            {
                "plan_id": p["id"],
                "expected_hash": p["content_hash"],
                "sample_id": "S1",
                "scores": dict.fromkeys(DIMS, 3),
                "confirmed": True,
                "reason": "保护历史不再沿用",
            },
        ).status_code
        == 409
    )


def test_cross_project_not_found_and_unauthenticated(setup):
    p = plan(setup).json()
    wrong = list(setup)
    wrong[1] = str(uuid4())
    assert setup[0].get(PREFIX + route(wrong, "/assets/" + p["id"])).status_code == 404
    with TestClient(create_app(setup[2], setup[4])) as c:
        assert c.get(PREFIX + route(setup)).status_code == 401


@pytest.fixture
def cloud_contract(setup):
    """Isolated persisted real-cloud contract fixture, no real HTTP/model verification."""
    c, pid, settings, app, provider = setup
    provider.source = "real_cloud"
    preview, permission, body = rh.prepare_run(c, pid)
    ids = []
    for i, key in enumerate(list(FIELD_LABELS)[:5]):
        if i:
            preview = rh.post(
                c,
                rh.ai(pid, "preview"),
                {"expected_revision": body["expected_revision"], "selected_fields": [key]},
            ).json()
            permission = rh.post(
                c,
                rh.ai(pid, "authorize"),
                {
                    "preview_id": preview["id"],
                    "expected_hash": preview["content_hash"],
                    "nonsensitive_confirmed": True,
                },
            ).json()
        run = rh.post(
            c, rh.ai(pid, "runs"), {**body, "preview_id": preview["id"], "authorization_id": permission["id"]}
        ).json()
        models.process_run(app.state.sessions, settings, provider, run["id"])
        ids.append(run["id"])
    data = [
        dict(
            id="S" + str(i + 1),
            title="公开契约样例" + str(i + 1),
            data_source="public",
            source_ref="隔离契约测试资料非真实云验证",
            expected="覆盖明确字段并输出结构候选",
            mode="platform_run",
            run_id=id,
        )
        for i, id in enumerate(ids)
    ]
    yield setup, data


def test_cloud_receipts_scoring_standard_and_hard_error_contract_only(cloud_contract):
    ex, data = cloud_contract
    p = plan(ex, samples=data, minimum_score=2.0).json()
    assert all(s["verified_real"] for s in p["payload"]["samples"])
    assert state(ex)["eligible_real_count"] == 5
    assert confirm(ex, p).status_code == 409
    for s in data:
        assert score(ex, p, sample_id=s["id"]).status_code == 200
    standard = confirm(ex, p)
    assert standard.status_code == 200, standard.json()
    assert state(ex)["groups"][2]["status"] == "passed"
    score(ex, p, hard_errors=[{"kind": "privacy_leak", "reason": "合成隐私泄露硬错误"}])
    v = state(ex)
    assert v["groups"][2]["status"] == "failed" and not v["standard"]["current"]
    assert confirm(ex, p).status_code == 200
    assert state(ex)["groups"][2]["status"] == "failed"
    assert ex[4].calls == 5  # the fixture's isolated FakeProvider, never cloud HTTP


def test_duplicate_run_and_synthetic_not_real_standard(cloud_contract):
    ex, data = cloud_contract
    duplicates = copy.deepcopy(data)
    for s in duplicates:
        s["run_id"] = data[0]["run_id"]
    p = plan(ex, samples=duplicates, minimum_score=2.0).json()
    for s in duplicates:
        score(ex, p, sample_id=s["id"])
    assert state(ex)["eligible_real_count"] == 1 and confirm(ex, p).status_code == 409
    for s in data:
        s["data_source"] = "synthetic"
    p = plan(ex, samples=data, minimum_score=2.0).json()
    for s in data:
        score(ex, p, sample_id=s["id"])
    assert state(ex)["eligible_real_count"] == 0 and confirm(ex, p).status_code == 409


def test_receipt_tamper_and_missing_cloud_evidence_stale(cloud_contract):
    ex, data = cloud_contract
    p = plan(ex, samples=data).json()
    with ex[3].state.sessions.begin() as s:
        receipt = next(r for r in evaluations.receipts(s, ex[1], data[0]["run_id"]))
        receipt.payload = {**receipt.payload, "sent": False}
    assert not state(ex)["plan"]["current"]
    assert score(ex, p).status_code == 409
    p = plan(ex, samples=data).json()
    assert not p["payload"]["samples"][0]["verified_real"]


def test_platform_mock_is_not_real(setup):
    c, pid, settings, app, provider = setup
    _, _, body = rh.prepare_run(c, pid)
    r = rh.post(c, rh.ai(pid, "runs"), body).json()
    models.process_run(app.state.sessions, settings, provider, r["id"])
    data = [
        dict(
            id="S" + str(i),
            title="隔离Mock样例",
            data_source="public",
            source_ref="隔离样例来源",
            expected="结构候选可核对",
            mode="platform_run",
            run_id=r["id"],
        )
        for i in range(1, 6)
    ]
    p = plan(setup, samples=data).json()
    assert all(s["model_source"] == "mock" and not s["verified_real"] for s in p["payload"]["samples"])
    assert state(setup)["eligible_real_count"] == 0


def test_score_quantity_is_separate_and_not_retried(setup):
    p = plan(setup).json()
    s = score(setup, p, quantity_compliant=False, quantity_reason="合成条目数量超过期待，交人工判断").json()
    assert s["payload"]["quantity_compliant"] is False
    assert state(setup)["groups"][0]["quantity_noncompliant"] == 1
    assert setup[4].calls == 0
    assert score(setup, p, quantity_compliant=1).status_code == 422


def test_standard_confirmation_pins_reviewed_scores(cloud_contract):
    ex, data = cloud_contract
    p = plan(ex, samples=data, minimum_score=2.0).json()
    for s in data:
        score(ex, p, sample_id=s["id"])
    old = [s["id"] for s in state(ex)["scores"]]
    score(ex, p)
    assert confirm(ex, p, expected_score_ids=old).json()["error"]["code"] == "REVISION_CONFLICT"
    assert state(ex)["standard"] is None


def test_blank_metadata_and_duplicate_trimmed_dimensions(setup):
    data = samples()
    data[0]["expected"] = "    "
    assert plan(setup, samples=data).status_code == 422
    assert plan(setup, dimensions=["标准", " 标准 "]).status_code == 422
    assert plan(setup, reason="    ").status_code == 422


def test_selected_knowledge_change_stales_and_rejects(setup):
    from backend.tests import test_contexts as ct

    context = ct.save(setup, ["file:README.md"]).json()
    p = plan(setup, context_id=context["id"]).json()
    score(setup, p)
    (ct.root(setup) / "README.md").write_text("知识资料已变化，旧版本必须复验")
    v = state(setup)
    assert not v["plan"]["current"] and not v["scores"][0]["current"]
    assert score(setup, p).status_code == 409
    assert plan(setup, context_id=context["id"]).status_code == 409


def test_nonterminal_or_foreign_run_cannot_be_imported(setup):
    c, pid, _, _, _ = setup
    _, _, body = rh.prepare_run(c, pid)
    r = rh.post(c, rh.ai(pid, "runs"), body).json()
    data = samples()
    data[0] = {k: data[0][k] for k in ("id", "title", "data_source", "source_ref", "expected")}
    data[0].update(mode="platform_run", run_id=r["id"])
    assert plan(setup, samples=data).status_code == 409
    data[0]["run_id"] = str(uuid4())
    assert plan(setup, samples=data).status_code == 404


def test_missing_success_receipt_does_not_count_real(cloud_contract):
    ex, data = cloud_contract
    with ex[3].state.sessions.begin() as session:
        for row in evaluations.receipts(session, ex[1], data[0]["run_id"]):
            row.payload = {**row.payload, "adapter_hash": "changed-adapter"}
    p = plan(ex, samples=data, minimum_score=2.0).json()
    assert not p["payload"]["samples"][0]["verified_real"] and state(ex)["eligible_real_count"] == 4
    for s in data:
        score(ex, p, sample_id=s["id"])
    assert confirm(ex, p).status_code == 409


def test_duplicate_inputs_not_representative(cloud_contract):
    ex, data = cloud_contract
    with ex[3].state.sessions.begin() as session:
        runs = [evaluations.requirements.owned_record(session, d["run_id"], ex[1], "run") for d in data]
        previews = [
            evaluations.requirements.owned_record(session, r.payload["preview_id"], ex[1], "analysis_preview")
            for r in runs
        ]
        for row in previews[1:]:
            row.payload = {**row.payload, "selected_fields": previews[0].payload["selected_fields"]}
    p = plan(ex, samples=data, minimum_score=2.0).json()
    for s in data:
        score(ex, p, sample_id=s["id"])
    assert confirm(ex, p).status_code == 409
    assert any("重复输入" in s for s in state(ex)["blockers"])
    assert state(ex)["eligible_real_count"] == 1


def test_source_record_tamper_never_alters_historical_scores(cloud_contract):
    ex, data = cloud_contract
    p = plan(ex, samples=data).json()
    old = score(ex, p).json()
    with ex[3].state.sessions.begin() as session:
        run = evaluations.requirements.owned_record(session, data[0]["run_id"], ex[1], "run")
        run.payload = {
            **run.payload,
            "candidate": {**run.payload["candidate"], "questions": []},
            "model": "changed-model",
        }
    v = state(ex)
    saved = next(x for x in v["history"] if x["id"] == old["id"])
    assert not saved["current"] and saved["payload"] == old["payload"]
