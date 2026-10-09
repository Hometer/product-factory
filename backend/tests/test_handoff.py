import concurrent.futures
import copy
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app import handoff
from backend.app.db import AuditEvent, CommandRun, ModelBudget, PlanningAsset, RequirementRecord
from backend.app.main import PREFIX, create_app
from backend.tests import test_acceptance as ah
from backend.tests import test_backups as bh
from backend.tests import test_commands as ch
from backend.tests import test_execution as eh
from backend.tests import test_planning as ph
from backend.tests import test_requirements as rh

setup = rh.setup
prepared = ph.prepared
ready = eh.ready
executable = ch.executable
cloud_contract = ah.cloud_contract
all_green = ah.all_green


def route(pid, suffix=""):
    return f"/projects/{pid}/handoff{suffix}"


def state(c, pid):
    r = c.get(PREFIX + route(pid))
    assert r.status_code == 200, r.json()
    return r.json()


def summary_body(c, pid):
    return dict(
        expected_state_hash=state(c, pid)["current"]["state_hash"],
        confirmed=True,
        reason="隔离接续核对，不批准执行",
    )


def save(c, pid, key=None):
    return rh.post(c, route(pid, "/summaries"), summary_body(c, pid), key)


def event_body(e):
    return dict(
        event_id=e["id"],
        expected_event_hash=e["event_hash"],
        expected_chain_hash=e["chain_hash"],
        note="补充核对说明，原事实与业务结论保留",
        reason="隔离审计更正核对",
        confirmed=True,
    )


def correct(c, pid, e, key=None):
    return rh.post(c, route(pid, "/corrections"), event_body(e), key)


def test_empty_truth_and_no_side_effect(setup):
    c, pid, _, app, provider = setup
    before = state(c, pid)
    after = state(c, pid)
    assert before == after
    s = before["current"]
    assert s["requirement"] is None and not s["completed"] and not s["authorizations"]
    assert s["pending"] and s["tasks"] == [] and "自动" in s["next_step"]
    assert provider.calls == 0
    with app.state.sessions() as session:
        assert (
            list(session.scalars(select(RequirementRecord).where(RequirementRecord.kind.in_(handoff.KINDS))))
            == []
        )
        budget = session.get(ModelBudget, "stage2")
        assert budget is None or budget.requests_used == 0


def test_summary_self_stable_and_immutable(setup):
    c, pid, _, app, _ = setup
    a = save(c, pid).json()
    assert a["current"]
    original = copy.deepcopy(a)
    assert state(c, pid)["current"]["state_hash"] == a["summary"]["state_hash"]
    assert save(c, pid).status_code == 200
    assert len(state(c, pid)["history"]) == 2
    assert c.get(PREFIX + route(pid, f"/summaries/{a['id']}")).json() == original
    with app.state.sessions() as session:
        assert session.get(RequirementRecord, a["id"]).payload["summary"] == a["summary"]


def test_plan_confirmation_not_completion(ready):
    c, pid, *_ = ready
    s = state(c, pid)["current"]
    assert s["requirement"] and s["tasks"] and not s["completed"]
    assert any("效果" in p or "quality" in p for p in s["pending"])
    assert any("S1" in p for p in s["pending"])


def test_new_draft_retains_frozen_reference(ready):
    c, pid, *_ = ready
    old = state(c, pid)["current"]["requirement"]
    rh.save_complete(c, pid)
    s = state(c, pid)["current"]
    assert s["requirement"] == old
    assert s["draft_revision"] > int(old["version"])
    assert any("草稿" in p for p in s["conflicts"])


def test_file_change_invalidates_old_summary(setup):
    c, pid, settings, *_ = setup
    a = save(c, pid).json()
    (settings.allowed_roots[0] / "demo" / "new.md").write_text("new source")
    v = c.get(PREFIX + route(pid, f"/summaries/{a['id']}")).json()
    assert not v["current"] and v["summary"] == a["summary"] and v["issues"]


def test_time_expiry_changes_validity_hash(ready):
    c, pid, _, app, *_ = ready
    a = save(c, pid).json()
    with app.state.sessions.begin() as session:
        env = session.scalar(select(PlanningAsset).where(PlanningAsset.kind == "environment"))
        env.created_at -= timedelta(hours=1)
    assert not c.get(PREFIX + route(pid, f"/summaries/{a['id']}")).json()["current"]
    assert state(c, pid)["current"]["conflicts"]


def test_stale_save_keeps_history(setup):
    c, pid, *_ = setup
    b = summary_body(c, pid)
    rh.save_complete(c, pid)
    r = rh.post(c, route(pid, "/summaries"), b)
    assert r.status_code == 409 and r.json()["error"]["code"] == "REVISION_CONFLICT"
    assert state(c, pid)["history"] == []


def test_summary_idempotency_and_conflict(setup):
    c, pid, *_ = setup
    b, key = summary_body(c, pid), str(uuid4())
    a = rh.post(c, route(pid, "/summaries"), b, key)
    rh.save_complete(c, pid)
    assert rh.post(c, route(pid, "/summaries"), b, key).json() == a.json()
    b["reason"] = "不同提交意图与理由"
    assert rh.post(c, route(pid, "/summaries"), b, key).status_code == 409


def test_audit_legacy_missing_and_new_metadata(setup):
    c, pid, _, app, _ = setup
    with app.state.sessions.begin() as session:
        old = AuditEvent(
            id=str(uuid4()), project_id=pid, action="legacy", details={"document": "raw original document"}
        )
        session.add(old)
    view = state(c, pid)
    event = next(e for e in view["events"] if e["id"] == old.id)
    assert set(event["missing"]) == {"actor", "role", "object", "version", "reason"}
    assert "raw original" not in json.dumps(view)
    a = save(c, pid).json()
    new = next(e for e in state(c, pid)["events"] if e["object"] == a["id"])
    assert not new["missing"] and new["actor"] == "local_operator" and new["version"] == a["content_hash"]
    with app.state.sessions() as session:
        assert session.get(AuditEvent, old.id).details == {"document": "raw original document"}


def test_append_corrections_preserves_original_and_summary(setup):
    c, pid, _, app, _ = setup
    a = save(c, pid).json()
    e = state(c, pid)["events"][0]
    with app.state.sessions() as session:
        old = copy.deepcopy(session.get(AuditEvent, e["id"]).details)
    r = correct(c, pid, e)
    assert r.status_code == 200, r.json()
    v = r.json()
    assert v["event_hash"] == e["event_hash"] and len(v["corrections"]) == 1
    assert correct(c, pid, v).status_code == 200
    assert c.get(PREFIX + route(pid, f"/summaries/{a['id']}")).json()["current"]
    with app.state.sessions() as session:
        assert session.get(AuditEvent, e["id"]).details == old


def test_stale_correction_and_idempotency(setup):
    c, pid, *_ = setup
    e = state(c, pid)["events"][0]
    key = str(uuid4())
    r = correct(c, pid, e, key)
    assert r.status_code == 200
    assert correct(c, pid, e, key).json() == r.json()
    assert correct(c, pid, e).status_code == 409
    v = next(x for x in state(c, pid)["events"] if x["id"] == e["id"])
    assert len(v["corrections"]) == 1


def test_original_event_hash_conflict(setup):
    c, pid, *_ = setup
    b = event_body(state(c, pid)["events"][0])
    b["expected_event_hash"] = "0" * 64
    assert rh.post(c, route(pid, "/corrections"), b).status_code == 409


@pytest.mark.parametrize(
    "text", ["password=synthetic-no-export", "api_key=synthetic-no-export", "个人联系 13800138000"]
)
@pytest.mark.parametrize("target", ["summary", "correction"])
def test_sensitive_input_never_persists(setup, text, target):
    c, pid, _, app, _ = setup
    if target == "summary":
        b = summary_body(c, pid)
        b["reason"] = text
        r = rh.post(c, route(pid, "/summaries"), b)
    else:
        b = event_body(state(c, pid)["events"][0])
        b["note"] = text
        r = rh.post(c, route(pid, "/corrections"), b)
    assert r.status_code == 409 and "HANDOFF_UNSAFE" in r.text
    assert text not in r.text
    with app.state.sessions() as session:
        assert not list(
            session.scalars(select(RequirementRecord).where(RequirementRecord.kind.in_(handoff.KINDS)))
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("confirmed", False),
        ("confirmed", "true"),
        ("reason", " "),
        ("expected_state_hash", "bad"),
        ("extra", "unexpected"),
    ],
)
def test_strict_summary_input(setup, field, value):
    c, pid, *_ = setup
    b = summary_body(c, pid)
    b[field] = value
    assert rh.post(c, route(pid, "/summaries"), b).status_code == 422


def test_actual_credential_not_exported(setup):
    c, pid, settings, app, _ = setup
    with app.state.sessions.begin() as session:
        session.add(
            AuditEvent(
                id=str(uuid4()),
                project_id=pid,
                action="credential legacy",
                details={"reason": settings.token, "document": settings.token},
            )
        )
    view = state(c, pid)
    assert settings.token not in json.dumps(view)
    assert "受保护" in json.dumps(view, ensure_ascii=False)
    b = summary_body(c, pid)
    b["reason"] = settings.token
    assert rh.post(c, route(pid, "/summaries"), b).status_code == 409


def test_sensitive_file_explicitly_pending_no_raw_document(setup):
    c, pid, settings, *_ = setup
    (settings.allowed_roots[0] / "demo" / "notes.md").write_text("password=synthetic-excluded")
    r = save(c, pid)
    assert r.status_code == 200, r.json()
    assert "synthetic-excluded" not in r.text
    assert any("未安全核验" in x for x in r.json()["summary"]["pending"])


def test_download_safe_snapshot_not_current_raw_state(setup):
    c, pid, *_ = setup
    a = save(c, pid).json()
    rh.save_complete(c, pid)
    d = c.get(PREFIX + route(pid, f"/summaries/{a['id']}/download"))
    assert d.status_code == 200 and d.headers["content-type"] == "application/json"
    assert "attachment" in d.headers["content-disposition"]
    v = d.json()
    assert v["asset"]["summary"] == a["summary"] and not v["asset"]["current"]
    assert not any(x in v for x in ["chat", "tables", "details", "files"])


@pytest.mark.parametrize("suffix", ["", "/summaries/missing", "/summaries/missing/download"])
def test_missing_project(setup, suffix):
    c, *_ = setup
    assert c.get(PREFIX + route(str(uuid4()), suffix)).status_code == 404


def test_cross_project_asset_and_event(setup):
    c, pid, settings, *_ = setup
    a = save(c, pid).json()
    e = state(c, pid)["events"][0]
    p = c.post(
        PREFIX + "/projects/create-preview",
        json=dict(
            name="other",
            target_user="owner",
            core_task="other",
            path=str(settings.allowed_roots[0] / "other"),
        ),
    ).json()
    other = rh.post(
        c, "/projects/confirm", dict(preview_id=p["preview_id"], expected_hash=p["content_hash"])
    ).json()["id"]
    for suffix in [f"/summaries/{a['id']}", f"/summaries/{a['id']}/download"]:
        assert c.get(PREFIX + route(other, suffix)).status_code == 404
    assert correct(c, other, e).status_code == 404
    assert all(v["id"] != e["id"] for v in state(c, other)["events"])


def test_archived_read_only_and_existing_download(setup):
    c, pid, *_ = setup
    a = save(c, pid).json()
    assert bh.archive(c, pid).status_code == 200
    v = state(c, pid)
    assert v["current"]["archived"] and not v["history"][0]["current"]
    assert save(c, pid).status_code == 409
    assert correct(c, pid, v["events"][0]).status_code == 409
    assert c.get(PREFIX + route(pid, f"/summaries/{a['id']}/download")).status_code == 200


def test_restore_history_not_authority(setup):
    c, pid, settings, *_ = setup
    _, p = bh.package(c, pid)
    pre = bh.inspect(c, pid, p, settings.allowed_roots[0] / "restored").json()
    r = bh.restore(c, pid, pre)
    assert r.status_code == 200, r.json()
    new = r.json()["payload"]["restored_project_id"]
    v = state(c, new)["current"]
    assert v["requirement"] is None and not v["authorizations"] and not v["completed"]
    assert any("新身份" in x for x in v["conflicts"])


def test_restart_preserves_summary_and_correction(setup):
    c, pid, settings, _, provider = setup
    a = save(c, pid).json()
    e = state(c, pid)["events"][0]
    assert correct(c, pid, e).status_code == 200
    original = state(c, pid)
    with TestClient(create_app(settings, provider), headers=dict(c.headers)) as reopened:
        assert state(reopened, pid) == original
        assert reopened.get(PREFIX + route(pid, f"/summaries/{a['id']}")).json()["current"]


def test_concurrent_correction_one_winner(setup):
    c, pid, *_ = setup
    e = state(c, pid)["events"][0]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: correct(c, pid, e), range(2)))
    assert sorted(r.status_code for r in results) == [200, 409]


def test_command_success_not_completion_and_old_unknown_not_erased(executable):
    c, pid, _, app, *_ = executable
    ch.preview(executable)
    with app.state.sessions.begin() as session:
        from backend.app.db import CommandAsset

        a = session.scalar(select(CommandAsset).where(CommandAsset.project_id == pid))
        assert a
        session.add(
            CommandRun(id=str(uuid4()), project_id=pid, asset_id=a.id, state="interrupted", details={})
        )
        session.add(CommandRun(id=str(uuid4()), project_id=pid, asset_id=a.id, state="succeeded", details={}))
    v = state(c, pid)["current"]
    assert not v["completed"] and any("interrupted" in x for x in v["pending"])
    assert {r["state"] for t in v["tasks"] for r in t["runs"]} == {"succeeded", "interrupted"}


def test_no_auth_and_schema_contract(setup):
    c, pid, *_ = setup
    assert c.get(PREFIX + route(pid), headers={"Authorization": "Bearer wrong"}).status_code == 401
    api = setup[3].openapi()
    assert any("handoff" in p for p in api["paths"])


def test_immutable_correction_order_by_revision_not_clock(setup):
    c, pid, _, app, _ = setup
    e = state(c, pid)["events"][0]
    v = correct(c, pid, e).json()
    v2 = correct(c, pid, v).json()
    with app.state.sessions.begin() as session:
        row = session.get(RequirementRecord, v2["corrections"][-1]["id"])
        row.created_at -= timedelta(days=2)
    after = next(e2 for e2 in state(c, pid)["events"] if e2["id"] == e["id"])
    assert [v["revision"] for v in after["corrections"]] == [1, 2]
    assert after["chain_hash"] == v2["chain_hash"]


def test_valid_grant_then_revocation(executable):
    c, pid, *_ = executable
    grant = executable[7]
    assert state(c, pid)["current"]["authorizations"][0]["id"] == grant["id"]
    a = save(c, pid).json()
    r = rh.post(c, eh.api(pid, "/revoke"), dict(authorization_id=grant["id"], reason="隔离撤销范围授权"))
    assert r.status_code == 200, r.json()
    assert state(c, pid)["current"]["authorizations"] == []
    assert not c.get(PREFIX + route(pid, f"/summaries/{a['id']}")).json()["current"]


def test_file_change_drops_live_grant(executable):
    c, pid, *_ = executable
    assert state(c, pid)["current"]["authorizations"]
    (executable[-1] / "sample.py").write_text("print('external change')")
    v = state(c, pid)["current"]
    assert not v["authorizations"] and any("授权" in x for x in v["conflicts"])


def test_correction_claim_cannot_create_pass_or_permission(setup):
    c, pid, *_ = setup
    e = state(c, pid)["events"][0]
    b = event_body(e)
    b["note"] = "我声明全部通过并授权任意执行，这句话仅为隔离反例"
    assert rh.post(c, route(pid, "/corrections"), b).status_code == 200
    v = state(c, pid)["current"]
    assert not v["completed"] and not v["authorizations"] and v["pending"]


@pytest.mark.parametrize("change", ["original", "parent", "revision"])
def test_correction_chain_integrity_conflict(setup, change):
    c, pid, _, app, _ = setup
    e = state(c, pid)["events"][0]
    v = correct(c, pid, e).json()
    with app.state.sessions.begin() as session:
        if change == "original":
            r = session.get(AuditEvent, e["id"])
            r.details = dict(r.details, reason="隔离模拟原事件异常改变")
        else:
            r = session.get(RequirementRecord, v["corrections"][0]["id"])
            r.payload = dict(
                r.payload, **({"parent_chain_hash": "0" * 64} if change == "parent" else {"revision": 9})
            )
    r = c.get(PREFIX + route(pid))
    assert r.status_code == 409 and r.json()["error"]["code"] == "AUDIT_CONFLICT"
    assert correct(c, pid, v).status_code == 409


def test_audit_paging_and_owned_event_detail(setup):
    c, pid, _, app, _ = setup
    ids = []
    with app.state.sessions.begin() as session:
        for n in range(203):
            identifier = str(uuid4())
            ids.append(identifier)
            session.add(
                AuditEvent(id=identifier, project_id=pid, action="legacy-paged", details={"sequence": n})
            )
    first = state(c, pid)
    second = c.get(PREFIX + route(pid) + "?audit_offset=200").json()
    assert len(first["events"]) == 200
    assert first["audit_total"] == second["audit_total"]
    assert set(e["id"] for e in first["events"]).isdisjoint(e["id"] for e in second["events"])
    identifier = second["events"][-1]["id"]
    r = c.get(PREFIX + route(pid, "/events/" + identifier))
    assert r.status_code == 200
    assert correct(c, pid, r.json()).status_code == 200
    assert c.get(PREFIX + route(str(uuid4()), "/events/" + identifier)).status_code == 404
    assert c.get(PREFIX + route(pid) + "?audit_offset=-1").status_code == 422


@pytest.mark.parametrize(
    "action,actor,role",
    [
        ("command_succeeded", "deterministic_executor", "system"),
        ("analysis_finished", "deterministic_executor", "system"),
        ("requirement_saved", "local_operator", "product_owner"),
    ],
)
def test_new_audit_records_deterministic_actor_role(setup, action, actor, role):
    from backend.app import requirements

    c, pid, _, app, _ = setup
    with app.state.sessions.begin() as session:
        requirements.audit(session, pid, action, {"revision": 2})
    e = next(x for x in state(c, pid)["events"] if x["action"] == action)
    assert e["actor"] == actor and e["role"] == role and e["version"] == "2"
    assert not e["missing"] and e["object"] == pid


@pytest.mark.parametrize("state_name", ["queued", "running", "result_unknown"])
def test_unknown_model_run_remains_pending(setup, state_name):
    c, pid, _, app, _ = setup
    with app.state.sessions.begin() as session:
        session.add(
            RequirementRecord(id=str(uuid4()), project_id=pid, kind="run", payload={"state": state_name})
        )
    v = state(c, pid)["current"]
    assert any(state_name in p for p in v["pending"]) and not v["completed"]


def test_summary_download_limit(setup, monkeypatch):
    c, pid, *_ = setup
    a = save(c, pid).json()
    original = handoff.public

    def large(*args):
        v = original(*args)
        v["reason"] = "x" * (1024 * 1024)
        return v

    monkeypatch.setattr(handoff, "public", large)
    r = c.get(PREFIX + route(pid, f"/summaries/{a['id']}/download"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "HANDOFF_LIMIT"


def test_effective_acceptance_only_then_invalidated(all_green):
    # Isolated FakeProvider contract only; does not certify live model or real acceptance.
    ex, ep, _ = all_green
    c, pid = ex[:2]
    package = ah.package(ex, declarations=ah.declarations("passed")).json()
    assert ah.decision(ex, package, outcome="passed").status_code == 200
    v = state(c, pid)["current"]
    assert len(v["completed"]) == 1 and v["completed"][0]["kind"] == "acceptance_decision"
    a = save(c, pid).json()
    ah.ev.score(ex, ep, hard_errors=[dict(kind="incorrect_fact", reason="隔离反例使原结论需复验")])
    v2 = state(c, pid)["current"]
    assert not v2["completed"] and v2["pending"] and v2["conflicts"]
    assert not c.get(PREFIX + route(pid, f"/summaries/{a['id']}")).json()["current"]


def test_external_authorization_valid_revoked_and_consumed(setup):
    c, pid, *_ = setup
    preview, auth, body = rh.prepare_run(c, pid)
    assert any(g["id"] == auth["id"] for g in state(c, pid)["current"]["authorizations"])
    assert (
        rh.post(
            c,
            rh.ai(pid, "runs"),
            dict(
                expected_revision=body["expected_revision"],
                preview_id=preview["id"],
                authorization_id=auth["id"],
            ),
        ).status_code
        == 202
    )
    assert not state(c, pid)["current"]["authorizations"]


def test_expiry_without_database_changes(ready, monkeypatch):
    from datetime import UTC, datetime

    from backend.app import planning

    c, pid, *_ = ready
    a = save(c, pid).json()

    class Future(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(UTC) + timedelta(hours=1)

    monkeypatch.setattr(planning, "datetime", Future)
    v = c.get(PREFIX + route(pid, f"/summaries/{a['id']}")).json()
    assert not v["current"]  # Source validity, not just a changed DB timestamp, is bound.


def test_audit_reason_version_reference_without_raw_log_duplication(setup):
    from backend.app import requirements

    c, pid, _, app, _ = setup
    with app.state.sessions.begin() as session:
        record = requirements.record(session, pid, "isolated_reason", dict(reason="具体责任人理由来自原版本"))
        requirements.audit(session, pid, "isolated_reason_saved", dict(asset_id=record.id, revision=1))
    e = next(e for e in state(c, pid)["events"] if e["action"] == "isolated_reason_saved")
    assert e["reason"] == "具体责任人理由来自原版本"
    with app.state.sessions.begin() as session:
        original = session.get(AuditEvent, e["id"])
        assert "具体责任人理由来自原版本" not in json.dumps(original.details, ensure_ascii=False)
        r = session.get(RequirementRecord, record.id)
        r.payload = dict(reason="隔离原版本异常被替换")
    e2 = next(v for v in state(c, pid)["events"] if v["id"] == e["id"])
    assert "需核对" in e2["reason"]


def test_archive_suppresses_live_grant(executable):
    c, pid, *_ = executable
    assert state(c, pid)["current"]["authorizations"]
    assert bh.archive(c, pid).status_code == 200
    v = state(c, pid)["current"]
    assert not v["authorizations"] and v["archived"]


def test_replaced_plan_does_not_hide_old_unknown_run(executable):
    c, pid, _, app, *_ = executable
    a = ch.preview(executable)
    run_id = str(uuid4())
    with app.state.sessions.begin() as session:
        session.add(CommandRun(id=run_id, project_id=pid, asset_id=a["id"], state="interrupted", details={}))
    plan = executable[5]
    text = copy.deepcopy(plan["payload"]["text"])
    for t in text["tasks"]:
        if t["id"] == "T1":
            t["id"] = "T9"
        t["depends_on"] = ["T9" if d == "T1" else d for d in t["depends_on"]]
    ph.revise(c, pid, plan, text)
    v = state(c, pid)["current"]
    assert not any(t["id"] == "T1" for t in v["tasks"])
    assert any(run_id in p and "历史命令" in p for p in v["pending"])
    assert not v["completed"]
