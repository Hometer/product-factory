import concurrent.futures
import copy
import json
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app import models
from backend.app.core import ROOT, Settings
from backend.app.db import RequirementRecord, connect
from backend.app.main import PREFIX, create_app

REAL_START_WORKER = models.start_worker


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(
        models, "start_worker", lambda *args: (threading.Event(), SimpleNamespace(join=lambda **kw: None))
    )
    root = tmp_path / "projects"
    root.mkdir()
    settings = Settings(
        tmp_path / "state",
        (root,),
        "test-only-local-token",
        hosts=("testserver",),
        model_env=tmp_path / ".env",
    )
    provider = FakeProvider()
    app = create_app(settings, provider)
    headers = {"Authorization": f"Bearer {settings.token}", "Origin": "http://127.0.0.1:3000"}
    with TestClient(app, headers=headers) as client:
        preview = client.post(
            f"{PREFIX}/projects/create-preview",
            json={
                "name": "需求测试",
                "target_user": "产品负责人",
                "core_task": "整理需求",
                "path": str(root / "demo"),
            },
        ).json()
        project = post(
            client,
            "/projects/confirm",
            {"preview_id": preview["preview_id"], "expected_hash": preview["content_hash"]},
        ).json()
        yield client, project["id"], settings, app, provider


def post(client, route, body=None, key=None):
    return client.post(PREFIX + route, json=body, headers={"Idempotency-Key": key or str(uuid4())})


def req(pid, suffix=""):
    return f"/projects/{pid}/requirements{suffix}"


def ai(pid, suffix):
    return f"/projects/{pid}/analysis/{suffix}"


def current(client, pid):
    response = client.get(PREFIX + req(pid))
    assert response.status_code == 200, response.json()
    return response.json()["draft"]


def complete_body(client, pid):
    draft = current(client, pid)
    fields = copy.deepcopy(draft["fields"])
    for f in fields:
        f.update(
            status="known", content=f"已确认的{f['key']}业务要求，用户负责判断", confirmed_by="local_operator"
        )
    fields[3].update(status="unknown", content="待真实样例评分", confirmed_by="")
    fields[-1].update(status="not_applicable", content="仅本机交付，无云上线账号要求")
    return {
        "expected_revision": draft["revision"],
        "fields": fields,
        "items": [
            {
                "id": "R1",
                "value": "需求能跨会话保存",
                "trigger": "用户填写需求后保存",
                "rule": "保存为新修订不覆盖旧基线",
                "exception": "版本冲突时保留输入",
                "acceptance": "重启后需求与基线可读取",
                "priority": "P0",
                "target_version": "首版",
                "source_ref": "user:R1",
            }
        ],
    }


def save_complete(client, pid):
    response = post(client, req(pid, "/revisions"), complete_body(client, pid))
    assert response.status_code == 200, response.json()
    return response.json()


class FakeProvider:
    source = "mock"
    calls = 0
    errors = []
    receipts = []

    def __init__(self):
        self.calls = 0
        self.errors = []
        self.receipts = []

    def send(self, key, base, selected, correction=False):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        if self.receipts:
            return self.receipts.pop(0)
        candidate = {
            "summary": "测试候选，不能充当真实效果",
            "suggestions": [
                {
                    "field": selected[0]["key"],
                    "content": "测试环境的候选需求内容",
                    "rationale": "依据显式选择的输入",
                    "source_fields": [selected[0]["key"]],
                }
            ],
            "questions": [],
        }
        return receipt(candidate)


def receipt(candidate):
    return {
        "usage": {"prompt_tokens": 100, "completion_tokens": 100},
        "choices": [{"message": {"content": json.dumps(candidate, ensure_ascii=False)}}],
    }


def prepare_run(client, pid):
    response = post(
        client, "/model/config", {"api_key": "test-only-fake-provider-key", "base_url": models.DEFAULT_BASE}
    )
    assert response.status_code == 200, response.json()
    draft = save_complete(client, pid)
    preview = post(
        client, ai(pid, "preview"), {"expected_revision": draft["revision"], "selected_fields": ["user_task"]}
    )
    assert preview.status_code == 200, preview.json()
    preview = preview.json()
    permission = post(
        client,
        ai(pid, "authorize"),
        {
            "preview_id": preview["id"],
            "expected_hash": preview["content_hash"],
            "nonsensitive_confirmed": True,
        },
    ).json()
    body = {
        "expected_revision": draft["revision"],
        "preview_id": preview["id"],
        "authorization_id": permission["id"],
    }
    return preview, permission, body


def test_initial_unknowns_and_no_directory_write(setup):
    client, pid, settings, _, _ = setup
    before = {p: p.read_bytes() for p in settings.allowed_roots[0].rglob("*") if p.is_file()}
    draft = current(client, pid)
    assert draft["revision"] == 0 and len(draft["fields"]) == 8
    assert draft["fields"][0]["source"] == "project"
    assert len(draft["gaps"]) == 8
    save_complete(client, pid)
    assert before == {p: p.read_bytes() for p in settings.allowed_roots[0].rglob("*") if p.is_file()}


@pytest.mark.parametrize("problem", ["duplicate_field", "extra", "blank_known", "duplicate_item"])
def test_strict_draft_validation(setup, problem):
    client, pid, *_ = setup
    body = complete_body(client, pid)
    if problem == "duplicate_field":
        body["fields"][1] = body["fields"][0]
    if problem == "extra":
        body["secret_note"] = "must-not-echo"
    if problem == "blank_known":
        body["fields"][1]["content"] = " "
    if problem == "duplicate_item":
        body["items"].append(body["items"][0])
    response = post(client, req(pid, "/revisions"), body)
    assert response.status_code == 422
    assert "must-not-echo" not in response.text


def test_revision_conflict_and_idempotency(setup):
    client, pid, *_ = setup
    body = complete_body(client, pid)
    first = post(client, req(pid, "/revisions"), body, "save-request-1")
    assert post(client, req(pid, "/revisions"), body, "save-request-1").json() == first.json()
    assert post(client, req(pid, "/revisions"), body).status_code == 409
    body["fields"][0]["content"] = "新的结论不能复用旧标识"
    assert post(client, req(pid, "/revisions"), body, "save-request-1").status_code == 409
    assert current(client, pid)["revision"] == 1
    assert client.get(PREFIX + req(pid, "/revisions/1")).json() == first.json()


def test_question_versions_and_uncertain_answer(setup):
    client, pid, *_ = setup
    round = post(client, req(pid, "/check"), {"expected_revision": 0}).json()
    assert 1 <= len(round["questions"]) <= 5
    q = round["questions"][0]
    assert all(q[k] for k in ("recommendation", "example", "impact"))
    body = {
        "expected_revision": 0,
        "round_id": round["id"],
        "question_version": round["version"],
        "answers": [{"question_id": q["id"], "mode": "uncertain"}],
    }
    answer = post(client, req(pid, "/answers"), body).json()
    assert answer["revision"] == 1
    assert next(f for f in answer["fields"] if f["key"] == q["id"])["status"] == "unknown"
    body["expected_revision"] = 1
    assert post(client, req(pid, "/answers"), body).json()["error"]["code"] == "QUESTION_STALE"


def test_uncertain_text_in_custom_answer_stays_unknown(setup):
    client, pid, *_ = setup
    round = post(client, req(pid, "/check"), {"expected_revision": 0}).json()
    question = round["questions"][0]
    response = post(
        client,
        req(pid, "/answers"),
        {
            "expected_revision": 0,
            "round_id": round["id"],
            "question_version": round["version"],
            "answers": [{"question_id": question["id"], "mode": "custom", "content": "不确定"}],
        },
    )
    field = next(f for f in response.json()["fields"] if f["key"] == question["id"])
    assert field["status"] == "unknown" and field["confirmed_by"] == ""


def test_recommendation_applies_only_shown_question(setup):
    client, pid, *_ = setup
    body = complete_body(client, pid)
    body["fields"][-1].update(status="unknown", content="")
    draft = post(client, req(pid, "/revisions"), body).json()
    round = post(client, req(pid, "/check"), {"expected_revision": draft["revision"]}).json()
    launch = next(q for q in round["questions"] if q["id"] == "launch")
    answer = post(
        client,
        req(pid, "/answers"),
        {
            "expected_revision": draft["revision"],
            "round_id": round["id"],
            "question_version": round["version"],
            "answers": [{"question_id": "launch", "mode": "recommended"}],
        },
    ).json()
    field = next(f for f in answer["fields"] if f["key"] == "launch")
    assert field["content"] == launch["recommendation"] and field["status"] == "not_applicable"
    assert next(f for f in answer["fields"] if f["key"] == "quality")["status"] == "unknown"


def test_manual_update_supersedes_pending_decision_preserving_history(setup):
    client, pid, _, app, _ = setup
    round = post(client, req(pid, "/check"), {"expected_revision": 0}).json()
    question = round["questions"][0]
    post(
        client,
        req(pid, "/answers"),
        {
            "expected_revision": 0,
            "round_id": round["id"],
            "question_version": round["version"],
            "answers": [{"question_id": question["id"], "mode": "uncertain"}],
        },
    )
    save_complete(client, pid)
    state = client.get(PREFIX + req(pid)).json()
    active = next(d for d in state["decisions"] if d["question"] == question["question"])
    assert active["status"] == "confirmed" and len(state["decisions"]) <= 10
    with app.state.sessions() as session:
        rows = session.scalars(
            select(RequirementRecord).where(
                RequirementRecord.project_id == pid, RequirementRecord.kind == "decision"
            )
        ).all()
        assert any(
            r.payload["question"] == question["question"] and r.payload["status"] == "pending" for r in rows
        )


def test_whitespace_item_cannot_freeze(setup):
    client, pid, *_ = setup
    body = complete_body(client, pid)
    body["items"][0]["acceptance"] = "   "
    assert post(client, req(pid, "/revisions"), body).status_code == 422


def test_blockers_and_immutable_baseline(setup):
    client, pid, *_ = setup
    zero = current(client, pid)
    response = post(
        client,
        req(pid, "/freeze"),
        {"expected_revision": 0, "expected_hash": zero["content_hash"], "reason": "明确确认需求"},
    )
    assert response.json()["error"]["code"] == "BASELINE_BLOCKED"
    draft = save_complete(client, pid)
    body = {
        "expected_revision": draft["revision"],
        "expected_hash": draft["content_hash"],
        "reason": "人工核对冻结",
    }
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: post(client, req(pid, "/freeze"), body).json(), range(4)))
    assert len({r["id"] for r in results}) == 1
    original = results[0]
    assert original["deferred"][0]["field"] == "quality"
    save_complete(client, pid)
    assert client.get(PREFIX + req(pid, f"/baselines/{original['id']}")).json() == original


def test_not_applicable_core_cannot_bypass_gate(setup):
    client, pid, *_ = setup
    body = complete_body(client, pid)
    body["fields"][0].update(status="not_applicable", content="这里不想填写目标用户")
    draft = post(client, req(pid, "/revisions"), body).json()
    assert any(g["id"] == "gap:na:user_task" for g in draft["gaps"])


def test_records_do_not_leak_cross_project(setup):
    client, pid, *_ = setup
    draft = save_complete(client, pid)
    baseline = post(
        client,
        req(pid, "/freeze"),
        {
            "expected_revision": draft["revision"],
            "expected_hash": draft["content_hash"],
            "reason": "确认需求内容",
        },
    ).json()
    assert client.get(PREFIX + req(str(uuid4()), f"/baselines/{baseline['id']}")).status_code == 404


def test_persisted_requirements_restart(setup):
    client, pid, settings, _, provider = setup
    draft = save_complete(client, pid)
    frozen = post(
        client,
        req(pid, "/freeze"),
        {
            "expected_revision": draft["revision"],
            "expected_hash": draft["content_hash"],
            "reason": "确认冻结当前需求",
        },
    ).json()
    with TestClient(create_app(settings, provider), headers=dict(client.headers)) as reopened:
        state = reopened.get(PREFIX + req(pid)).json()
        assert state["draft"] == draft and state["baselines"][0] == frozen


def test_config_does_not_echo_key_or_call(setup):
    client, _, settings, _, provider = setup
    settings.model_env.write_text("OTHER_SETTING=preserved\n")
    result = post(
        client, "/model/config", {"api_key": "test-only-fake-provider-key", "base_url": models.DEFAULT_BASE}
    )
    assert result.status_code == 200 and "test-only-fake-provider-key" not in result.text
    assert "OTHER_SETTING=preserved" in settings.model_env.read_text()
    assert settings.model_env.stat().st_mode & 0o077 == 0 and provider.calls == 0
    assert (
        post(
            client,
            "/model/config",
            {"api_key": "test-only-fake-provider-key", "base_url": "https://external.invalid"},
        ).status_code
        == 422
    )


def test_model_secret_symlink_is_rejected(setup):
    client, _, settings, _, provider = setup
    outside = settings.model_env.parent / "outside.txt"
    outside.write_text("must-be-preserved")
    settings.model_env.symlink_to(outside)
    response = post(
        client, "/model/config", {"api_key": "test-only-fake-provider-key", "base_url": models.DEFAULT_BASE}
    )
    assert response.status_code == 409 and outside.read_text() == "must-be-preserved"
    assert provider.calls == 0 and "test-only-fake-provider-key" not in response.text


def test_http_provider_contract_uses_no_tools_and_no_thinking(monkeypatch):
    original = models.httpx.Client
    observed = []

    def handle(request):
        observed.append(json.loads(request.content))
        return models.httpx.Response(
            200, json=receipt({"summary": "测试输入的候选摘要", "suggestions": [], "questions": []})
        )

    monkeypatch.setattr(
        models.httpx, "Client", lambda **kw: original(**kw, transport=models.httpx.MockTransport(handle))
    )
    response = models.Provider().send(
        "test-only-fake-provider-key",
        models.DEFAULT_BASE,
        [{"key": "user_task", "content": "非敏感合成资料", "status": "known"}],
    )
    assert response["usage"]["prompt_tokens"] == 100
    assert observed[0]["enable_thinking"] is False and observed[0]["response_format"] == {
        "type": "json_object"
    }
    assert "tools" not in observed[0] and observed[0]["max_tokens"] == models.MAX_OUTPUT


def test_no_key_model_is_disabled(setup):
    client, pid, *_ = setup
    assert not client.get(PREFIX + "/model/status").json()["configured"]
    assert (
        post(client, ai(pid, "preview"), {"expected_revision": 0, "selected_fields": ["user_task"]}).json()[
            "error"
        ]["code"]
        == "MODEL_NOT_CONFIGURED"
    )


@pytest.mark.parametrize("content", ["password=do-not-send-this", "13812345678", "person@example.invalid"])
def test_sensitive_selected_input_blocked(setup, content):
    client, pid, *_ = setup
    preview, _, _ = prepare_run(client, pid)
    body = complete_body(client, pid)
    body["fields"][0]["content"] = content
    draft = post(client, req(pid, "/revisions"), body).json()
    response = post(
        client, ai(pid, "preview"), {"expected_revision": draft["revision"], "selected_fields": ["user_task"]}
    )
    assert response.json()["error"]["code"] == "SENSITIVE_INPUT" and content not in response.text
    assert [f["key"] for f in preview["selected_fields"]] == ["user_task"]


def test_authorization_hash_false_consent_and_changed_revision(setup):
    client, pid, *_ = setup
    preview, _, body = prepare_run(client, pid)
    assert (
        post(
            client,
            ai(pid, "authorize"),
            {
                "preview_id": preview["id"],
                "expected_hash": preview["content_hash"],
                "nonsensitive_confirmed": False,
            },
        ).status_code
        == 422
    )
    save_complete(client, pid)
    assert post(client, ai(pid, "runs"), body).json()["error"]["code"] == "REVISION_CONFLICT"


def test_numeric_consent_does_not_authorize(setup):
    client, pid, *_ = setup
    preview, _, _ = prepare_run(client, pid)
    assert (
        post(
            client,
            ai(pid, "authorize"),
            {
                "preview_id": preview["id"],
                "expected_hash": preview["content_hash"],
                "nonsensitive_confirmed": 1,
            },
        ).status_code
        == 422
    )


def test_budget_reserved_replay_and_settlement(setup):
    client, pid, settings, app, provider = setup
    _, _, body = prepare_run(client, pid)
    response = post(client, ai(pid, "runs"), body, "run-idempotent-1")
    assert response.status_code == 202
    run = response.json()
    assert post(client, ai(pid, "runs"), body, "run-idempotent-1").json() == run
    assert client.get(PREFIX + "/model/status").json()["requests_reserved"] == 2
    models.process_run(app.state.sessions, settings, provider, run["id"])
    result = client.get(PREFIX + ai(pid, f"runs/{run['id']}")).json()
    assert result["state"] == "succeeded" and result["source"] == "mock"
    status = client.get(PREFIX + "/model/status").json()
    assert status["requests_used"] == 1 and status["reserved_micro"] == 0 and status["spent_micro"] == 280
    adopted = post(
        client,
        ai(pid, f"runs/{run['id']}/adopt"),
        {"expected_revision": result["revision"], "fields": ["user_task"]},
    ).json()
    assert adopted["revision"] == result["revision"] + 1
    assert adopted["fields"][0]["source"] == "ai_suggestion"


@pytest.mark.parametrize("limit", ["requests", "money"])
def test_stage_budget_limits(setup, limit):
    client, pid, _, app, provider = setup
    _, _, body = prepare_run(client, pid)
    with models.locked(app.state.sessions) as session:
        account = models.budget(session)
        if limit == "requests":
            account.requests_used = 99
        else:
            account.spent_micro = 100_000_000
    assert post(client, ai(pid, "runs"), body).json()["error"]["code"] == "BUDGET_EXCEEDED"
    assert provider.calls == 0


def test_concurrent_single_run_and_revocation(setup):
    client, pid, settings, app, provider = setup
    _, permission, body = prepare_run(client, pid)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: post(client, ai(pid, "runs"), body), range(4)))
    assert sum(r.status_code == 202 for r in responses) == 1
    run = next(r.json() for r in responses if r.status_code == 202)
    assert post(client, ai(pid, f"authorizations/{permission['id']}/revoke"), {}).json()["revoked"]
    models.process_run(app.state.sessions, settings, provider, run["id"])
    assert provider.calls == 0
    assert client.get(PREFIX + ai(pid, f"runs/{run['id']}")).json()["state"] == "failed"


def test_structure_correction_once(setup):
    client, pid, settings, app, provider = setup
    _, _, body = prepare_run(client, pid)
    provider.receipts = [receipt({"extra": "invalid"}), receipt({"extra": "still-invalid"})]
    run = post(client, ai(pid, "runs"), body).json()
    models.process_run(app.state.sessions, settings, provider, run["id"])
    result = client.get(PREFIX + ai(pid, f"runs/{run['id']}")).json()
    assert result["state"] == "failed" and len(result["attempts"]) == 2 and provider.calls == 2
    assert result["settled_micro"] == 560


@pytest.mark.parametrize("sent", [False, True])
def test_initialization_or_unknown_result(setup, sent):
    client, pid, settings, app, provider = setup
    _, _, body = prepare_run(client, pid)
    provider.errors = [models.ProviderError("TEST_FAILURE", sent)]
    run = post(client, ai(pid, "runs"), body).json()
    models.process_run(app.state.sessions, settings, provider, run["id"])
    result = client.get(PREFIX + ai(pid, f"runs/{run['id']}")).json()
    assert provider.calls == 1 and result["state"] == ("result_unknown" if sent else "failed")
    status = client.get(PREFIX + "/model/status").json()
    assert status["requests_used"] == 0
    assert (status["reserved_micro"] > 0) == sent


def test_missing_usage_preserves_budget(setup):
    client, pid, settings, app, provider = setup
    _, _, body = prepare_run(client, pid)
    provider.receipts = [{"usage": {}, "choices": []}]
    run = post(client, ai(pid, "runs"), body).json()
    models.process_run(app.state.sessions, settings, provider, run["id"])
    assert client.get(PREFIX + ai(pid, f"runs/{run['id']}")).json()["state"] == "result_unknown"
    assert client.get(PREFIX + "/model/status").json()["reserved_micro"] == run["reserved_micro"]


def test_restart_does_not_repeat_unknown_external_call(setup):
    client, pid, settings, app, provider = setup
    _, _, body = prepare_run(client, pid)
    run = post(client, ai(pid, "runs"), body).json()
    with models.locked(app.state.sessions) as session:
        row = session.get(RequirementRecord, run["id"])
        row.payload = {
            **row.payload,
            "state": "running",
            "attempts": [{"attempt": 1, "state": "dispatching"}],
        }
    stop, thread = REAL_START_WORKER(app.state.sessions, settings, provider)
    stop.set()
    thread.join(timeout=3)
    recovered = client.get(PREFIX + ai(pid, f"runs/{run['id']}")).json()
    assert recovered["state"] == "result_unknown" and provider.calls == 0
    assert client.get(PREFIX + "/model/status").json()["reserved_micro"] == run["reserved_micro"]
    with TestClient(create_app(settings, provider), headers=dict(client.headers)) as reopened:
        assert reopened.get(PREFIX + ai(pid, "runs")).json()["items"][0]["state"] == "result_unknown"
        assert reopened.get(PREFIX + "/model/status").json()["requests_reserved"] == 2


def test_two_projects_compete_for_last_stage_request_allowance(setup):
    client, pid, settings, app, provider = setup
    _, _, body1 = prepare_run(client, pid)
    preview = client.post(
        PREFIX + "/projects/create-preview",
        json={
            "name": "第二项目",
            "target_user": "用户",
            "core_task": "核对共享预算",
            "path": str(settings.allowed_roots[0] / "second"),
        },
    ).json()
    pid2 = post(
        client,
        "/projects/confirm",
        {"preview_id": preview["preview_id"], "expected_hash": preview["content_hash"]},
    ).json()["id"]
    _, _, body2 = prepare_run(client, pid2)
    with models.locked(app.state.sessions) as session:
        models.budget(session).requests_used = 98
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(lambda pair: post(client, ai(pair[0], "runs"), pair[1]), [(pid, body1), (pid2, body2)])
        )
    assert sorted(r.status_code for r in responses) == [202, 409]
    assert next(r for r in responses if r.status_code == 409).json()["error"]["code"] == "BUDGET_EXCEEDED"
    assert provider.calls == 0
    status = client.get(PREFIX + "/model/status").json()
    assert status["requests_used"] + status["requests_reserved"] == 100


def test_actual_provider_initialization_guard(monkeypatch):
    monkeypatch.setattr(
        models.httpx, "Client", lambda **kw: (_ for _ in ()).throw(RuntimeError("secret-must-not-echo"))
    )
    with pytest.raises(models.ProviderError) as exc:
        models.Provider().send("test-only-placeholder", models.DEFAULT_BASE, [])
    assert not exc.value.sent and "secret-must-not-echo" not in str(exc.value)


def test_quote_expiry_rejects(setup, monkeypatch):
    client, pid, _, _, provider = setup
    _, _, body = prepare_run(client, pid)
    monkeypatch.setattr(models, "quote_valid", lambda: False)
    assert post(client, ai(pid, "runs"), body).json()["error"]["code"] == "QUOTE_UNAVAILABLE"
    assert provider.calls == 0


def test_migration_from_a1_preserves_projects(tmp_path):
    settings = Settings(
        tmp_path / "data",
        (tmp_path,),
        "test-only-placeholder",
        hosts=("testserver",),
        model_env=tmp_path / ".env",
    )
    engine, _ = connect(settings.data_dir)
    migration = Config(str(ROOT / "backend/alembic.ini"))
    migration.set_main_option("script_location", str(ROOT / "backend/alembic"))
    with engine.begin() as connection:
        migration.attributes["connection"] = connection
        command.upgrade(migration, "0001")
        connection.exec_driver_sql(
            "INSERT INTO projects VALUES (?,?,?,?,?)",
            (
                "old-project",
                str(tmp_path),
                json.dumps(
                    {
                        "name": "旧项目",
                        "description": "",
                        "target_user": "用户",
                        "core_task": "任务",
                        "template_version": "starter-v1",
                        "delivery_target": "local",
                    }
                ),
                "2026-10-08 00:00:00",
                1,
            ),
        )
    engine.dispose()
    with TestClient(create_app(settings), headers={"Authorization": f"Bearer {settings.token}"}) as client:
        assert client.get(PREFIX + "/projects/old-project").json()["name"] == "旧项目"
        assert client.get(PREFIX + "/health").json()["schema_version"] == "0005"
