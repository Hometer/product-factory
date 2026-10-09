"""Selective immutable ZIP, actual gate delegation, local privacy and replay contracts."""

import base64
import concurrent.futures
import copy
import hashlib
import io
import json
import os
from uuid import uuid4
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app import acceptance, delivery, models
from backend.app.db import RequirementRecord
from backend.app.main import PREFIX, create_app
from backend.tests import test_artifacts as h
from backend.tests.test_requirements import post

setup = h.setup
prepared = h.prepared
ready = h.ready


def route(pid, suffix=""):
    return f"/projects/{pid}/delivery{suffix}"


def view(ex):
    r = ex[0].get(PREFIX + route(ex[1]))
    assert r.status_code == 200, r.json()
    return r.json()


def preview_body(ex, **extra):
    b = dict(
        stage_id="S1",
        expected_index_hash=view(ex)["index_hash"],
        files=["demo.py"],
        startup_steps=["在交付目录执行 python demo.py 并核对 VERSION 内容"],
        config_names=["API_KEY", "SERVICE_PORT"],
        limitations="合成代码没有真实业务和模型效果验证",
        backup_recovery="先复制交付目录到本机备份，恢复到新目录并按清单核对摘要，不覆盖原始资料",
        confirmed=True,
        sensitive_checked=True,
        reason="合成隔离工程合同测试，非真实业务验收",
    )
    b.update(extra)
    return b


def preview(ex, **extra):
    r = post(ex[0], route(ex[1], "/previews"), preview_body(ex, **extra))
    assert r.status_code == 200, r.json()
    return r.json()


def package_body(p, **extra):
    b = dict(
        preview_id=p["id"],
        expected_hash=p["content_hash"],
        confirmed=True,
        sensitive_checked=True,
        reason="人工确认合成选择性包，未批准代码执行",
    )
    b.update(extra)
    return b


def package(ex, p=None):
    r = post(ex[0], route(ex[1], "/packages"), package_body(p or preview(ex)))
    assert r.status_code == 200, r.json()
    return r.json()


def release_body(p, **extra):
    b = dict(
        package_id=p["id"],
        expected_hash=p["content_hash"],
        previous_id=None,
        outcome="partial",
        address="http://127.0.0.1:3000",
        confirmed=True,
        reason="合成本机人工报告，不代表生产发布完成",
        checks={
            k: dict(status="pending", reason="尚未在生产环境实际检查", evidence=[])
            for k in ("isolation", "persistence", "secrets", "errors", "monitoring", "recovery")
        },
    )
    b.update(extra)
    return b


def download(ex, p):
    return ex[0].get(PREFIX + route(ex[1], "/packages/" + p["id"] + "/download"))


def test_empty_readonly(setup):
    c, pid, _, app, provider = setup
    with app.state.sessions() as s:
        before = len(list(s.scalars(select(RequirementRecord))))
    v = view(setup)
    assert not v["packages"] and not v["stages"] and not v["previews"]
    with app.state.sessions() as s:
        assert len(list(s.scalars(select(RequirementRecord)))) == before
    assert provider.calls == 0


def test_selective_zip_and_blank_config(ready):
    root = ready[5]
    (root / "选择说明.md").write_text("本文件明确包含，合成资料。\n")
    (root / "未选择.md").write_text("这份资料明确不在交付包内。\n")
    (root / ".env").write_text("PASSWORD=hidden-sensitive-test\n")
    p = package(ready, preview(ready, files=["demo.py", "选择说明.md"]))
    assert p["payload"]["status"] == "partial_delivery" and p["blockers"]
    assert "zip_base64" not in p["payload"]
    assert all("content" not in f for f in p["payload"]["files"])
    r = download(ready, p)
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert r.headers["cache-control"] == "no-store"
    assert hashlib.sha256(r.content).hexdigest() == p["payload"]["zip_hash"]
    with ZipFile(io.BytesIO(r.content)) as z:
        assert set(z.namelist()) == {"files/demo.py", "files/选择说明.md"} | {
            "delivery/" + d for d in delivery.DOCS
        }
        assert z.read("files/demo.py") == (root / "demo.py").read_bytes()
        cfg = json.loads(z.read("delivery/config.example.json"))
        assert cfg["environment"] == [dict(name="API_KEY", value=None), dict(name="SERVICE_PORT", value=None)]
        manifest = json.loads(z.read("delivery/manifest.json"))
        assert manifest["status"] == "partial_delivery"
        assert not json.loads(z.read("delivery/acceptance.json"))["effective"]
        assert json.loads(z.read("delivery/requirements.json"))["items"][0]["id"] == "R1"
        assert all(b"hidden-sensitive-test" not in z.read(n) for n in z.namelist())
    assert ready[4].calls == 0


@pytest.mark.parametrize(
    "changes",
    [
        dict(confirmed=False),
        dict(confirmed="true"),
        dict(sensitive_checked=False),
        dict(actor="somebody"),
        dict(files=[]),
        dict(files=["demo.py", "demo.py"]),
        dict(config_names=["API_KEY=secret"]),
        dict(config_names=["a-b"]),
        dict(config_names=["API_KEY", "API_KEY"]),
        dict(startup_steps=["  "]),
        dict(limitations="        "),
        dict(backup_recovery="        "),
    ],
)
def test_strict_preview(ready, changes):
    r = post(ready[0], route(ready[1], "/previews"), preview_body(ready, **changes))
    assert r.status_code == 422 and set(r.json()) == {"error"}
    assert not view(ready)["previews"]


@pytest.mark.parametrize(
    "name",
    [
        "../demo.py",
        "/demo.py",
        ".env",
        "AGENTS.md",
        "node_modules/a.py",
        "data/a.py",
        "other\\demo.py",
        "package.json",
    ],
)
def test_protected_paths(ready, name):
    r = post(ready[0], route(ready[1], "/previews"), preview_body(ready, files=[name]))
    assert r.status_code == 409 and not view(ready)["previews"]


@pytest.mark.parametrize(
    "content",
    [
        "API_KEY=example-sensitive-12345",
        "contact=person@example.com",
        "mobile=13800138000",
        "\0unsafe",
        "x" * 65537,
    ],
)
def test_content_not_packaged(ready, content):
    (ready[5] / "excluded.py").write_text(content)
    b = preview_body(ready, files=["excluded.py"])
    r = post(ready[0], route(ready[1], "/previews"), b)
    assert r.status_code == 409 and not view(ready)["previews"]


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_links(ready, kind):
    root = ready[5]
    if kind == "symlink":
        (root / "linked.py").symlink_to(root / "demo.py")
    else:
        os.link(root / "demo.py", root / "linked.py")
    r = post(ready[0], route(ready[1], "/previews"), preview_body(ready, files=["linked.py"]))
    assert r.status_code == 409


def test_total_limit(ready):
    names = [f"f{i}.py" for i in range(9)]
    for n in names:
        (ready[5] / n).write_text("x" * 65536)
    r = post(ready[0], route(ready[1], "/previews"), preview_body(ready, files=names))
    assert r.status_code == 409 and r.json()["error"]["code"] == "SNAPSHOT_LIMIT"


def test_changes_block_confirmation_and_frozen_download(ready):
    p = preview(ready)
    q = package(ready, p)
    original = download(ready, q).content
    (ready[5] / "demo.py").write_text("VERSION = 2\n")
    r = post(ready[0], route(ready[1], "/packages"), package_body(p))
    assert r.status_code == 409 and r.json()["error"]["code"] == "DELIVERY_STALE"
    assert not view(ready)["packages"][0]["current"]
    assert download(ready, q).content == original
    newer = package(ready)
    assert newer["id"] != q["id"] and len(view(ready)["packages"]) == 2


def test_replay_and_conflict(ready):
    body = preview_body(ready)
    key = str(uuid4())
    one = post(ready[0], route(ready[1], "/previews"), body, key)
    two = post(ready[0], route(ready[1], "/previews"), body, key)
    assert one.json() == two.json()
    changed = post(ready[0], route(ready[1], "/previews"), {**body, "reason": "不同正文应阻止重复记录"}, key)
    assert changed.status_code == 409
    key = str(uuid4())
    b = package_body(one.json())
    q = post(ready[0], route(ready[1], "/packages"), b, key)
    assert post(ready[0], route(ready[1], "/packages"), b, key).json() == q.json()
    assert len(view(ready)["previews"]) == len(view(ready)["packages"]) == 1


def test_partial_report_append_and_conflict(ready):
    q = package(ready)
    b = release_body(q)
    r = post(ready[0], route(ready[1], "/releases"), b)
    assert r.status_code == 200, r.json()
    assert r.json()["payload"]["source"] == "manual_report"
    assert not r.json()["payload"]["deployed_by_platform"]
    assert post(ready[0], route(ready[1], "/releases"), b).status_code == 409
    b["previous_id"] = r.json()["id"]
    b["checks"]["recovery"] = dict(
        status="failed", reason="合成恢复核对未通过", evidence=["本机合成恢复检查说明"]
    )
    newer = post(ready[0], route(ready[1], "/releases"), b)
    assert newer.status_code == 200 and len(view(ready)["releases"]) == 2
    assert not next(x for x in view(ready)["releases"] if x["id"] == r.json()["id"])["current"]


def test_no_release_by_address_or_declarations(ready):
    q = package(ready)
    b = release_body(q, outcome="reported_release")
    r = post(ready[0], route(ready[1], "/releases"), b)
    assert r.status_code == 409 and r.json()["error"]["code"] == "RELEASE_BLOCKED"
    for v in b["checks"].values():
        v.update(status="passed", evidence=["只是人工声明，缺有效验收"])
    assert post(ready[0], route(ready[1], "/releases"), b).status_code == 409
    assert not view(ready)["releases"]


@pytest.mark.parametrize(
    "changes",
    [
        dict(checks={}),
        dict(confirmed=False),
        dict(actor="owner"),
        dict(address="https://user:secret@example.invalid/"),
        dict(address="https://example.invalid/?token=x"),
        dict(address="https://example.invalid/#secret"),
        dict(address="file:///tmp/f"),
        dict(address="https://example.invalid:invalid/"),
    ],
)
def test_release_contract(ready, changes):
    q = package(ready)
    r = post(ready[0], route(ready[1], "/releases"), release_body(q, **changes))
    assert r.status_code == 422 and not view(ready)["releases"]


def test_passed_requires_evidence(ready):
    q = package(ready)
    b = release_body(q)
    b["checks"]["secrets"]["status"] = "passed"
    assert post(ready[0], route(ready[1], "/releases"), b).status_code == 422


def test_cross_project_references(ready):
    c, pid, _, _, _, root, _ = ready
    v = c.post(
        PREFIX + "/projects/create-preview",
        json=dict(
            name="另一个合成项目", target_user="负责人", core_task="核对隔离", path=str(root.parent / "other")
        ),
    ).json()
    other = post(
        c, "/projects/confirm", dict(preview_id=v["preview_id"], expected_hash=v["content_hash"])
    ).json()["id"]
    p = preview(ready)
    q = package(ready, p)
    assert post(c, route(other, "/packages"), package_body(p)).status_code == 404
    assert post(c, route(other, "/releases"), release_body(q)).status_code == 404
    assert c.get(PREFIX + route(other, f"/packages/{q['id']}/download")).status_code == 404


def test_new_app_download_immutable_and_budget_zero(ready):
    q = package(ready)
    raw = download(ready, q).content
    app = create_app(ready[2], ready[4])
    with TestClient(app, headers=dict(ready[0].headers)) as c:
        assert c.get(PREFIX + route(ready[1], f"/packages/{q['id']}/download")).content == raw
        assert c.get(PREFIX + route(ready[1])).json()["packages"][0]["id"] == q["id"]
    assert ready[4].calls == 0
    with ready[3].state.sessions() as s:
        assert models.budget(s).requests_used == 0


def test_corrupt_package_not_served(ready):
    q = package(ready)
    with ready[3].state.sessions() as s:
        r = s.get(RequirementRecord, q["id"])
        p = copy.deepcopy(r.payload)
        p["zip_base64"] = base64.b64encode(b"corrupted").decode()
        r.payload = p
        s.commit()
    r = download(ready, q)
    assert r.status_code == 409 and r.json()["error"]["code"] == "DELIVERY_UNAVAILABLE"


def test_actual_key_and_token_not_packaged(ready):
    for sensitive in [ready[2].token, "new-configured-secret-test-only"]:
        if sensitive != ready[2].token:
            ready[2].model_env.write_text("FACTORY_MODEL_KEY=" + sensitive + "\n")
            ready[2].model_env.chmod(0o600)
        r = post(
            ready[0],
            route(ready[1], "/previews"),
            preview_body(ready, limitations="合成限制内容：" + sensitive),
        )
        assert r.status_code == 409


def test_effective_gate_delegates_to_actual_acceptance(ready):
    # An adopted artifact or a file alone never substitutes the A13 decision.
    h.entry(ready)
    with ready[3].state.sessions() as s:
        v = delivery.effective(s, ready[2], ready[1], "S1")
        assert not v["effective"] and v["blockers"]


def test_isolated_green_and_then_stale_contract(ready, monkeypatch):
    """Deterministic delivery contract only; explicitly not real model validation."""

    def green(session, settings, pid, sid):
        return dict(
            stage_id=sid,
            package_id=None,
            package_hash=None,
            decision_id=None,
            decision_hash=None,
            effective=True,
            blockers=[],
        )

    monkeypatch.setattr(delivery, "effective", green)
    q = package(ready)
    assert q["payload"]["status"] == "local_ready"
    b = release_body(q, outcome="reported_release")
    for v in b["checks"].values():
        v.update(status="passed", evidence=["隔离契约，非真实环境验证"])
    r = post(ready[0], route(ready[1], "/releases"), b)
    assert r.status_code == 200 and r.json()["payload"]["source"] == "manual_report"
    monkeypatch.setattr(
        delivery, "effective", lambda *args: dict(green(*args), effective=False, blockers=["验收变化需复验"])
    )
    v = view(ready)
    assert not v["packages"][0]["current"] and not v["releases"][0]["current"]
    assert v["releases"][0]["payload"]["outcome"] == "reported_release"


def test_all_plan_stages_required(ready, monkeypatch):
    actual = delivery.effective
    monkeypatch.setattr(
        delivery,
        "effective",
        lambda s, st, p, sid: (
            dict(
                stage_id=sid,
                package_id=None,
                package_hash=None,
                decision_id=None,
                decision_hash=None,
                effective=True,
                blockers=[],
            )
            if sid == "S1"
            else actual(s, st, p, sid)
        ),
    )
    q = package(ready)
    assert q["payload"]["status"] == "local_ready"
    b = release_body(q, outcome="reported_release")
    for v in b["checks"].values():
        v.update(status="passed", evidence=["隔离测试本阶段检查报告"])
    assert post(ready[0], route(ready[1], "/releases"), b).status_code == 409


def test_doc_only_stays_partial(ready):
    (ready[5] / "说明.md").write_text("只有说明文档，不含运行代码。\n")
    q = package(ready, preview(ready, files=["说明.md"]))
    assert q["payload"]["status"] == "partial_delivery"
    assert any("代码" in b for b in q["blockers"])


@pytest.mark.parametrize(
    "changes",
    [
        dict(files=["bad\n.py"]),
        dict(files=["bad\x7f.py"]),
        dict(files=["bad\ud800.py"]),
        dict(startup_steps=["合成启动步骤\ud800"]),
    ],
)
def test_nested_utf8_and_names_are_structured_errors(ready, changes):
    body = preview_body(ready, **changes)
    r = ready[0].post(
        PREFIX + route(ready[1], "/previews"),
        content=json.dumps(body, ensure_ascii=True),
        headers={"Content-Type": "application/json", "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 422 and set(r.json()) == {"error"}


def test_newly_configured_protected_value_blocks_old_zip(ready):
    p = preview(ready, limitations="合成资料说明：protected-later-test-value，非真实效果验证")
    q = package(ready, p)
    ready[2].model_env.write_text("FACTORY_MODEL_KEY=protected-later-test-value\n")
    ready[2].model_env.chmod(0o600)
    assert download(ready, q).status_code == 409


def test_partial_address_can_be_empty(ready):
    q = package(ready)
    r = post(ready[0], route(ready[1], "/releases"), release_body(q, address=""))
    assert r.status_code == 200, r.json()
    assert r.json()["payload"]["address"] == ""


def test_concurrent_idempotent_confirm(ready):
    p = preview(ready)
    key = str(uuid4())
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: post(ready[0], route(ready[1], "/packages"), package_body(p), key), range(2))
        )
    assert all(r.status_code == 200 for r in results)
    assert results[0].json() == results[1].json() and len(view(ready)["packages"]) == 1


@pytest.mark.parametrize(
    "change", ["blockers", "package_stale", "decision_stale", "rejected", "wrong_package"]
)
def test_effective_rechecks_gate_and_matching_decision(ready, monkeypatch, change):
    from types import SimpleNamespace

    p = SimpleNamespace(id="contract-package", payload={"stage": {"id": "S1"}})
    d = SimpleNamespace(id="contract-decision", payload={"outcome": "passed", "package_id": p.id})
    if change == "rejected":
        d.payload["outcome"] = "rejected"
    if change == "wrong_package":
        d.payload["package_id"] = "different-package"
    monkeypatch.setattr(
        acceptance,
        "latest",
        lambda s, pid, sid, kind="acceptance_package": p if kind == "acceptance_package" else d,
    )
    monkeypatch.setattr(
        acceptance,
        "public",
        lambda s, st, row: dict(
            current=not (change == "package_stale" and row is p or change == "decision_stale" and row is d),
            blockers=["必需项待验"] if change == "blockers" else [],
        ),
    )
    with ready[3].state.sessions() as s:
        assert not delivery.effective(s, ready[2], ready[1], "S1")["effective"]
