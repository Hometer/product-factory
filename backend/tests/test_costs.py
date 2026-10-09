import concurrent.futures
import copy
import json
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app import costs, models
from backend.app.db import ModelBudget, RequirementRecord
from backend.app.main import PREFIX, create_app
from backend.tests import test_planning as ph
from backend.tests import test_requirements as rh

setup = rh.setup
prepared = ph.prepared


def route(pid, suffix=""):
    return f"/projects/{pid}/costs{suffix}"


def view(ex):
    r = ex[0].get(PREFIX + route(ex[1]))
    assert r.status_code == 200, r.json()
    return r.json()


def policy(ex, **changes):
    p = view(ex)["policy"]
    return (
        dict(
            previous_id=p["id"],
            expected_hash=p["content_hash"],
            **{k: p["payload"][k] for k in costs.DEFAULT},
            confirmed=True,
            reason="隔离测试收紧已确认全局额度",
            **changes,
        )
        if not changes
        else {**policy(ex), **changes}
    )


def report(**changes):
    return {
        "title": "隔离测试的外部执行账单",
        "scope": "product_development",
        "category": "execution",
        "occurred_on": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
        "stage_id": None,
        "task_id": None,
        "source": "manual_report",
        "status": "pending",
        "estimated_micro": None,
        "actual_micro": None,
        "evidence": [],
        "corrects_id": None,
        "expected_hash": None,
        "confirmed": True,
        "reason": "隔离测试人工台账，不执行外部收费",
        **changes,
    }


def save(ex, suffix, body, key=None):
    return rh.post(ex[0], route(ex[1], "/" + suffix), body, key)


def unknown(ex, *, partial=False):
    _, _, body = rh.prepare_run(ex[0], ex[1])
    if partial:
        ex[4].receipts = [rh.receipt({"invalid": "structure"}), {}]
    else:
        ex[4].errors = [models.ProviderError("NETWORK_UNKNOWN", sent=True)]
    r = rh.post(ex[0], rh.ai(ex[1], "runs"), body)
    assert r.status_code == 202, r.json()
    models.process_run(ex[3].state.sessions, ex[2], ex[4], r.json()["id"])
    return view(ex)["automatic"][0]


def reconciliation(ex, run, **changes):
    p = next((r for r in view(ex)["reconciliations"] if r["payload"]["run_id"] == run["id"]), None)
    return (
        dict(
            run_id=run["id"],
            expected_run_hash=run["content_hash"],
            previous_id=p["id"] if p else None,
            expected_previous_hash=p["content_hash"] if p else None,
            outcome="reconciled",
            actual_micro=500,
            requests_count=1,
            evidence=["隔离账单定位：账单测试第一行"],
            confirmed=True,
            reason="隔离测试人工核对总实费，未发送账单",
            **changes,
        )
        if not changes
        else {**reconciliation(ex, run), **changes}
    )


def test_get_readonly_no_account_or_model_calls(setup):
    with setup[3].state.sessions() as s:
        before = [(r.id, copy.deepcopy(r.payload)) for r in s.scalars(select(RequirementRecord))]
        assert s.get(ModelBudget, models.BUDGET_ID) is None
    v = view(setup)
    assert v["policy"]["payload"] == costs.DEFAULT
    assert len(v["totals"]) == 24 and v["periods"]["execution"] == "statistics_only"
    assert v["budget"]["remaining_micro"] == 100_000_000
    assert view(setup) == v and setup[4].calls == 0
    with setup[3].state.sessions() as s:
        assert s.get(ModelBudget, models.BUDGET_ID) is None
        assert [(r.id, r.payload) for r in s.scalars(select(RequirementRecord))] == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("per_task_micro", 2_000_001),
        ("stage_micro", 100_000_001),
        ("request_limit", 101),
        ("per_task_micro", -1),
        ("stage_micro", True),
        ("request_limit", 1.0),
        ("per_task_micro", "100"),
        ("confirmed", False),
        ("actor", "model"),
    ],
)
def test_policy_hard_caps_strict_and_identity(setup, field, value):
    assert save(setup, "policies", policy(setup, **{field: value})).status_code == 422
    assert not view(setup)["policy_history"] and setup[4].calls == 0


def test_policy_replay_stale_and_no_reset(setup):
    rh.prepare_run(setup[0], setup[1])
    with setup[3].state.sessions.begin() as s:
        a = models.budget(s)
        a.spent_micro, a.reserved_micro, a.requests_used, a.requests_reserved = 800, 200, 5, 2
    b = policy(setup, stage_micro=100)
    key = str(uuid4())
    r = save(setup, "policies", b, key)
    assert r.status_code == 200 and save(setup, "policies", b, key).json() == r.json()
    assert save(setup, "policies", b).status_code == 409
    v = view(setup)
    assert v["budget"]["over_limit"] and v["budget"]["spent_micro"] == 800
    assert v["budget"]["reserved_micro"] == 200 and v["budget"]["requests_used"] == 5
    assert v["budget"]["requests_reserved"] == 2 and len(v["policy_history"]) == 1


@pytest.mark.parametrize("field", ["per_task_micro", "stage_micro", "request_limit"])
def test_suspend_before_reserve_zero_no_send(setup, field):
    _, _, body = rh.prepare_run(setup[0], setup[1])
    assert save(setup, "policies", policy(setup, **{field: 0})).status_code == 200
    r = rh.post(setup[0], rh.ai(setup[1], "runs"), body)
    assert r.status_code == 409 and r.json()["error"]["code"] == "BUDGET_EXCEEDED"
    assert view(setup)["budget"]["reserved_micro"] == 0 and setup[4].calls == 0


def test_queue_policy_change_stops_before_dispatch_and_releases(setup):
    _, _, body = rh.prepare_run(setup[0], setup[1])
    run = rh.post(setup[0], rh.ai(setup[1], "runs"), body).json()
    assert save(setup, "policies", policy(setup, request_limit=0)).status_code == 200
    models.process_run(setup[3].state.sessions, setup[2], setup[4], run["id"])
    v = view(setup)
    assert setup[4].calls == 0 and v["automatic"][0]["state"] == "failed"
    assert v["budget"]["reserved_micro"] == v["budget"]["requests_reserved"] == 0


def test_retry_rechecks_policy_and_counts_first_call(setup):
    _, _, body = rh.prepare_run(setup[0], setup[1])
    run = rh.post(setup[0], rh.ai(setup[1], "runs"), body).json()
    original = setup[4].send

    def send(*args, **kw):
        setup[4].receipts = [rh.receipt({"invalid": "structure"})]
        response = original(*args, **kw)
        assert save(setup, "policies", policy(setup, per_task_micro=0)).status_code == 200
        return response

    setup[4].send = send
    models.process_run(setup[3].state.sessions, setup[2], setup[4], run["id"])
    v = view(setup)
    assert setup[4].calls == 1 and v["budget"]["spent_micro"] == 280
    assert v["budget"]["requests_used"] == 1 and v["budget"]["requests_reserved"] == 0
    assert v["automatic"][0]["synthetic"] and v["periods"]["day"]["actual_micro"] == 0


def test_policy_global_and_parallel_conflict(setup):
    p = rh.post(
        setup[0],
        "/projects/create-preview",
        dict(
            name="预算全局测试",
            target_user="测试负责人",
            core_task="核对共享预算",
            path=str(setup[2].allowed_roots[0] / "other"),
        ),
    ).json()
    other = rh.post(
        setup[0], "/projects/confirm", dict(preview_id=p["preview_id"], expected_hash=p["content_hash"])
    ).json()["id"]
    b = policy(setup, request_limit=80)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: save(setup, "policies", b), range(2)))
    assert sorted(r.status_code for r in results) == [200, 409]
    v = setup[0].get(PREFIX + route(other)).json()
    assert v["policy"]["payload"]["request_limit"] == 80
    assert v["budget"] == view(setup)["budget"]


@pytest.mark.parametrize(
    "changes",
    [
        {"actual_micro": True},
        {"estimated_micro": 0.01},
        {"estimated_micro": "0"},
        {"actual_micro": -1},
        {"estimated_micro": 1_000_000_000_001},
        {"status": "reported_actual", "actual_micro": 0},
        {"actual_micro": 0},
        {"corrects_id": str(uuid4())},
        {"scope": "other"},
        {"confirmed": False},
        {"evidence": ["\ud800"]},
        {"reason": "    "},
        {"title": "   x"},
    ],
)
def test_report_invalid_contract(setup, changes):
    b = report(**changes)
    r = setup[0].post(
        PREFIX + route(setup[1], "/reports"),
        content=json.dumps(b).encode(),
        headers={"Content-Type": "application/json", "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 422, r.json()
    assert not view(setup)["reports"]


@pytest.mark.parametrize("value", ["2026-02-30", "9999-01-01", "0000-01-01"])
def test_invalid_future_date_no_write(setup, value):
    r = save(setup, "reports", report(occurred_on=value))
    assert r.status_code == 400 and r.json()["error"]["code"] == "COST_DATE_INVALID"


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", "password=never-allow-this"),
        ("reason", "联系 person@example.invalid"),
        ("evidence", ["API_KEY=never-allow-this"]),
    ],
)
def test_privacy_in_nested_financial_fact(setup, field, value):
    r = save(setup, "reports", report(**{field: value}))
    assert r.status_code == 409 and r.json()["error"]["code"] == "CONTEXT_EXCLUDED"
    assert not view(setup)["reports"]


def test_classification_unknown_zero_synthetic_and_correction(setup):
    for scope in costs.SCOPES:
        for category in costs.CATEGORIES:
            b = report(
                scope=scope,
                category=category,
                estimated_micro=300,
                actual_micro=200,
                status="reported_actual",
                evidence=["测试独立人工账单第三行"],
            )
            assert save(setup, "reports", b).status_code == 200
    synth = save(
        setup,
        "reports",
        report(
            source="synthetic",
            status="reported_actual",
            actual_micro=900,
            evidence=["仅合成测试演示，没有真实费用"],
        ),
    ).json()
    pending = save(setup, "reports", report()).json()
    v = view(setup)
    assert v["periods"]["day"]["actual_micro"] == 2400 and v["periods"]["day"]["pending_count"] == 1
    assert v["periods"]["day"]["unknown_estimates"] == 1
    b = report(
        corrects_id=pending["id"],
        expected_hash=pending["content_hash"],
        actual_micro=0,
        status="reported_actual",
        evidence=["已核对零费用账单第零行定位"],
    )
    key = str(uuid4())
    corrected = save(setup, "reports", b, key)
    assert corrected.status_code == 200 and save(setup, "reports", b, key).json() == corrected.json()
    assert save(setup, "reports", b).status_code == 409
    v = view(setup)
    assert v["periods"]["day"]["actual_micro"] == 2400 and v["periods"]["day"]["pending_count"] == 0
    assert (
        len(v["reports"]) == 15
        and next(r for r in v["reports"] if r["id"] == pending["id"])["payload"] == pending["payload"]
    )
    b = report(corrects_id=synth["id"], expected_hash=synth["content_hash"], source="manual_report")
    assert save(setup, "reports", b).json()["error"]["code"] == "COST_SOURCE_INVALID"
    assert v["budget"]["spent_micro"] == v["budget"]["reserved_micro"] == 0


def test_report_plan_association_and_old_money_kept(prepared):
    plan = ph.generate_plan(prepared)
    task = plan["payload"]["text"]["tasks"][0]
    r = save(
        prepared,
        "reports",
        report(
            stage_id=task["stage_id"],
            task_id=task["id"],
            actual_micro=500,
            status="reported_actual",
            evidence=["测试账单第一行定位"],
        ),
    ).json()
    assert r["association_current"] and r["payload"]["planning_id"] == plan["id"]
    assert save(prepared, "reports", report(stage_id="S9999", task_id=task["id"])).status_code == 409
    text = copy.deepcopy(plan["payload"]["text"])
    text["stages"][0]["name"] = "计划更改但费用不能消失"
    ph.revise(prepared[0], prepared[1], plan, text)
    v = view(prepared)
    assert not v["reports"][0]["association_current"] and v["periods"]["day"]["actual_micro"] == 500


def test_unknown_hold_pending_then_once_reconcile_and_delta(setup):
    run = unknown(setup)
    assert run["actual_micro"] is None and run["held_micro"] == run["estimated_micro"] > 0
    assert (
        save(
            setup,
            "reconciliations",
            reconciliation(
                setup, run, outcome="pending", actual_micro=None, requests_count=None, evidence=[]
            ),
        ).status_code
        == 200
    )
    assert view(setup)["budget"]["reserved_micro"] == run["estimated_micro"]
    b = reconciliation(setup, run)
    key = str(uuid4())
    result = save(setup, "reconciliations", b, key)
    assert result.status_code == 200 and save(setup, "reconciliations", b, key).json() == result.json()
    assert save(setup, "reconciliations", b).status_code == 409
    v = view(setup)
    assert v["budget"]["spent_micro"] == 500 and v["budget"]["requests_used"] == 1
    assert v["budget"]["reserved_micro"] == v["budget"]["requests_reserved"] == 0
    assert v["automatic"][0]["state"] == "result_unknown" and v["automatic"][0]["held_micro"] == 0
    assert save(setup, "reconciliations", reconciliation(setup, run, actual_micro=200)).status_code == 200
    assert view(setup)["budget"]["spent_micro"] == 200 and len(view(setup)["reconciliations"]) == 3
    assert (
        save(
            setup,
            "reconciliations",
            reconciliation(setup, run, outcome="pending", actual_micro=None, requests_count=None),
        ).status_code
        == 409
    )
    with setup[3].state.sessions() as s:
        row = s.get(RequirementRecord, run["id"])
        assert row.payload["state"] == "result_unknown" and row.payload["candidate"] is None
    assert setup[4].calls == 1


@pytest.mark.parametrize(
    "changes,status",
    [
        ({"requests_count": 0, "actual_micro": 0}, 409),
        ({"requests_count": 3}, 422),
        ({"requests_count": True}, 422),
        ({"actual_micro": 1.1}, 422),
        ({"actual_micro": None}, 422),
        ({"evidence": []}, 422),
        ({"expected_run_hash": "0" * 64}, 409),
        ({"previous_id": str(uuid4())}, 422),
        ({"outcome": "pending"}, 422),
        ({"confirmed": False}, 422),
    ],
)
def test_reconcile_invalid_keeps_hold(setup, changes, status):
    run = unknown(setup)
    assert save(setup, "reconciliations", reconciliation(setup, run, **changes)).status_code == status
    assert view(setup)["budget"]["reserved_micro"] == run["estimated_micro"]
    assert view(setup)["budget"]["spent_micro"] == 0


def test_partial_retry_unknown_requires_two_and_zero_bill_still_counts(setup):
    run = unknown(setup, partial=True)
    assert run["usage_cost_micro"] == 280 and run["actual_micro"] is None
    assert save(setup, "reconciliations", reconciliation(setup, run, requests_count=1)).status_code == 409
    r = save(setup, "reconciliations", reconciliation(setup, run, requests_count=2, actual_micro=0))
    assert r.status_code == 200
    v = view(setup)
    assert v["budget"]["requests_used"] == 2 and v["budget"]["spent_micro"] == 0
    assert v["automatic"][0]["usage_cost_micro"] == 280 and v["automatic"][0]["actual_micro"] == 0


def test_over_hold_actual_bill_counted_not_capped(setup):
    run = unknown(setup)
    assert (
        save(setup, "reconciliations", reconciliation(setup, run, actual_micro=100_000_001)).status_code
        == 200
    )
    v = view(setup)
    assert v["budget"]["spent_micro"] == 100_000_001 and v["budget"]["over_limit"]
    assert v["budget"]["reserved_micro"] == 0 and v["automatic"][0]["state"] == "result_unknown"


def test_parallel_reconcile_once_restart_no_resend_or_rehold(setup):
    run = unknown(setup)
    b = reconciliation(setup, run)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: save(setup, "reconciliations", b), range(2)))
    assert sorted(r.status_code for r in results) == [200, 409]
    before = view(setup)
    with TestClient(create_app(setup[2], setup[4]), headers=dict(setup[0].headers)) as c:
        assert view((c, *setup[1:])) == before
    assert setup[4].calls == 1
    _, _, body = rh.prepare_run(setup[0], setup[1])
    assert rh.post(setup[0], rh.ai(setup[1], "runs"), body).json()["error"]["code"] == "PROJECT_BUSY"


def test_account_underflow_rolls_back_no_financial_record(setup):
    run = unknown(setup)
    with setup[3].state.sessions.begin() as s:
        s.get(ModelBudget, models.BUDGET_ID).requests_reserved = 1
    assert (
        save(setup, "reconciliations", reconciliation(setup, run)).json()["error"]["code"]
        == "COST_ACCOUNT_INVALID"
    )
    v = view(setup)
    assert not v["reconciliations"] and v["budget"]["reserved_micro"] == run["estimated_micro"]


def test_wrong_project_and_not_unknown_cannot_reconcile(setup):
    _, _, body = rh.prepare_run(setup[0], setup[1])
    run = rh.post(setup[0], rh.ai(setup[1], "runs"), body).json()
    row = view(setup)["automatic"][0]
    assert save(setup, "reconciliations", reconciliation(setup, row)).status_code == 409
    b = reconciliation(setup, row)
    b["run_id"] = str(uuid4())
    assert save(setup, "reconciliations", b).status_code == 404
    assert run["state"] == "queued" and setup[4].calls == 0


def test_cross_project_financial_references_rejected(setup):
    run = unknown(setup)
    old = save(setup, "reports", report()).json()
    p = rh.post(
        setup[0],
        "/projects/create-preview",
        dict(
            name="成本归属测试",
            target_user="测试负责人",
            core_task="核对归属",
            path=str(setup[2].allowed_roots[0] / "financial-other"),
        ),
    ).json()
    pid = rh.post(
        setup[0], "/projects/confirm", dict(preview_id=p["preview_id"], expected_hash=p["content_hash"])
    ).json()["id"]
    foreign = (setup[0], pid, *setup[2:])
    b = reconciliation(setup, run)
    assert save(foreign, "reconciliations", b).status_code == 404
    assert (
        save(foreign, "reports", report(corrects_id=old["id"], expected_hash=old["content_hash"])).status_code
        == 404
    )
    assert view(setup)["budget"]["reserved_micro"] == run["estimated_micro"]
    assert not view(foreign)["reports"]


def test_historical_correction_remains_in_original_period(setup):
    old = save(
        setup,
        "reports",
        report(
            occurred_on="2020-01-01",
            status="reported_actual",
            actual_micro=100,
            evidence=["历史账单定位测试第一行"],
        ),
    ).json()
    r = save(
        setup,
        "reports",
        report(
            occurred_on="2020-01-01",
            status="reported_actual",
            actual_micro=200,
            evidence=["更正历史账单定位测试第一行"],
            corrects_id=old["id"],
            expected_hash=old["content_hash"],
        ),
    )
    assert r.status_code == 200
    v = view(setup)
    assert v["periods"]["day"]["actual_micro"] == v["periods"]["month_totals"]["actual_micro"] == 0
    t = next(t for t in v["totals"] if not t["synthetic"] and t["fact_count"])
    assert t["actual_micro"] == 200 and t["fact_count"] == 1


@pytest.mark.parametrize("changed", ["missing", "reserved_micro", "requests_reserved"])
def test_missing_or_inconsistent_hold_stops_dispatch_without_refund(setup, changed):
    _, _, body = rh.prepare_run(setup[0], setup[1])
    run = rh.post(setup[0], rh.ai(setup[1], "runs"), body).json()
    with setup[3].state.sessions.begin() as s:
        account = s.get(ModelBudget, models.BUDGET_ID)
        if changed == "missing":
            s.delete(account)
        else:
            setattr(account, changed, 0)
    before = view(setup)["budget"]
    models.process_run(setup[3].state.sessions, setup[2], setup[4], run["id"])
    v = view(setup)
    assert setup[4].calls == 0 and v["budget"] == before
    assert v["automatic"][0]["state"] == "result_unknown"
    assert not v["automatic"][0]["attempts"]
    result = setup[0].get(PREFIX + rh.ai(setup[1], "runs/" + run["id"])).json()
    assert result["error"]["code"] == "COST_ACCOUNT_INVALID"
