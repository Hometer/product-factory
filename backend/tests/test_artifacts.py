"""Real local contracts, provenance, raw Git inspection and no-write rollback previews."""

import concurrent.futures
import copy
import hashlib
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app import version_files
from backend.app.db import Project, RequirementRecord
from backend.app.main import PREFIX, create_app
from backend.tests import test_planning as helpers
from backend.tests.test_requirements import post

setup = helpers.setup
prepared = helpers.prepared


def route(pid, suffix=""):
    return f"/projects/{pid}/artifacts{suffix}"


def view(c, pid):
    r = c.get(PREFIX + route(pid))
    assert r.status_code == 200, r.json()
    return r.json()


@pytest.fixture
def ready(prepared):
    c, pid, settings, app, provider, *_ = prepared
    plan = helpers.generate_plan(prepared)
    with app.state.sessions() as session:
        root = Path(session.get(Project, pid).canonical_path)
    (root / "demo.py").write_text("VERSION = 1\n")
    yield c, pid, settings, app, provider, root, plan


def register(ready, **extra):
    c, pid, *_, plan = ready
    s = next(s for s in view(c, pid)["sources"] if s["id"] == "file:demo.py")
    return dict(
        version_id="V1",
        task_id="T1",
        requirement_ids=["R1"],
        kind="code_change",
        title="合成测试代码产物",
        source_id=s["id"],
        source_hash=s["sha256"],
        run_id=None,
        relocates_id=None,
        sensitive_checked=True,
        confirmed=True,
        reason="合成资料人工核对，不代表业务验收",
        **extra,
    )


def entry(ready):
    r = post(ready[0], route(ready[1], "/entries"), register(ready))
    assert r.status_code == 200, r.json()
    return r.json()


def decide(ready, row, status, **extra):
    return post(
        ready[0],
        route(ready[1], "/decisions"),
        dict(
            entry_id=row["id"],
            previous_id=row["state_id"],
            expected_hash=row["state_hash"],
            status=status,
            replacement_id=None,
            confirmed=True,
            reason="人工核对合成产物采用状态",
            **extra,
        ),
    )


def version_body(ready, **extra):
    v = view(ready[0], ready[1])
    return dict(
        task_id="T1",
        version_id="V1",
        requirement_ids=["R1"],
        label="合成测试文件版本",
        files=["demo.py"],
        expected_index_hash=v["index_hash"],
        expected_revision=v["code_revision"],
        reason="只保存显式代码快照，无执行或提交",
        confirmed=True,
        **extra,
    )


def snapshot(ready):
    r = post(ready[0], route(ready[1], "/versions"), version_body(ready))
    assert r.status_code == 200, r.json()
    return r.json()


def rollback_body(ready, target):
    return dict(
        version_id=target["id"],
        expected_hash=target["content_hash"],
        expected_index_hash=view(ready[0], ready[1])["index_hash"],
        compatibility="unknown",
        compatibility_reason="原始数据兼容性尚未人工核对",
        confirmed=True,
        reason="仅对比旧版本，不恢复或覆盖",
    )


def test_empty_is_readonly(setup):
    c, pid, _, app, provider = setup
    with app.state.sessions() as s:
        before = list(s.scalars(select(RequirementRecord)))
    v = view(c, pid)
    assert not v["entries"] and not v["code_versions"] and not v["tasks"]
    with app.state.sessions() as s:
        assert len(list(s.scalars(select(RequirementRecord)))) == len(before)
    assert provider.calls == 0


@pytest.mark.parametrize(
    "kind",
    [
        "requirement",
        "solution",
        "development_document",
        "api_document",
        "code_change",
        "build_artifact",
        "evaluation_set",
        "delivery_package",
    ],
)
def test_eight_text_artifact_types(ready, kind):
    body = register(ready)
    body["kind"] = kind
    r = post(ready[0], route(ready[1], "/entries"), body)
    assert r.status_code == 200 and r.json()["status"] == "draft"
    assert r.json()["current"] and r.json()["payload"]["source"]["sha256"] == body["source_hash"]
    assert ready[4].calls == 0


def test_state_history_and_stale_adoption(ready):
    root = entry(ready)
    assert decide(ready, root, "adopted").status_code == 409
    assert decide(ready, root, "candidate").status_code == 200
    candidate = view(ready[0], ready[1])["entries"][0]
    assert decide(ready, root, "unavailable").status_code == 409
    (ready[5] / "demo.py").write_text("VERSION = 2\n")
    result = decide(ready, candidate, "adopted")
    assert result.status_code == 409 and result.json()["error"]["code"] == "SOURCE_STALE"
    current = view(ready[0], ready[1])["entries"][0]
    assert not current["current"] and current["status"] == "candidate"
    original = ready[0].get(PREFIX + route(ready[1], "/assets/" + root["id"])).json()
    assert original["payload"] == root["payload"]


def adopt(ready, row):
    assert decide(ready, row, "candidate").status_code == 200
    candidate = next(r for r in view(ready[0], ready[1])["entries"] if r["id"] == row["id"])
    assert decide(ready, candidate, "adopted").status_code == 200
    return next(r for r in view(ready[0], ready[1])["entries"] if r["id"] == row["id"])


def test_replacement_preserves_history_and_blocks_cycle(ready):
    a, b = adopt(ready, entry(ready)), adopt(ready, entry(ready))
    body = dict(
        entry_id=a["id"],
        previous_id=a["state_id"],
        expected_hash=a["state_hash"],
        status="superseded",
        replacement_id=b["id"],
        confirmed=True,
        reason="人工选择同范围新采用产物",
    )
    assert post(ready[0], route(ready[1], "/decisions"), body).status_code == 200
    a2 = next(r for r in view(ready[0], ready[1])["entries"] if r["id"] == a["id"])
    assert decide(ready, a2, "candidate").status_code == 200
    a3 = next(r for r in view(ready[0], ready[1])["entries"] if r["id"] == a["id"])
    assert decide(ready, a3, "adopted").status_code == 200
    a4 = next(r for r in view(ready[0], ready[1])["entries"] if r["id"] == a["id"])
    body.update(
        entry_id=b["id"], previous_id=b["state_id"], expected_hash=b["state_hash"], replacement_id=a4["id"]
    )
    assert post(ready[0], route(ready[1], "/decisions"), body).status_code == 409
    assert len(view(ready[0], ready[1])["history"]) == 9


@pytest.mark.parametrize(
    "field,value",
    [
        ("task_id", "T99"),
        ("version_id", "V99"),
        ("requirement_ids", ["R99"]),
        ("run_id", "f" * 8 + "-ffff-ffff-ffff-" + "f" * 12),
        ("source_hash", "0" * 64),
        ("source_id", "record:" + str(uuid4())),
        ("relocates_id", str(uuid4())),
    ],
)
def test_invalid_associations_fail_without_record(ready, field, value):
    body = register(ready)
    body[field] = value
    result = post(ready[0], route(ready[1], "/entries"), body)
    assert result.status_code in {404, 409}
    assert not view(ready[0], ready[1])["entries"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("confirmed", False),
        ("confirmed", "true"),
        ("sensitive_checked", False),
        ("sensitive_checked", 1),
        ("actor", "admin"),
        ("reason", "   "),
        ("requirement_ids", ["R1", "R1"]),
    ],
)
def test_strict_contract(ready, field, value):
    body = register(ready)
    body[field] = value
    assert post(ready[0], route(ready[1], "/entries"), body).status_code == 422


@pytest.mark.parametrize(
    "value", ["api_key=super-secret-private-value", "电话 13800138000", "test-only-local-token"]
)
def test_sensitive_metadata_rejected(ready, value):
    body = register(ready)
    body["reason"] = value
    r = post(ready[0], route(ready[1], "/entries"), body)
    assert r.status_code == 409 and value not in r.text


@pytest.mark.parametrize(
    "name",
    [
        "../demo.py",
        "/tmp/demo.py",
        ".env",
        "backups/demo.py",
        "package.json",
        "nested/../../demo.py",
        "secret.py",
    ],
)
def test_protected_paths(ready, name):
    assert name not in view(ready[0], ready[1])["snapshot_files"]
    body = version_body(ready)
    body["files"] = [name]
    r = post(ready[0], route(ready[1], "/versions"), body)
    assert r.status_code == 409 and not view(ready[0], ready[1])["code_versions"]


@pytest.mark.parametrize("hard", [False, True])
def test_link_refused(ready, hard):
    original = ready[5] / "demo.py"
    if hard:
        os.link(original, ready[5] / "alias.py")
    else:
        (ready[5] / "alias.py").symlink_to(original)
    body = version_body(ready)
    body["files"] = ["alias.py"]
    assert post(ready[0], route(ready[1], "/versions"), body).status_code == 409


def test_snapshot_limits(ready):
    (ready[5] / "large.py").write_text("x" * 65537)
    body = version_body(ready)
    body["files"] = ["large.py"]
    assert post(ready[0], route(ready[1], "/versions"), body).status_code == 409
    names = [f"part{n}.py" for n in range(9)]
    for name in names:
        (ready[5] / name).write_text("x" * 60000)
    body = version_body(ready)
    body["files"] = names
    r = post(ready[0], route(ready[1], "/versions"), body)
    assert r.status_code == 409 and r.json()["error"]["code"] == "SNAPSHOT_LIMIT"


def test_snapshot_rollback_unknown_and_originals_unchanged(ready):
    a = snapshot(ready)
    assert a["current"]
    (ready[5] / "demo.py").write_text("VERSION = 2\n")
    b = snapshot(ready)
    assert b["payload"]["revision"] == 2 and b["current"]
    body = rollback_body(ready, a)
    r = post(ready[0], route(ready[1], "/rollback-previews"), body)
    assert r.status_code == 200, r.json()
    p = r.json()["payload"]
    assert not p["writes_files"] and p["compatibility"] == "unknown"
    assert "+VERSION = 1" in p["differences"][0]["diff"] and "-VERSION = 2" in p["differences"][0]["diff"]
    assert (ready[5] / "demo.py").read_text() == "VERSION = 2\n"
    assert ready[0].get(PREFIX + route(ready[1], "/assets/" + a["id"])).json()["payload"] == a["payload"]
    (ready[5] / "demo.py").write_text("VERSION = 3\n")
    assert not view(ready[0], ready[1])["rollback_previews"][0]["current"]
    assert post(ready[0], route(ready[1], "/rollback-previews"), body).status_code == 409


def test_missing_file_and_relocation(ready):
    a = entry(ready)
    original = ready[5] / "demo.py"
    original.rename(ready[5] / "moved.py")
    assert not view(ready[0], ready[1])["entries"][0]["current"]
    body = dict(a["payload"])
    body = {
        k: v
        for k, v in body.items()
        if k
        in {
            "task_id",
            "version_id",
            "requirement_ids",
            "kind",
            "title",
            "source_id",
            "source_hash",
            "run_id",
            "relocates_id",
            "sensitive_checked",
            "confirmed",
            "reason",
        }
    }
    body.update(source_id="file:moved.py", relocates_id=a["id"])
    r = post(ready[0], route(ready[1], "/entries"), body)
    assert r.status_code == 200 and r.json()["current"]
    assert r.json()["payload"]["relocates_id"] == a["id"]
    assert len(view(ready[0], ready[1])["entries"]) == 2


def test_missing_snapshot_no_restore(ready):
    a = snapshot(ready)
    (ready[5] / "demo.py").unlink()
    r = post(ready[0], route(ready[1], "/rollback-previews"), rollback_body(ready, a))
    assert r.status_code == 200 and r.json()["payload"]["differences"][0]["status"] == "unavailable"
    assert not (ready[5] / "demo.py").exists()


def test_image_metadata_and_signature(ready):
    # Genuine small PNG; bytes never returned by registry.
    import struct
    import zlib

    def chunk(kind, payload):
        return (
            struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
        )

    raw = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff\xff"))
        + chunk(b"IEND", b"")
    )
    (ready[5] / "screen.png").write_bytes(raw)
    body = register(ready)
    body.update(kind="screenshot", source_id="file:screen.png", source_hash=hashlib.sha256(raw).hexdigest())
    r = post(ready[0], route(ready[1], "/entries"), body)
    assert r.status_code == 200 and r.json()["payload"]["source"]["mime"] == "image/png"
    assert "iVBOR" not in r.text
    (ready[5] / "fake.png").write_text("not an image")
    body.update(source_id="file:fake.png", source_hash=hashlib.sha256(b"not an image").hexdigest())
    assert post(ready[0], route(ready[1], "/entries"), body).status_code == 409


def test_idempotency_and_concurrent_revision(ready):
    body = version_body(ready)
    key = str(uuid4())
    r = post(ready[0], route(ready[1], "/versions"), body, key)
    assert r.status_code == 200
    assert post(ready[0], route(ready[1], "/versions"), body, key).json() == r.json()
    changed = copy.deepcopy(body)
    changed["label"] += "另一次"
    assert post(ready[0], route(ready[1], "/versions"), changed, key).status_code == 409
    next_body = version_body(ready)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(lambda _: post(ready[0], route(ready[1], "/versions"), next_body).status_code, range(2))
        )
    assert sorted(results) == [200, 409]


def test_restart_history(ready):
    a = entry(ready)
    b = snapshot(ready)
    headers = {"Authorization": "Bearer " + ready[2].token, "Origin": "http://127.0.0.1:3000"}
    with TestClient(create_app(ready[2], ready[4]), headers=headers) as c:
        assert c.get(PREFIX + route(ready[1], "/assets/" + a["id"])).json()["payload"] == a["payload"]
        assert c.get(PREFIX + route(ready[1], "/assets/" + b["id"])).json()["payload"] == b["payload"]


def git(root, *args):
    return (
        subprocess.run(["/usr/bin/git", *args], cwd=root, check=True, capture_output=True)
        .stdout.decode()
        .strip()
    )


def test_real_git_no_filters_hooks_or_commits(ready):
    root = ready[5]
    git(root, "init")
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "add", "demo.py")
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "synthetic initial",
    )
    head = git(root, "rev-parse", "HEAD")
    sentinel = root / "EXECUTED"
    script = root / "spy.sh"
    script.write_text('#!/bin/sh\ntouch "' + str(sentinel) + '"\ncat\n')
    script.chmod(0o700)
    git(root, "config", "core.fsmonitor", str(script))
    git(root, "config", "filter.spy.clean", str(script))
    git(root, "config", "diff.spy.textconv", str(script))
    git(root, "config", "diff.external", str(script))
    git(root, "config", "core.hooksPath", str(root))
    (root / ".gitattributes").write_text("*.py filter=spy diff=spy\n")
    (root / "demo.py").write_text("VERSION = 2\n")
    v = view(ready[0], ready[1])
    assert v["git"]["status"] == "recorded" and v["git"]["head"] == head
    assert {r["path"]: r["status"] for r in v["git"]["changes"]}["demo.py"] == " M"
    assert "+VERSION = 2" in next(r["diff"] for r in v["git"]["changes"] if r["path"] == "demo.py")
    a = snapshot(ready)
    (root / "demo.py").write_text("VERSION = 3\n")
    assert post(ready[0], route(ready[1], "/rollback-previews"), rollback_body(ready, a)).status_code == 200
    assert not sentinel.exists() and (root / "demo.py").read_text() == "VERSION = 3\n"
    assert git(root, "rev-parse", "HEAD") == head


def test_git_special_metadata_unknown(ready):
    root = ready[5]
    git(root, "init")
    (root / ".git" / "objects" / "info" / "alternates").write_text("/outside/objects")
    assert view(ready[0], ready[1])["git"]["status"] == "unknown"
    assert snapshot(ready)["current"]


def test_bounded_git_failure(ready, monkeypatch):
    root = ready[5]
    git(root, "init")
    monkeypatch.setattr(version_files, "bounded", lambda *_: (_ for _ in ()).throw(ValueError("failed")))
    assert view(ready[0], ready[1])["git"]["status"] == "unknown"
    assert snapshot(ready)["current"]


def test_native_foreign_sources_refused(ready):
    body = register(ready)
    body.update(source_id="execution:" + str(uuid4()))
    assert post(ready[0], route(ready[1], "/entries"), body).status_code == 404
    v = view(ready[0], ready[1])
    native = next(s for s in v["sources"] if s["id"].startswith("plan:"))
    body.update(source_id=native["id"], source_hash=native["sha256"])
    assert post(ready[0], route(ready[1], "/entries"), body).status_code == 200


@pytest.mark.parametrize("private", ["邮箱 name@example.com", "手机 13800138000", "证件 11010519491231002X"])
def test_privacy_detection_on_long_text_remains_strict(private):
    from backend.app import models

    assert models.PII.search("x" * 100000) is None
    assert models.PII.search("x" * 100000 + "\n" + private)


def test_cross_project_asset_and_source_not_visible(ready):
    c, pid, _, app, _, root, _ = ready
    a = entry(ready)
    b = snapshot(ready)
    preview = post(
        c,
        "/projects/create-preview",
        dict(
            name="其他合成项目", target_user="负责人", core_task="归属核对", path=str(root.parent / "other")
        ),
    ).json()
    other = post(
        c, "/projects/confirm", dict(preview_id=preview["preview_id"], expected_hash=preview["content_hash"])
    ).json()["id"]
    assert c.get(PREFIX + route(other, "/assets/" + a["id"])).status_code == 404
    assert c.get(PREFIX + route(other, "/assets/" + b["id"])).status_code == 404
    assert not any(s["id"] == "version:" + b["id"] for s in view(c, other)["sources"])
    with app.state.sessions() as session:
        session.add(Project(id=str(uuid4()), canonical_path=str(root / "child"), payload={}))
        session.commit()
    (root / "child").mkdir()
    (root / "child" / "other.py").write_text("OTHER = 1\n")
    body = register(ready)
    body.update(source_id="file:child/other.py", source_hash=hashlib.sha256(b"OTHER = 1\n").hexdigest())
    assert post(c, route(pid, "/entries"), body).status_code == 409


def test_stale_snapshot_revision_and_replaced_source_are_rejected(ready):
    body = version_body(ready)
    (ready[5] / "demo.py").write_text("VERSION = 2\n")
    assert post(ready[0], route(ready[1], "/versions"), body).status_code == 409
    a = entry(ready)
    old = dict(a["payload"])
    with ready[3].state.sessions() as s:
        row = s.get(RequirementRecord, a["id"])
        assert row.payload == old
    assert decide(ready, a, "candidate").status_code == 200
    candidate = view(ready[0], ready[1])["entries"][0]
    (ready[5] / "demo.py").unlink()
    (ready[5] / "demo.py").symlink_to(ready[5] / "README.md")
    assert decide(ready, candidate, "adopted").status_code == 409


def test_existing_analysis_run_is_linked_without_execution_or_truth_claim(ready):
    c, pid, _, app, provider, *_ = ready
    identifier = str(uuid4())
    with app.state.sessions() as session:
        session.add(
            RequirementRecord(
                id=identifier,
                project_id=pid,
                kind="run",
                payload=dict(state="succeeded", revision=1, source="mock", candidate=None),
            )
        )
        session.commit()
    item = next(s for s in view(c, pid)["sources"] if s["id"] == "model-run:" + identifier)
    assert item["provenance"] == "mock" and item["run_id"] == identifier
    body = register(ready)
    body.update(source_id=item["id"], source_hash=item["sha256"], run_id=identifier)
    r = post(c, route(pid, "/entries"), body)
    assert r.status_code == 200 and r.json()["status"] == "draft" and provider.calls == 0


def test_historical_decision_displays_its_original_state(ready):
    c, pid, *_ = ready
    a = entry(ready)
    candidate = decide(ready, a, "candidate").json()
    now = view(c, pid)["entries"][0]
    assert decide(ready, now, "adopted").status_code == 200
    original = c.get(PREFIX + route(pid, "/assets/" + candidate["id"])).json()
    assert original["status"] == "candidate" and original["payload"] == candidate["payload"]
    assert not original["current"]
    assert view(c, pid)["entries"][0]["status"] == "adopted"
