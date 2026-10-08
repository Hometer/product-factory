import copy
import os
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.app import execution, planning
from backend.app.db import ModelBudget
from backend.app.main import PREFIX, create_app
from backend.tests import test_planning as planning_helpers
from backend.tests import test_requirements as requirement_helpers
from backend.tests.test_planning import confirm, generate_plan, route
from backend.tests.test_requirements import post

setup = requirement_helpers.setup
prepared = planning_helpers.prepared


def api(pid, suffix=""):
    return f"/projects/{pid}/execution{suffix}"


@pytest.fixture
def ready(prepared):
    client, pid, settings, app, provider, base, env, tech = prepared
    plan = generate_plan(prepared)
    assert confirm(client, pid, "plans", plan).status_code == 200
    result = post(
        client,
        api(pid, "/plans/generate"),
        {
            "planning_id": plan["id"],
            "task_id": "T1",
            "expected_revision": 0,
        },
    )
    assert result.status_code == 200, result.json()
    yield client, pid, settings, app, provider, plan, result.json()


def state(client, pid):
    result = client.get(PREFIX + api(pid))
    assert result.status_code == 200, result.json()
    return result.json()


def save(ready, text=None, **extra):
    client, pid, _, _, _, plan, row = ready
    body = {
        "planning_id": plan["id"],
        "task_id": row["task_id"],
        "expected_revision": row["revision"],
        "text": text or copy.deepcopy(row["payload"]["text"]),
        **extra,
    }
    return post(client, api(pid, "/plans/revisions"), body)


def review(client, pid, row, action="check", key=None):
    body = {"execution_id": row["id"], "expected_hash": row["content_hash"]}
    if action == "authorize":
        body["reason"] = "测试本地范围准备，不启动执行"
    return post(client, api(pid, "/" + action), body, key)


def with_file(ready):
    text = copy.deepcopy(ready[-1]["payload"]["text"])
    text["files"] = ["src/example.txt"]
    result = save(ready, text)
    assert result.status_code == 200, result.json()
    return result.json()


def test_empty_workspace_and_missing_source(setup):
    client, pid, _, _, provider = setup
    assert state(client, pid)["tasks"] == []
    assert (
        post(
            client,
            api(pid, "/plans/generate"),
            {
                "planning_id": "missing",
                "task_id": "T1",
                "expected_revision": 0,
            },
        ).status_code
        == 404
    )
    assert provider.calls == 0


def test_whole_chain_blocked_no_executor_recovery_and_no_cost(ready):
    client, pid, settings, _, provider, _, _ = ready
    row = with_file(ready)
    grant = review(client, pid, row, "authorize").json()
    checked = review(client, pid, row).json()
    assert checked["payload"]["can_start"] is False
    values = {c["key"]: c for c in checked["payload"]["checks"]}
    assert values["authorization"]["status"] == "passed"
    assert values["executor"]["status"] == "blocked"
    assert values["occupancy"]["status"] == "pending"
    assert len(row["payload"]["required_checks"]) == 10
    assert row["payload"]["acceptance"]["R1"]
    with TestClient(create_app(settings, provider), headers=dict(client.headers)) as reopened:
        entry = state(reopened, pid)["entries"][0]
        assert entry["plan"]["id"] == row["id"]
        assert entry["check"]["id"] == checked["id"]
        assert entry["authorization"]["id"] == grant["id"]
        assert post(reopened, api(pid, "/execute"), {}).status_code == 404
    assert provider.calls == 0


def test_missing_files_authorization_blocked_and_dependency_pending(ready):
    client, pid, _, _, _, plan, row = ready
    assert review(client, pid, row, "authorize").status_code == 409
    matrix = review(client, pid, row).json()["payload"]
    assert next(c for c in matrix["checks"] if c["key"] == "write_scope")["status"] == "blocked"
    row2 = post(
        client,
        api(pid, "/plans/generate"),
        {
            "planning_id": plan["id"],
            "task_id": "T2",
            "expected_revision": 0,
        },
    ).json()
    checked = review(client, pid, row2).json()["payload"]
    assert next(c for c in checked["checks"] if c["key"] == "dependencies")["status"] == "pending"


def test_idempotency_conflict_and_immutable_assets(ready):
    client, pid, _, app, _, plan, row = ready
    body = {
        "planning_id": plan["id"],
        "task_id": "T1",
        "expected_revision": 1,
        "text": copy.deepcopy(row["payload"]["text"]),
    }
    a = post(client, api(pid, "/plans/revisions"), body, "execution-save")
    assert a.status_code == 200
    assert post(client, api(pid, "/plans/revisions"), body, "execution-save").json() == a.json()
    assert post(client, api(pid, "/plans/revisions"), body).status_code == 409
    body["text"]["risks"] = "改变风险后的请求不能用旧幂等键"
    assert post(client, api(pid, "/plans/revisions"), body, "execution-save").status_code == 409
    assert client.get(PREFIX + api(pid, "/assets/" + row["id"])).json() == row
    assert review(client, pid, row).status_code == 409
    checked = review(client, pid, a.json(), key="execution-check").json()
    assert review(client, pid, a.json(), key="execution-check").json() == checked
    with app.state.sessions() as s:
        assert len(execution.rows(s, pid, "T1", "plan")) == 2


def test_validation_only_revision_inherits_authorization(ready):
    client, pid, _, _, _, plan, _ = ready
    row = with_file(ready)
    grant = review(client, pid, row, "authorize").json()
    text = copy.deepcopy(row["payload"]["text"])
    text["steps"][0]["validation"] = "补充接口复查与截图，原验收保持"
    updated = save((*ready[:-1], row), text).json()
    assert updated["payload"]["authorization_hash"] == row["payload"]["authorization_hash"]
    assert state(client, pid)["entries"][0]["authorization"]["id"] == grant["id"]
    text["steps"][0]["instruction"] = "实际操作范围改变，不能沿用授权"
    changed = save((*ready[:-1], updated), text).json()
    assert changed["payload"]["authorization_hash"] != updated["payload"]["authorization_hash"]
    assert state(client, pid)["entries"][0]["authorization"] is None
    assert changed["payload"]["inputs"]["planning_id"] == plan["id"]


def test_revoke_is_durable_and_can_reauthorize(ready):
    client, pid, _, _, _, _, _ = ready
    row = with_file(ready)
    grant = review(client, pid, row, "authorize").json()
    assert review(client, pid, row, "authorize").json() == grant
    body = {"authorization_id": grant["id"], "reason": "撤销准备中的本地范围"}
    revoked = post(client, api(pid, "/revoke"), body).json()
    assert post(client, api(pid, "/revoke"), body).json() == revoked
    assert state(client, pid)["entries"][0]["authorization"] is None
    new = review(client, pid, row, "authorize").json()
    assert new["id"] != grant["id"] and new["revision"] == 2
    assert state(client, pid)["entries"][0]["authorization"]["id"] == new["id"]
    assert client.get(PREFIX + api(pid, "/assets/" + grant["id"])).json() == grant


@pytest.mark.parametrize("operation", ["model_call", "dependency_install", "deploy", "irreversible"])
def test_high_risk_description_not_execution_authorization(ready, operation):
    client, pid, _, _, provider, _, _ = ready
    text = copy.deepcopy(ready[-1]["payload"]["text"])
    text["steps"][0]["operation"] = operation
    row = save(ready, text).json()
    assert review(client, pid, row, "authorize").status_code == 409
    checks = review(client, pid, row).json()["payload"]["checks"]
    assert next(c for c in checks if c["key"] == "authorization")["status"] == "blocked"
    assert provider.calls == 0


def test_budget_facts_not_reset_or_reserved_for_preparation(ready):
    client, pid, _, app, _, _, _ = ready
    from backend.app.models import BUDGET_ID

    with app.state.sessions.begin() as s:
        old = s.get(ModelBudget, BUDGET_ID)
        if not old:
            old = ModelBudget(
                id=BUDGET_ID,
                spent_micro=90_000_000,
                reserved_micro=8_000_000,
                requests_used=99,
                requests_reserved=1,
            )
            s.add(old)
        else:
            old.spent_micro, old.reserved_micro, old.requests_used, old.requests_reserved = (
                90_000_000,
                8_000_000,
                99,
                1,
            )
    text = copy.deepcopy(ready[-1]["payload"]["text"])
    text["estimate_micro"] = 3_000_000
    row = save(ready, text).json()
    assert review(client, pid, row, "authorize").status_code == 409
    checked = review(client, pid, row).json()["payload"]
    assert checked["budget"]["reserved_micro"] == 8_000_000
    assert next(c for c in checked["checks"] if c["key"] == "budget_limit")["status"] == "blocked"
    with app.state.sessions() as s:
        assert s.get(ModelBudget, BUDGET_ID).requests_reserved == 1


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        "/tmp/file.txt",
        ".env",
        "data/private.txt",
        ".git/config",
        "node_modules/pkg/a.js",
        "secrets.json",
        "backup.sqlite3",
        "src//a.txt",
        "src\\a.txt",
    ],
)
def test_protected_and_escaping_files_rejected(ready, path):
    text = copy.deepcopy(ready[-1]["payload"]["text"])
    text["files"] = [path]
    assert save(ready, text).status_code == 400


@pytest.mark.parametrize("kind", ["symlink", "parent_link", "fifo", "oversized", "secret", "hardlink"])
def test_unsafe_file_never_read_or_persisted(ready, kind, tmp_path):
    _, _, settings, _, _, _, _ = ready
    root = settings.allowed_roots[0] / "demo"
    outside = tmp_path / "outside"
    outside.write_text("outside remains")
    name = "candidate.txt"
    path = root / name
    if kind == "symlink":
        path.symlink_to(outside)
    elif kind == "parent_link":
        outside_dir = tmp_path / "elsewhere"
        outside_dir.mkdir()
        (outside_dir / "candidate.txt").write_text("not authorized")
        (root / "linked").symlink_to(outside_dir, target_is_directory=True)
        name = "linked/candidate.txt"
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "oversized":
        path.write_bytes(b"x" * 262145)
    elif kind == "secret":
        path.write_text('{"api_key":"not-a-real-test-only-value"}')
    else:
        os.link(outside, path)
    text = copy.deepcopy(ready[-1]["payload"]["text"])
    text["files"] = [name]
    assert save(ready, text).status_code == 409
    assert outside.read_text() == "outside remains"


def test_external_file_change_blocks_check_and_grant(ready):
    client, pid, settings, _, _, _, _ = ready
    root = settings.allowed_roots[0] / "demo"
    (root / "src").mkdir()
    target = root / "src/example.txt"
    target.write_text("first revision")
    row = with_file(ready)
    target.write_text("external second revision")
    checked = review(client, pid, row).json()["payload"]
    assert next(c for c in checked["checks"] if c["key"] == "files")["status"] == "blocked"
    assert review(client, pid, row, "authorize").status_code == 409
    assert target.read_text() == "external second revision"
    updated = save((*ready[:-1], row)).json()
    assert updated["payload"]["files"][0]["sha256"] != row["payload"]["files"][0]["sha256"]
    assert review(client, pid, updated, "authorize").status_code == 200


def test_environment_expired_checks_preserved_and_grant_rejected(ready):
    client, pid, _, app, _, _, _ = ready
    row = with_file(ready)
    with app.state.sessions.begin() as s:
        env = planning.owned(s, pid, row["payload"]["inputs"]["environment_id"])
        env.created_at = datetime.now(UTC) - timedelta(minutes=16)
    checked = review(client, pid, row).json()["payload"]
    assert next(c for c in checked["checks"] if c["key"] == "source")["status"] == "blocked"
    assert review(client, pid, row, "authorize").status_code == 409
    assert state(client, pid)["source_issues"]
    assert save((*ready[:-1], row)).status_code == 409


def test_new_unconfirmed_plan_stales_execution_inputs(ready):
    client, pid, _, _, _, plan, row = ready
    result = post(
        client,
        route(pid, "/plans/revisions"),
        {
            "expected_revision": plan["revision"],
            "technical_id": plan["payload"]["technical_id"],
            "text": plan["payload"]["text"],
        },
    )
    assert result.status_code == 200
    checked = review(client, pid, row).json()["payload"]
    assert next(c for c in checked["checks"] if c["key"] == "source")["status"] == "blocked"


def test_cross_project_history_and_confirmation_forbidden(ready):
    client, pid, settings, _, _, _, row = ready
    preview = post(
        client,
        "/projects/create-preview",
        {
            "name": "其他准备项目",
            "target_user": "其他用户",
            "core_task": "不可跨用",
            "path": str(settings.allowed_roots[0] / "other"),
        },
    ).json()
    other = post(
        client,
        "/projects/confirm",
        {
            "preview_id": preview["preview_id"],
            "expected_hash": preview["content_hash"],
        },
    ).json()["id"]
    assert client.get(PREFIX + api(other, "/assets/" + row["id"])).status_code == 404
    assert review(client, other, row).status_code == 404
    assert (
        post(
            client, api(other, "/revoke"), {"authorization_id": row["id"], "reason": "不能跨项目撤销"}
        ).status_code
        == 404
    )
    assert state(client, pid)["entries"][0]["plan"]["id"] == row["id"]


@pytest.mark.parametrize("mutation", ["empty", "whitespace", "duplicate", "extra", "bool_cost", "too_many"])
def test_strict_contract_rejects_forged_readiness(ready, mutation):
    text = copy.deepcopy(ready[-1]["payload"]["text"])
    if mutation == "empty":
        text["steps"] = []
    elif mutation == "whitespace":
        text["recovery"] = "    "
    elif mutation == "duplicate":
        text["steps"] *= 2
    elif mutation == "extra":
        text["can_start"] = True
    elif mutation == "bool_cost":
        text["estimate_micro"] = True
    else:
        text["files"] = [f"src/{i}.txt" for i in range(21)]
    assert save(ready, text).status_code == 422


def test_read_only_requires_no_grant_but_never_auto_executes(ready):
    client, pid, _, _, _, _, _ = ready
    text = copy.deepcopy(ready[-1]["payload"]["text"])
    text["steps"][0]["operation"] = "read_only"
    row = save(ready, text).json()
    checked = review(client, pid, row).json()["payload"]
    assert next(c for c in checked["checks"] if c["key"] == "authorization")["status"] == "passed"
    assert checked["can_start"] is False


def test_secret_description_and_batch_limits_rejected(ready):
    text = copy.deepcopy(ready[-1]["payload"]["text"])
    text["modifications"] = "api_key=not-a-real-test-only-key"
    result = save(ready, text)
    assert result.status_code == 400 and "not-a-real-test-only-key" not in result.text
    root = ready[2].allowed_roots[0] / "demo"
    for i in range(9):
        (root / f"{i}.txt").write_bytes(b"x" * 262144)
    text = copy.deepcopy(ready[-1]["payload"]["text"])
    text["files"] = [f"{i}.txt" for i in range(9)]
    assert save(ready, text).status_code == 409


def test_wrong_hash_and_unknown_task_rejected(ready):
    client, pid, _, _, _, plan, row = ready
    assert (
        post(client, api(pid, "/check"), {"execution_id": row["id"], "expected_hash": "0" * 64}).status_code
        == 409
    )
    assert (
        post(
            client,
            api(pid, "/plans/generate"),
            {
                "planning_id": plan["id"],
                "task_id": "T999",
                "expected_revision": 0,
            },
        ).status_code
        == 404
    )


@pytest.mark.parametrize("changed_task,inherit", [("T1", False), ("T2", True)])
def test_only_affected_task_scope_requires_reauthorization(ready, changed_task, inherit):
    client, pid, _, _, _, plan, _ = ready
    row = with_file(ready)
    grant = review(client, pid, row, "authorize").json()
    text = copy.deepcopy(plan["payload"]["text"])
    next(t for t in text["tasks"] if t["id"] == changed_task)["title"] = "核对后改动的任务范围"
    changed = post(
        client,
        route(pid, "/plans/revisions"),
        {
            "expected_revision": plan["revision"],
            "technical_id": plan["payload"]["technical_id"],
            "text": text,
        },
    ).json()
    assert confirm(client, pid, "plans", changed).status_code == 200
    revised = post(
        client,
        api(pid, "/plans/revisions"),
        {
            "planning_id": changed["id"],
            "task_id": "T1",
            "expected_revision": row["revision"],
            "text": row["payload"]["text"],
        },
    )
    assert revised.status_code == 200, revised.json()
    auth = state(client, pid)["entries"][0]["authorization"]
    assert (auth is not None) == inherit
    if inherit:
        assert auth["id"] == grant["id"]
