"""FR22 safety, version pinning and invalidation regressions (isolated mock only)."""

import concurrent.futures
import copy
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app import templates
from backend.app.db import RequirementRecord
from backend.app.main import PREFIX, create_app
from backend.tests import test_acceptance as ah
from backend.tests import test_execution as eh
from backend.tests import test_planning as ph
from backend.tests import test_requirements as rh

setup = rh.setup
prepared = ph.prepared
ready = eh.ready


def route(pid, suffix=""):
    return f"/projects/{pid}/templates{suffix}"


def state(ex):
    r = ex[0].get(PREFIX + route(ex[1]))
    assert r.status_code == 200, r.json()
    return r.json()


def post(ex, kind, body, key=None):
    return rh.post(
        ex[0],
        route(ex[1], "/" + kind),
        dict(confirmed=True, reason="隔离合成复核，不冒充真实模型验收", **body),
        key,
    )


def draft(ex, **changes):
    b = copy.deepcopy(templates.BUILTIN)
    b.update(version="local-1.0.1", **changes)
    r = post(ex, "drafts", b)
    assert r.status_code == 200, r.json()
    return r.json()


def ref(a):
    return dict(asset_id=a["id"], expected_hash=a["content_hash"])


def publish(ex, d=None):
    d = d or draft(ex)
    v = post(ex, "validations", ref(d)).json()
    r = post(ex, "publications", dict(**ref(d), validation_id=v["id"], validation_hash=v["content_hash"]))
    assert r.status_code == 200, r.json()
    return r.json()


def customize(ex, component="stage_strategy", content=None, **changes):
    return post(
        ex,
        "customizations",
        dict(
            expected_state_hash=state(ex)["state_hash"],
            component=component,
            action="set",
            content=content or "# 项目定制阶段\n沿用当前任务与需求，原验收须依照证据核对。",
            **changes,
        ),
    )


def preview(ex, publication, selected=None):
    return post(
        ex,
        "previews",
        dict(
            expected_state_hash=state(ex)["state_hash"],
            target_id=publication["id"],
            target_hash=templates.digest(publication["payload"]["bundle"]),
            selected_components=selected or list(templates.LABELS),
        ),
    )


def decision(ex, p, outcome="apply", key=None):
    return post(ex, "decisions", dict(**ref(p), outcome=outcome), key)


def test_initial_pin_readonly_restart_and_no_calls(setup):
    s = state(setup)
    assert s["binding"]["payload"]["target_id"] == templates.BUILTIN_ID
    assert len(s["components"]) == 7 and not s["stamp"] and not s["blockers"]
    before = len(s["history"])
    assert len(state(setup)["history"]) == before
    with TestClient(create_app(setup[2], setup[4]), headers=dict(setup[0].headers)) as c:
        assert state((c, setup[1])) == s
    assert setup[4].calls == 0


def test_legacy_read_does_not_backfill(setup):
    with setup[3].state.sessions.begin() as s:
        row = templates.latest(s, setup[1], "template_binding")
        s.delete(row)  # simulate legacy fixture, never touches product data
    assert state(setup)["binding"] is None
    assert state(setup)["history"] == []


@pytest.mark.parametrize(
    "key,value",
    [
        ("prd_structure", "null"),
        ("prd_structure", "[{}]"),
        ("task_recipes", '["shell"]'),
        ("task_recipes", "null"),
        ("task_recipes", '["python_syntax","python_syntax"]'),
        ("acceptance_checks", "null"),
        ("acceptance_checks", '{"invalid":true}'),
        ("output_schema", "null"),
        ("output_schema", '{"schema_version":true}'),
        ("rule_card", "短正文无标题"),
        ("stage_strategy", "# 太短"),
        ("technical_manual", "缺标题，未经结构审核不能发布。"),
    ],
)
def test_damage_saved_failed_but_not_publishable(setup, key, value):
    b = copy.deepcopy(templates.BUILTIN["components"])
    b[key] = value
    if len(value) < 4:  # request structural bound is tested separately
        value = value + " " * (4 - len(value))
        b[key] = value
    d = draft(setup, components=b)
    v = post(setup, "validations", ref(d)).json()
    assert not v["payload"]["result"]["passed"]
    r = post(setup, "publications", dict(**ref(d), validation_id=v["id"], validation_hash=v["content_hash"]))
    assert r.status_code == 409 and r.json()["error"]["code"] == "TEMPLATE_INVALID"
    assert len(state(setup)["catalogue"]) == 1


@pytest.mark.parametrize(
    "change",
    [
        dict(confirmed=False),
        dict(confirmed="true"),
        dict(extra="no"),
        dict(module_policy="force_all"),
        dict(storage_policy="sqlite_all"),
        dict(terminal_policy="web_all"),
        dict(version="latest"),
        dict(components={}),
        dict(reason="    "),
    ],
)
def test_strict_request_contract(setup, change):
    b = dict(copy.deepcopy(templates.BUILTIN), confirmed=True, reason="隔离合成明确确认")
    b.update(change)
    assert rh.post(setup[0], route(setup[1], "/drafts"), b).status_code == 422


def test_incompatible_and_boolean_output_rejected(setup):
    d = draft(setup, compatibility="foreign_contract")
    v = post(setup, "validations", ref(d)).json()
    assert not v["payload"]["result"]["passed"]
    b = copy.deepcopy(templates.BUILTIN)
    output = json.loads(b["components"]["output_schema"])
    output["schema_version"] = True
    b["components"]["output_schema"] = json.dumps(output)
    assert not templates.validate(b)["passed"]


def test_sensitive_content_no_persist(setup):
    b = copy.deepcopy(templates.BUILTIN["components"])
    b["rule_card"] += "\n联系人手机号 13800138000"
    r = post(setup, "drafts", {**templates.BUILTIN, "components": b})
    assert r.status_code == 409
    assert not any(h["kind"] == "template_draft" for h in state(setup)["history"])


def test_publish_does_not_apply_unique_hash_and_idempotency(setup):
    components = copy.deepcopy(templates.BUILTIN["components"])
    components["technical_manual"] += "\n补充说明：按项目需求核对兼容性。"
    before = state(setup)
    d = draft(setup, components=components)
    v = post(setup, "validations", ref(d)).json()
    b = dict(**ref(d), validation_id=v["id"], validation_hash=v["content_hash"])
    p = post(setup, "publications", b, "publish-replay").json()
    assert post(setup, "publications", b, "publish-replay").json() == p
    assert post(setup, "publications", b).status_code == 409
    after = state(setup)
    assert after["components"] == before["components"] and after["state_hash"] == before["state_hash"]
    assert len(after["catalogue"]) == 2
    assert post(setup, "publications", dict(b, validation_hash="0" * 64)).status_code == 409


def test_partial_preserve_custom_defer_and_replay(setup):
    components = copy.deepcopy(templates.BUILTIN["components"])
    components["technical_manual"] += "\n新增本项目适配检查说明。"
    components["stage_strategy"] += "\n公共更新的额外阶段说明。"
    pub = publish(setup, draft(setup, components=components))
    assert customize(setup).status_code == 200
    before = state(setup)
    p = preview(setup, pub, ["stage_strategy", "technical_manual"]).json()
    assert p["payload"]["diffs"][0]["preserved_custom"]
    assert p["payload"]["affected_domains"] and p["payload"]["migration_steps"]
    d = decision(setup, p, "defer").json()
    assert d["payload"]["binding_id"] is None and state(setup)["state_hash"] == before["state_hash"]
    assert decision(setup, p).status_code == 409
    p = preview(setup, pub, ["stage_strategy", "technical_manual"]).json()
    applied = decision(setup, p, key="apply-once").json()
    assert decision(setup, p, key="apply-once").json() == applied
    after = state(setup)
    assert after["components"]["stage_strategy"] == before["components"]["stage_strategy"]
    assert after["components"]["technical_manual"] == components["technical_manual"]
    assert after["components"]["rule_card"] == before["components"]["rule_card"]
    assert after["origins"]["rule_card"] == templates.BUILTIN_ID
    assert after["origins"]["technical_manual"] == pub["id"]
    assert after["blockers"] and after["stamp"] != before["stamp"]
    r = post(
        setup,
        "customizations",
        dict(
            expected_state_hash=after["state_hash"], component="stage_strategy", action="reset", content=None
        ),
    )
    assert (
        r.status_code == 200 and state(setup)["components"]["stage_strategy"] == components["stage_strategy"]
    )


def test_same_content_and_defer_do_not_invalidate(setup):
    p = publish(setup)
    before = state(setup)
    v = preview(setup, p).json()
    assert decision(setup, v).status_code == 200
    assert state(setup)["stamp"] is None and not state(setup)["blockers"]
    assert state(setup)["components"] == before["components"]


def test_stale_binding_target_preview_and_cross_project(setup):
    pub = publish(setup)
    p = preview(setup, pub).json()
    old = state(setup)["state_hash"]
    assert customize(setup).status_code == 200
    assert decision(setup, p).status_code == 409
    assert (
        post(
            setup,
            "customizations",
            dict(expected_state_hash=old, component="rule_card", action="reset", content=None),
        ).status_code
        == 409
    )
    assert (
        post(
            setup,
            "previews",
            dict(
                expected_state_hash=state(setup)["state_hash"],
                target_id=pub["id"],
                target_hash="0" * 64,
                selected_components=["rule_card"],
            ),
        ).status_code
        == 409
    )
    with setup[3].state.sessions.begin() as s:
        from backend.app.db import Project

        root = setup[2].allowed_roots[0] / "other"
        root.mkdir()
        other = Project(id=str(uuid4()), canonical_path=str(root), payload={})
        s.add(other)
        s.flush()
        oid = other.id
    response = rh.post(
        setup[0], route(oid, "/validations"), dict(**ref(pub), reason="跨项目不可读取草案", confirmed=True)
    )
    assert response.status_code == 404
    assert any(c["id"] == pub["id"] for c in state((setup[0], oid))["catalogue"])


def test_concurrent_updates_one_winner(setup):
    body = dict(
        expected_state_hash=state(setup)["state_hash"],
        component="stage_strategy",
        action="set",
        content="# 并发定制规则\n保持原功能和安全约束，冲突时不得覆盖已保存内容。",
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: post(setup, "customizations", body), range(2)))
    assert sorted(r.status_code for r in results) == [200, 409]


@pytest.mark.parametrize("busy_state", ["queued", "running", "result_unknown"])
def test_unknown_or_active_model_blocks_effective_update(setup, busy_state):
    with setup[3].state.sessions.begin() as s:
        rh.models.record(s, setup[1], "run", dict(state=busy_state))
    r = customize(setup)
    assert r.status_code == 409 and r.json()["error"]["code"] == "TEMPLATE_BUSY"
    assert state(setup)["stamp"] is None


def test_context_invalidation_plan_review_and_revision(prepared):
    plan = ph.generate_plan(prepared)
    c, pid = prepared[:2]
    old = c.get(PREFIX + f"/projects/{pid}/context?stage=planning").json()
    assert customize(prepared).status_code == 200
    after = c.get(PREFIX + f"/projects/{pid}/context?stage=planning").json()
    assert old["index_hash"] != after["index_hash"] and after["basis"]["template_stamp"]
    assert any(s["kind"] == "template" for s in after["sources"])
    s = state(prepared)
    tasks = [
        dict(task_id=t["id"], status="unknown", reason="尚未核对当前模板与任务兼容性") for t in s["tasks"]
    ]
    b = dict(expected_review_hash=s["review_hash"], tasks=tasks, old_acceptance_acknowledged=True)
    assert post(prepared, "reviews", dict(b, tasks=[])).status_code == 409
    assert post(prepared, "reviews", dict(b, old_acceptance_acknowledged=False)).status_code == 422
    assert post(prepared, "reviews", b).status_code == 200 and state(prepared)["blockers"]
    for t in tasks:
        t["status"] = "inheritable"
    assert (
        post(prepared, "reviews", dict(b, tasks=tasks)).status_code == 200 and not state(prepared)["blockers"]
    )
    assert not ph.state(c, pid)["plan_issues"]
    text = copy.deepcopy(plan["payload"]["text"])
    text["tasks"][0]["title"] += "修订"
    ph.revise(c, pid, plan, text)
    assert state(prepared)["blockers"]
    assert post(prepared, "reviews", b).status_code == 409


def test_old_acceptance_immutable_stale_and_no_magic_pass(ready):
    before = ah.state(ready)
    p = ah.package(ready).json()
    assert customize(ready).status_code == 200
    after = ah.state(ready)
    assert after["snapshot_hash"] != before["snapshot_hash"]
    assert after["snapshot"]["template_stamp"]
    assert ah.read(ready, p["id"]).json()["payload"] == p["payload"]
    assert ah.decision(ready, p, outcome="passed").status_code == 409
    s = state(ready)
    b = dict(
        expected_review_hash=s["review_hash"],
        tasks=[
            dict(task_id=t["id"], status="inheritable", reason="仅确认任务文字可继承，验证报告需另行重跑")
            for t in s["tasks"]
        ],
        old_acceptance_acknowledged=True,
    )
    assert post(ready, "reviews", b).status_code == 200
    assert ah.decision(ready, p, outcome="passed").status_code == 409
    assert ready[4].calls == 0


def test_restart_full_history_custom_stamp(prepared):
    ph.generate_plan(prepared)
    assert customize(prepared).status_code == 200
    before = state(prepared)
    with TestClient(create_app(prepared[2], prepared[4]), headers=dict(prepared[0].headers)) as c:
        assert state((c, prepared[1])) == before
    with prepared[3].state.sessions() as s:
        assert (
            len(
                list(
                    s.scalars(
                        select(RequirementRecord).where(RequirementRecord.kind == "template_customization")
                    )
                )
            )
            == 1
        )


@pytest.mark.parametrize("value", [{}, None, {"components": {}}, {"components": {"rule_card": "#损坏"}}])
def test_corrupt_bundle_cannot_validate(value):
    assert not templates.validate(value)["passed"]


def test_deep_json_is_failed_validation_not_internal_error(setup):
    b = copy.deepcopy(templates.BUILTIN["components"])
    b["prd_structure"] = "[" * 1500 + "null" + "]" * 1500
    d = draft(setup, components=b)
    v = post(setup, "validations", ref(d))
    assert v.status_code == 200 and not v.json()["payload"]["result"]["passed"]


@pytest.mark.parametrize(
    "kind,busy_state",
    [
        ("command", "running"),
        ("command", "serving"),
        ("command", "paused"),
        ("patch", "preparing"),
        ("patch", "applying"),
        ("patch", "rollback_required"),
    ],
)
def test_local_run_blocks_template_mutation(ready, kind, busy_state):
    from backend.app.db import CommandAsset, CommandRun, PatchAsset, PatchRun

    with ready[3].state.sessions.begin() as s:
        if kind == "command":
            a = CommandAsset(id=str(uuid4()), project_id=ready[1], execution_id=ready[6]["id"], payload={})
            s.add(a)
            s.flush()
            s.add(
                CommandRun(id=str(uuid4()), project_id=ready[1], asset_id=a.id, state=busy_state, details={})
            )
        else:
            a = PatchAsset(id=str(uuid4()), project_id=ready[1], execution_id=ready[6]["id"], payload={})
            s.add(a)
            s.flush()
            s.add(PatchRun(id=str(uuid4()), project_id=ready[1], patch_id=a.id, state=busy_state, details={}))
    r = customize(ready)
    assert r.status_code == 409 and r.json()["error"]["code"] == "TEMPLATE_BUSY"


def test_revert_still_requires_fresh_review(prepared):
    ph.generate_plan(prepared)
    original = state(prepared)["components"]["stage_strategy"]
    assert customize(prepared).status_code == 200
    changed = state(prepared)
    b = dict(
        expected_review_hash=changed["review_hash"],
        tasks=[
            dict(task_id=t["id"], status="inheritable", reason="仅文字兼容核对，不替代原验证")
            for t in changed["tasks"]
        ],
        old_acceptance_acknowledged=True,
    )
    assert post(prepared, "reviews", b).status_code == 200
    assert customize(prepared, content=original).status_code == 200
    after = state(prepared)
    assert after["components"]["stage_strategy"] == original and after["blockers"]
    assert after["stamp"] != changed["stamp"]
    assert post(prepared, "reviews", b).status_code == 409
