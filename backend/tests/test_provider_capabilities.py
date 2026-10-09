import copy
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app import models
from backend.app import provider_capabilities as caps
from backend.app.db import RequirementRecord
from backend.app.main import PREFIX, create_app
from backend.tests import test_requirements as rh

setup = rh.setup


def view(ex):
    r = ex[0].get(PREFIX + "/model/provider")
    assert r.status_code == 200, r.json()
    return r.json()


def execute(ex, receipts=None):
    c, pid, settings, app, provider = ex
    _, _, body = rh.prepare_run(c, pid)
    if receipts is not None:
        provider.receipts = receipts
    run = rh.post(c, rh.ai(pid, "runs"), body).json()
    models.process_run(app.state.sessions, settings, provider, run["id"])
    return c.get(PREFIX + rh.ai(pid, "runs/" + run["id"])).json()


def good():
    r = rh.receipt({"summary": "仅用于离线协议测试的合成候选", "suggestions": [], "questions": []})
    r["id"] = "chatcmpl-contract-test"
    return r


def test_no_key_six_capabilities_no_mutation_or_network(setup, monkeypatch):
    monkeypatch.setattr(models.httpx, "Client", lambda *a, **k: pytest.fail("GET must not call"))
    ex = setup
    with ex[3].state.sessions() as s:
        before = [(r.id, copy.deepcopy(r.payload)) for r in s.scalars(select(RequirementRecord))]
    v = view(ex)
    assert not v["configured"] and v["key_mask"] == "未配置"
    assert not v["automatic_switch"] and v["max_attempts"] == 2
    assert len(v["capabilities"]) == 6 and not any(c["can_run"] for c in v["capabilities"])
    assert all(c["real_verified_count"] == 0 for c in v["capabilities"])
    assert {c["id"] for c in v["capabilities"] if c["implemented"]} == {"text", "structured", "usage"}
    assert v["receipt_total"] == 0
    assert view(ex) == v
    with ex[3].state.sessions() as s:
        assert [(r.id, r.payload) for r in s.scalars(select(RequirementRecord))] == before


def test_config_only_fixed_mask_supported_and_no_calls(setup):
    ex = setup
    response = rh.post(
        ex[0], "/model/config", {"api_key": "test-only-fake-provider-key", "base_url": models.DEFAULT_BASE}
    )
    assert response.status_code == 200
    v = view(ex)
    assert v["configured"] and v["key_mask"] == "••••••••"
    assert "test-only-fake-provider-key" not in json.dumps(v)
    assert {c["id"] for c in v["capabilities"] if c["can_run"]} == {"text", "structured", "usage"}
    assert ex[4].calls == 0 and all(c["real_verified_count"] == 0 for c in v["capabilities"])


@pytest.mark.parametrize("changed", ["quote", "model"])
def test_contract_or_quote_changed_blocks_runtime(setup, monkeypatch, changed):
    rh.post(
        setup[0], "/model/config", {"api_key": "test-only-fake-provider-key", "base_url": models.DEFAULT_BASE}
    )
    if changed == "quote":
        monkeypatch.setattr(models, "quote_valid", lambda: False)
    else:
        monkeypatch.setattr(models, "MODEL", "unreviewed-model")
    assert not models.available(setup[2])
    v = view(setup)
    assert v["blockers"] and not any(c["can_run"] for c in v["capabilities"])


def test_receipt_persists_with_attempt_and_mock_never_verifies_real(setup):
    run = execute(setup, [good()])
    assert run["state"] == "succeeded"
    v = view(setup)
    r = v["receipts"][0]
    assert r["source"] == "mock" and r["run_id"] == run["id"] and r["attempt"] == 1
    assert r["request_id"] == "chatcmpl-contract-test" and r["elapsed_ms"] >= 0
    assert r["text_valid"] and r["structure_valid"] and r["usage_valid"]
    assert r["cost_micro"] == 280 and r["input_tokens"] == r["output_tokens"] == 100
    assert run["attempts"][0]["request_id"] == r["request_id"]
    assert run["attempts"][0]["elapsed_ms"] == r["elapsed_ms"]
    assert all(c["real_verified_count"] == 0 for c in v["capabilities"])
    with TestClient(create_app(setup[2], setup[4]), headers=dict(setup[0].headers)) as c:
        assert view((c, *setup[1:])) == v
    assert setup[4].calls == 1


def test_one_repair_two_receipts_authorization_and_budget_counts(setup, monkeypatch):
    corrections = []
    before_decisions = []
    original = setup[4].send

    def send(key, base, selected, correction=False):
        if not corrections:
            with setup[3].state.sessions() as session:
                before_decisions.extend(
                    (r.id, copy.deepcopy(r.payload))
                    for r in session.scalars(
                        select(RequirementRecord).where(RequirementRecord.kind == "decision")
                    )
                )
        corrections.append(correction)
        return original(key, base, selected, correction)

    monkeypatch.setattr(setup[4], "send", send)
    invalid = rh.receipt({"wrong": "离线错误结构"})
    run = execute(setup, [invalid, good()])
    assert run["state"] == "succeeded" and len(run["attempts"]) == 2
    v = view(setup)
    assert {r["attempt"] for r in v["receipts"]} == {1, 2}
    assert sorted(r["structure_valid"] for r in v["receipts"]) == [False, True]
    assert sum(r["cost_micro"] for r in v["receipts"]) == 560
    assert v["spent_micro"] == 560 and v["requests_used"] == 2 and v["requests_reserved"] == 0
    assert corrections == [False, True]
    with setup[3].state.sessions() as s:
        decisions = list(s.scalars(select(RequirementRecord).where(RequirementRecord.kind == "decision")))
        assert [(r.id, r.payload) for r in decisions] == before_decisions


def test_repair_failure_keeps_two_receipts_no_candidate(setup):
    run = execute(setup, [rh.receipt({"bad": "结构"}), rh.receipt({"bad": "仍失败"})])
    assert run["state"] == "failed" and run["candidate"] is None
    assert len(view(setup)["receipts"]) == 2 and setup[4].calls == 2


@pytest.mark.parametrize("sent", [False, True])
def test_failure_metadata_and_unknown_budget_are_honest(setup, sent):
    setup[4].errors = [models.ProviderError("MODEL_RESULT_UNKNOWN" if sent else "MODEL_INIT_FAILED", sent)]
    run = execute(setup)
    v = view(setup)
    r = v["receipts"][0]
    assert r["sent"] is sent and r["cost_micro"] == (None if sent else 0)
    assert not r["usage_valid"] and not r["structure_valid"] and r["elapsed_ms"] >= 0
    assert run["state"] == ("result_unknown" if sent else "failed")
    assert bool(v["reserved_micro"]) == sent and setup[4].calls == 1


def test_unknown_usage_receipt_never_false_zero_or_verified(setup):
    value = good()
    value["usage"] = {"prompt_tokens": True, "completion_tokens": 100}
    run = execute(setup, [value])
    v = view(setup)
    assert run["state"] == "result_unknown" and v["reserved_micro"] > 0
    assert v["receipts"][0]["cost_micro"] is None and not v["receipts"][0]["usage_valid"]
    assert run["attempts"][0]["state"] == "unknown" and setup[4].calls == 1


@pytest.mark.parametrize(
    "identifier",
    [
        "sk-" + "x" * 32,
        "chatcmpl-13812345678",
        "chatcmpl-test-only-fake-provider-key",
        "hello@example.com",
        "../secret",
        "<script>",
        "x" * 200,
    ],
)
def test_unsafe_request_identifier_not_stored_or_returned(setup, identifier):
    value = good()
    value["id"] = identifier
    run = execute(setup, [value])
    v = view(setup)
    assert run["attempts"][0]["request_id"] is None and v["receipts"][0]["request_id"] is None
    assert identifier not in json.dumps(v, ensure_ascii=False)


def test_only_current_successful_real_provenance_observations_count(setup, monkeypatch):
    # Isolated persisted fixture for filtering; no actual cloud validation is claimed.
    execute(setup, [good()])
    with models.locked(setup[3].state.sessions) as s:
        row = next(s.scalars(select(RequirementRecord).where(RequirementRecord.kind == "provider_receipt")))
        row.payload = {**row.payload, "source": "real_cloud"}
    assert [c["real_verified_count"] for c in view(setup)["capabilities"] if c["implemented"]] == [1, 1, 1]
    original = caps.adapter_hash()
    monkeypatch.setattr(caps, "adapter_hash", lambda: "a" * 64)
    assert all(c["real_verified_count"] == 0 for c in view(setup)["capabilities"])
    monkeypatch.setattr(caps, "adapter_hash", lambda: original)
    rh.post(
        setup[0],
        "/model/config",
        {
            "api_key": "test-only-fake-provider-key",
            "base_url": "https://testspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        },
    )
    assert all(c["real_verified_count"] == 0 for c in view(setup)["capabilities"])


def test_capability_read_requires_token_and_has_no_write_endpoint(setup):
    c = setup[0]
    assert c.get(PREFIX + "/model/provider", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert rh.post(c, "/model/provider", {"automatic_switch": True}).status_code == 405


@pytest.mark.parametrize("case", ["auth", "limit", "redirect", "timeout", "malformed", "array", "oversize"])
def test_http_adapter_error_contract_never_leaks_or_retries(monkeypatch, case):
    original = models.httpx.Client
    calls = []

    def handler(request):
        calls.append(request)
        if case == "timeout":
            raise httpx.ReadTimeout("secret-response-body", request=request)
        if case in {"auth", "limit", "redirect"}:
            return httpx.Response(
                {"auth": 401, "limit": 429, "redirect": 302}[case],
                text="secret-response-body",
                headers={"location": "https://external.invalid/"},
            )
        return httpx.Response(
            200, content={"malformed": b"{", "array": b"[]", "oversize": b"x" * 262145}[case]
        )

    monkeypatch.setattr(
        models.httpx, "Client", lambda **kw: original(**kw, transport=httpx.MockTransport(handler))
    )
    with pytest.raises(models.ProviderError) as err:
        models.Provider().send("test-only-fake-provider-key", models.DEFAULT_BASE, [])
    assert err.value.sent and len(calls) == 1 and "secret-response-body" not in str(err.value)


def test_http_adapter_metadata_and_request_flags(monkeypatch):
    original = models.httpx.Client
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, json=good(), headers={"x-request-id": "01234567-89ab-cdef-0123-456789abcdef"}
        )

    monkeypatch.setattr(
        models.httpx, "Client", lambda **kw: original(**kw, transport=httpx.MockTransport(handle))
    )
    r = models.Provider().send("test-only-fake-provider-key", models.DEFAULT_BASE, [], correction=True)
    assert r["_factory_request_id"] == "01234567-89ab-cdef-0123-456789abcdef"
    assert requests[0]["stream"] is False and requests[0]["enable_thinking"] is False
    assert requests[0]["response_format"] == {"type": "json_object"} and "tools" not in requests[0]
    assert "上次输出未通过结构校验" in requests[0]["messages"][0]["content"]


def test_revocation_between_invalid_output_and_repair_prevents_second_call(setup, monkeypatch):
    c, pid, settings, app, provider = setup
    _, permission, body = rh.prepare_run(c, pid)
    original = provider.send

    def send(*args, **kwargs):
        response = original(*args, **kwargs)
        assert rh.post(c, rh.ai(pid, "authorizations/" + permission["id"] + "/revoke"), {}).status_code == 200
        return response

    monkeypatch.setattr(provider, "send", send)
    provider.receipts = [rh.receipt({"bad": "不合规"})]
    run = rh.post(c, rh.ai(pid, "runs"), body).json()
    models.process_run(app.state.sessions, settings, provider, run["id"])
    final = c.get(PREFIX + rh.ai(pid, "runs/" + run["id"])).json()
    assert final["state"] == "failed" and final["candidate"] is None
    assert provider.calls == 1 and len(view(setup)["receipts"]) == 1
    assert view(setup)["requests_used"] == 1 and view(setup)["reserved_micro"] == 0


def test_foreign_model_receipt_retains_budget_no_repair(setup):
    value = good()
    value["model"] = "unexpected-paid-model"
    final = execute(setup, [value])
    v = view(setup)
    assert final["state"] == "result_unknown" and setup[4].calls == 1
    assert v["reserved_micro"] > 0 and v["receipts"][0]["cost_micro"] is None
    assert all(c["real_verified_count"] == 0 for c in v["capabilities"])


def test_truncated_output_one_repair_immutable_receipt_results(setup):
    value = good()
    value["choices"][0]["finish_reason"] = "length"
    final = execute(setup, [value, good()])
    assert final["state"] == "succeeded" and len(final["attempts"]) == 2
    receipts = view(setup)["receipts"]
    assert sorted(r["structure_valid"] for r in receipts) == [False, True]
    assert sum(r["cost_micro"] for r in receipts) == final["settled_micro"] == 560
