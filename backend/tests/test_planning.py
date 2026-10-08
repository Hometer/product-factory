import copy
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import planning
from backend.app.db import PlanningAsset
from backend.app.main import PREFIX, create_app
from backend.tests import test_requirements as requirement_helpers
from backend.tests.test_requirements import post, req, save_complete

setup = requirement_helpers.setup


def route(pid, suffix=""):
    return f"/projects/{pid}/planning{suffix}"


def state(client, pid):
    response = client.get(PREFIX + route(pid))
    assert response.status_code == 200, response.json()
    return response.json()


@pytest.fixture
def prepared(setup, monkeypatch):
    client, pid, settings, app, provider = setup
    monkeypatch.setattr(planning, "trusted_version", lambda name: "22.23.2" if name == "node" else "2.54.0")
    draft = save_complete(client, pid)
    base = post(
        client,
        req(pid, "/freeze"),
        {
            "expected_revision": draft["revision"],
            "expected_hash": draft["content_hash"],
            "reason": "测试需求准备基线",
        },
    ).json()
    env = post(client, route(pid, "/environment"), {}).json()
    response = post(
        client,
        route(pid, "/technical/generate"),
        {
            "expected_revision": 0,
            "baseline_id": base["id"],
            "environment_id": env["id"],
        },
    )
    assert response.status_code == 200, response.json()
    yield client, pid, settings, app, provider, base, env, response.json()


def confirm(client, pid, kind, asset, key=None):
    return post(
        client,
        route(pid, f"/{kind}/confirm"),
        {
            "expected_revision": asset["revision"],
            "expected_hash": asset["content_hash"],
            "reason": "测试人工核对，不代表真实效果或执行",
        },
        key,
    )


def generate_plan(prepared):
    client, pid, _, _, _, _, _, tech = prepared
    assert confirm(client, pid, "technical", tech).status_code == 200
    response = post(
        client, route(pid, "/plans/generate"), {"expected_revision": 0, "technical_id": tech["id"]}
    )
    assert response.status_code == 200, response.json()
    return response.json()


def revise(client, pid, plan, text):
    response = post(
        client,
        route(pid, "/plans/revisions"),
        {
            "expected_revision": plan["revision"],
            "technical_id": plan["payload"]["technical_id"],
            "text": text,
        },
    )
    assert response.status_code == 200, response.json()
    return response.json()


def test_empty_project_environment_without_baseline_cannot_make_technical(setup, monkeypatch):
    client, pid, _, _, provider = setup
    monkeypatch.setattr(planning, "trusted_version", lambda _: "1.2.3")
    assert state(client, pid)["baselines"] == []
    env = post(client, route(pid, "/environment"), {}).json()
    response = post(
        client,
        route(pid, "/technical/generate"),
        {
            "expected_revision": 0,
            "baseline_id": "missing",
            "environment_id": env["id"],
        },
    )
    assert response.status_code == 404 and not state(client, pid)["handoff_ready"]
    assert provider.calls == 0


def test_whole_chain_immutable_approval_and_no_executor(prepared):
    client, pid, settings, _, provider, base, env, tech = prepared
    plan = generate_plan(prepared)
    assert plan["payload"]["source"] == "rule_template"
    assert plan["payload"]["acceptance"] == {"R1": base["items"][0]["acceptance"]}
    assert len(plan["payload"]["required_checks"]) == 10
    approved = confirm(client, pid, "plans", plan).json()
    workspace = state(client, pid)
    assert workspace["handoff_ready"] and not workspace["execution_supported"]
    assert workspace["plan_issues"] == []
    with TestClient(create_app(settings, provider), headers=dict(client.headers)) as reopened:
        assert state(reopened, pid)["plan_approval"]["id"] == approved["id"]
        assert state(reopened, pid)["handoff_ready"]
        assert reopened.get(PREFIX + route(pid, f"/assets/{tech['id']}")).json() == tech
    assert provider.calls == 0
    assert post(client, route(pid, "/execute"), {}).status_code == 404


def test_confirmation_replay_and_same_hash_different_keys(prepared):
    client, pid, _, app, _, _, _, tech = prepared
    a = confirm(client, pid, "technical", tech, "planning-confirm-key")
    assert confirm(client, pid, "technical", tech, "planning-confirm-key").json() == a.json()
    assert confirm(client, pid, "technical", tech).json() == a.json()
    with app.state.sessions() as session:
        assert len(planning.assets(session, pid, "technical_approval")) == 1


def test_save_revision_conflict_and_approval_history(prepared):
    client, pid, _, _, _, base, env, tech = prepared
    old = confirm(client, pid, "technical", tech).json()
    body = {
        "expected_revision": 1,
        "baseline_id": base["id"],
        "environment_id": env["id"],
        "text": {**tech["payload"]["text"], "risks": "用户核对的新风险，旧确认必须保留"},
    }
    revised = post(client, route(pid, "/technical/revisions"), body, "save-planning-key").json()
    assert revised["revision"] == 2 and revised["payload"]["source"] == "user_edited"
    assert post(client, route(pid, "/technical/revisions"), body, "save-planning-key").json() == revised
    assert (
        post(client, route(pid, "/technical/revisions"), body).json()["error"]["code"] == "REVISION_CONFLICT"
    )
    assert state(client, pid)["technical_approval"] is None
    assert client.get(PREFIX + route(pid, "/assets/" + old["id"])).json() == old
    assert (
        post(
            client, route(pid, "/technical/revisions"), {**body, "expected_revision": 2}, "save-planning-key"
        ).json()["error"]["code"]
        == "IDEMPOTENCY_CONFLICT"
    )


def test_new_baseline_invalidates_existing_approvals(prepared):
    client, pid, _, _, _, _, _, tech = prepared
    plan = generate_plan(prepared)
    assert confirm(client, pid, "plans", plan).status_code == 200
    draft = save_complete(client, pid)
    post(
        client,
        req(pid, "/freeze"),
        {
            "expected_revision": draft["revision"],
            "expected_hash": draft["content_hash"],
            "reason": "新的需求基线",
        },
    )
    workspace = state(client, pid)
    assert not workspace["handoff_ready"]
    assert workspace["technical_issues"][0]["code"] == "BASELINE_STALE"
    assert workspace["technical_approval"] and workspace["plan_approval"]
    assert confirm(client, pid, "technical", tech).json()["error"]["code"] == "PLAN_BLOCKED"


def test_new_unfrozen_draft_does_not_implicitly_change_bound_scope(prepared):
    client, pid, _, _, _, _, _, _ = prepared
    plan = generate_plan(prepared)
    confirm(client, pid, "plans", plan)
    save_complete(client, pid)
    assert state(client, pid)["handoff_ready"]
    assert any("尚未冻结" in n for n in state(client, pid)["notices"])


def test_environment_expiry_blocks_approval(prepared):
    client, pid, _, app, _, _, env, tech = prepared
    with app.state.sessions.begin() as session:
        session.get(PlanningAsset, env["id"]).created_at = datetime.now(UTC) - timedelta(minutes=16)
    assert state(client, pid)["technical_issues"][0]["code"] == "ENVIRONMENT_EXPIRED"
    assert confirm(client, pid, "technical", tech).status_code == 409


def test_changed_dependencies_and_permission_recheck(prepared):
    client, pid, settings, _, _, _, _, tech = prepared
    file = settings.allowed_roots[0] / "demo/package.json"
    file.write_text('{"dependencies":{"next":"16.0.0"}}')
    assert any(i["code"] == "ENVIRONMENT_CHANGED" for i in state(client, pid)["technical_issues"])
    assert confirm(client, pid, "technical", tech).status_code == 409


@pytest.mark.parametrize("bad", ["json", "size", "secret", "symlink", "parent_symlink", "fifo"])
def test_dependency_reader_refuses_bad_files_without_echo_or_scripts(setup, monkeypatch, bad):
    client, pid, settings, _, provider = setup
    monkeypatch.setattr(planning, "trusted_version", lambda _: "1.2.3")
    root = settings.allowed_roots[0] / "demo"
    target = root / "package.json"
    if bad == "json":
        target.write_text("{")
    elif bad == "size":
        target.write_text(" " * 65537)
    elif bad == "secret":
        target.write_text('{"password":"do-not-echo-private-data"}')
    elif bad == "symlink":
        secret = settings.data_dir / "secret"
        secret.write_text("never-show-target")
        target.symlink_to(secret)
    elif bad == "parent_symlink":
        (root / "frontend").symlink_to(settings.data_dir, target_is_directory=True)
        (settings.data_dir / "package.json").write_text('{"password":"do-not-echo-private-data"}')
    else:
        import os

        os.mkfifo(target)
    response = post(client, route(pid, "/environment"), {})
    assert response.status_code == 200, response.json()
    assert any(
        c["status"] == "blocked" and c["key"] == "dependencies" for c in response.json()["payload"]["checks"]
    )
    assert "do-not-echo-private-data" not in response.text and "never-show-target" not in response.text
    assert provider.calls == 0


def test_existing_stack_recognition_only_reads_declarations(setup, monkeypatch):
    client, pid, settings, _, _ = setup
    monkeypatch.setattr(planning, "trusted_version", lambda _: "1.2.3")
    root = settings.allowed_roots[0] / "demo"
    (root / "package.json").write_text(
        json.dumps({"dependencies": {"vue": "3"}, "scripts": {"postinstall": "touch compromised"}})
    )
    (root / "requirements.txt").write_text("django==5.0")
    raw_before = (root / "package.json").read_bytes()
    env = post(client, route(pid, "/environment"), {}).json()
    assert "Vue" in env["payload"]["frameworks"] and "Django" in env["payload"]["frameworks"]
    assert not (root / "compromised").exists() and (root / "package.json").read_bytes() == raw_before
    assert "postinstall" not in json.dumps(env)


def test_new_technical_revision_invalidates_bound_plan(prepared):
    client, pid, _, _, _, base, env, tech = prepared
    plan = generate_plan(prepared)
    confirm(client, pid, "plans", plan)
    post(
        client,
        route(pid, "/technical/revisions"),
        {
            "expected_revision": 1,
            "baseline_id": base["id"],
            "environment_id": env["id"],
            "text": tech["payload"]["text"],
        },
    )
    assert not state(client, pid)["handoff_ready"]
    assert state(client, pid)["plan_issues"][0]["code"] == "TECHNICAL_REQUIRED"


@pytest.mark.parametrize(
    "change,code",
    [
        ("version_cycle", "DEPENDENCY_CYCLE"),
        ("stage_cycle", "DEPENDENCY_CYCLE"),
        ("task_cycle", "DEPENDENCY_CYCLE"),
        ("parent_cycle", "DEPENDENCY_CYCLE"),
        ("missing_ref", "DEPENDENCY_MISSING"),
        ("duplicate", "DUPLICATE_ID"),
        ("coverage", "REQUIREMENT_UNCOVERED"),
        ("new_requirement", "REQUIREMENT_UNKNOWN"),
        ("priority", "PRIORITY_WEAKENED"),
        ("version", "VERSION_MISMATCH"),
        ("stage", "STAGE_MISSING"),
        ("stage_dep", "STAGE_DEPENDENCY"),
        ("reason", "REASON_REQUIRED"),
        ("defect", "DEFECT_REQUIRED"),
        ("no_requirement", "REQUIREMENT_REQUIRED"),
        ("empty", "PLAN_EMPTY"),
    ],
)
def test_invalid_plans_save_but_cannot_confirm(prepared, change, code):
    client, pid, _, _, _, _, _, _ = prepared
    plan = generate_plan(prepared)
    text = copy.deepcopy(plan["payload"]["text"])
    if change == "version_cycle":
        text["versions"][0]["depends_on"] = ["V1"]
    if change == "stage_cycle":
        text["stages"][0]["depends_on"] = ["S2"]
    if change == "task_cycle":
        text["tasks"][0]["depends_on"] = ["T2"]
    if change == "parent_cycle":
        text["stages"][0]["parent_id"] = "S1"
    if change == "missing_ref":
        text["tasks"][0]["depends_on"] = ["T99"]
    if change == "duplicate":
        text["tasks"].append(copy.deepcopy(text["tasks"][0]))
    if change == "coverage":
        for task in text["tasks"]:
            task.update(kind="infrastructure", requirement_ids=[], reason="必要测试基础设施")
    if change == "new_requirement":
        text["tasks"][0]["requirement_ids"] = ["R99"]
    if change == "priority":
        text["tasks"][0]["priority"] = "P2"
    if change == "version":
        text["versions"][0]["name"] = "未经基线确认的第二版"
    if change == "stage":
        text["tasks"][0]["stage_id"] = "S99"
    if change == "stage_dep":
        text["stages"][1]["depends_on"] = []
    if change == "reason":
        text["tasks"][1]["reason"] = ""
    if change == "defect":
        text["tasks"][0].update(kind="defect", defect_ref="")
    if change == "no_requirement":
        text["tasks"][0]["requirement_ids"] = []
    if change == "empty":
        text = {"versions": [], "stages": [], "tasks": []}
    bad = revise(client, pid, plan, text)
    assert code in {i["code"] for i in state(client, pid)["plan_issues"]}
    assert confirm(client, pid, "plans", bad).json()["error"]["code"] == "PLAN_BLOCKED"
    assert not state(client, pid)["handoff_ready"]


def test_original_acceptance_and_gates_cannot_be_removed(prepared):
    client, pid, _, _, _, base, _, _ = prepared
    plan = generate_plan(prepared)
    body = {
        "expected_revision": 1,
        "technical_id": plan["payload"]["technical_id"],
        "text": {**plan["payload"]["text"], "required_checks": [], "acceptance": {}},
    }
    assert post(client, route(pid, "/plans/revisions"), body).status_code == 422
    text = copy.deepcopy(plan["payload"]["text"])
    text["tasks"].append({**text["tasks"][0], "id": "T3", "title": "拆分后的需求实现"})
    saved = revise(client, pid, plan, text)
    assert saved["payload"]["acceptance"] == {"R1": base["items"][0]["acceptance"]}
    assert saved["payload"]["required_checks"] == planning.CHECKS


def test_cross_project_and_malformed_confirm_are_rejected(prepared):
    client, pid, _, _, _, _, env, tech = prepared
    assert client.get(PREFIX + route("other", "/assets/" + tech["id"])).status_code == 404
    assert (
        post(
            client,
            route(pid, "/technical/confirm"),
            {"expected_revision": 1, "expected_hash": "0" * 64, "reason": "明确确认"},
        ).status_code
        == 409
    )
    body = {"expected_revision": 1, "expected_hash": tech["content_hash"], "reason": "   "}
    assert post(client, route(pid, "/technical/confirm"), body).status_code == 422
    assert (
        post(
            client,
            route(pid, "/technical/generate"),
            {"expected_revision": 1, "baseline_id": env["id"], "environment_id": env["id"]},
        ).status_code
        == 404
    )


def test_version_recipe_fixed_args_timeout_and_no_raw_output(monkeypatch):
    calls = []
    monkeypatch.setattr(Path, "is_file", lambda _: True)
    monkeypatch.setattr(planning.os, "access", lambda *args: True)

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args, 0, stdout=b"v22.23.2\nsensitive-extra-output", stderr=b"secret"
        )

    monkeypatch.setattr(planning.subprocess, "run", run)
    assert planning.trusted_version("node") == "22.23.2"
    args, kw = calls[0]
    assert (
        args == ["/opt/homebrew/bin/node", "--version"] and kw["cwd"] == planning.ROOT and kw["timeout"] == 2
    )
    assert "NODE_OPTIONS" not in kw["env"]
    monkeypatch.setattr(
        planning.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("node", 2)),
    )
    assert planning.trusted_version("node") == "无法核对"


def test_arbitrary_project_node_on_path_is_not_executed(monkeypatch, tmp_path):
    project_binary = tmp_path / "project/node"
    project_binary.parent.mkdir()
    project_binary.write_text("malicious repository binary")
    monkeypatch.setattr(planning.shutil, "which", lambda _: str(project_binary))
    monkeypatch.setattr(Path, "is_file", lambda _: False)
    monkeypatch.setattr(planning.subprocess, "run", lambda *a, **k: pytest.fail("不执行任意项目二进制"))
    assert planning.trusted_version("node") == "未检测到"


def test_maximum_baseline_fields_generate_valid_bounded_summary(prepared):
    client, pid, _, _, _, base, env, _ = prepared
    # Pure summary contract with full-length immutable baseline, not a model result.
    large = copy.deepcopy(base)
    for field in large["fields"]:
        field["content"] = "需求" * 1500
    with prepared[3].state.sessions() as session:
        env_row = session.get(PlanningAsset, env["id"])
        text = planning.initial_technical(large, env_row)
    assert len(text["understanding"]) <= 3000 and len(text["costs"]) <= 2000
    assert "摘要已截断" in text["understanding"] and "摘要已截断" in text["data_flow"]
    assert large["fields"][0]["content"] == "需求" * 1500


def test_latest_revision_not_replaced_by_later_timestamp(prepared):
    client, pid, _, app, _, base, env, tech = prepared
    newer = post(
        client,
        route(pid, "/technical/revisions"),
        {
            "expected_revision": 1,
            "baseline_id": base["id"],
            "environment_id": env["id"],
            "text": {**tech["payload"]["text"], "risks": "真正的新修订，应按修订号确认"},
        },
    ).json()
    with app.state.sessions.begin() as session:
        session.get(PlanningAsset, tech["id"]).created_at = datetime.now(UTC) + timedelta(days=1)
    assert state(client, pid)["technical"]["id"] == newer["id"]
    assert confirm(client, pid, "technical", newer).status_code == 200


def test_all_baseline_items_with_distinct_versions_fit_editing_limit():
    # Worst supported default template: 100 unique versions, 200 stages and 200 tasks.
    from backend.app.planning_schemas import PlanText

    base = {
        "items": [
            {"id": f"R{i}", "value": "需求价值" * 150, "priority": "P0", "target_version": f"版本{i}"}
            for i in range(1, 101)
        ]
    }
    result = planning.initial_plan(base)
    PlanText.model_validate(result)
    assert len(result["tasks"]) == 200
    assert len(json.dumps(result, ensure_ascii=False).encode()) < 1_048_000


def test_oversized_reference_rejected_before_graph_work(prepared):
    client, pid, *_ = prepared
    plan = generate_plan(prepared)
    text = copy.deepcopy(plan["payload"]["text"])
    text["tasks"][0]["depends_on"] = ["T" + "1" * 1000]
    assert (
        post(
            client,
            route(pid, "/plans/revisions"),
            {
                "expected_revision": 1,
                "technical_id": plan["payload"]["technical_id"],
                "text": text,
            },
        ).status_code
        == 422
    )
