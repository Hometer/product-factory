import copy
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import patches, planning
from backend.app.db import AuditEvent, ModelBudget, PatchAsset, PatchRun, Project
from backend.app.main import PREFIX, create_app
from backend.app.patch_files import Files, workspace_lock
from backend.tests import test_execution as eh
from backend.tests import test_planning as ph
from backend.tests import test_requirements as rh
from backend.tests.test_requirements import post

setup = rh.setup
prepared = ph.prepared
ready = eh.ready


def route(pid, suffix=""):
    return f"/projects/{pid}/patches{suffix}"


@pytest.fixture
def candidate(ready):
    client, pid, settings, app, provider, plan, row = ready
    root = settings.allowed_roots[0] / "demo"
    (root / "one.txt").write_text("original first\n")
    (root / "one.txt").chmod(0o640)
    text = copy.deepcopy(row["payload"]["text"])
    text["files"] = ["one.txt", "two.txt"]
    row = eh.save(ready, text).json()
    grant = eh.review(client, pid, row, "authorize").json()
    body = {
        "execution_id": row["id"],
        "expected_hash": row["content_hash"],
        "files": [
            {"path": "one.txt", "content": "candidate first\n"},
            {"path": "two.txt", "content": "candidate second\n"},
        ],
        "validation_plan": "检查两文件内容、运行相关测试；仍保留原闸门",
    }
    yield client, pid, settings, app, provider, plan, row, grant, body, root


def preview(candidate, **changes):
    c, pid, *rest = candidate
    result = post(c, route(pid, "/preview"), {**candidate[-2], **changes})
    assert result.status_code == 200, result.json()
    return result.json()


def apply(candidate, asset, key=None, **changes):
    client, pid = candidate[:2]
    body = {
        "patch_id": asset["id"],
        "expected_hash": asset["content_hash"],
        "confirmed": True,
        "reason": "测试审阅的具体补丁，真实临时文件",
    }
    return post(client, route(pid, "/apply"), {**body, **changes}, key)


def test_real_files_preview_apply_replay_and_restart(candidate):
    client, pid, settings, app, provider, _, _, grant, _, root = candidate
    asset = preview(candidate)
    assert asset["can_apply"] and asset["payload"]["source"] == "manual_input"
    assert asset["payload"]["acceptance"] == {"R1": "重启后需求与基线可读取"}
    assert len(asset["payload"]["required_checks"]) == 10
    assert "-original first" in asset["payload"]["files"][0]["diff"]
    assert (root / "one.txt").read_text() == "original first\n" and not (root / "two.txt").exists()
    result = apply(candidate, asset, "patch-apply-test-key")
    assert result.status_code == 200, result.json()
    run = result.json()
    assert run["state"] == "applied" and run["details"]["authorization_id"] == grant["id"]
    assert (root / "one.txt").read_text() == "candidate first\n"
    assert (root / "two.txt").read_text() == "candidate second\n"
    assert (root / "one.txt").stat().st_mode & 0o777 == 0o640
    assert (root / "two.txt").stat().st_mode & 0o777 == 0o600
    stamp = (root / "one.txt").stat().st_mtime_ns
    assert apply(candidate, asset, "patch-apply-test-key").json() == run
    assert apply(candidate, asset).json() == run
    assert (root / "one.txt").stat().st_mtime_ns == stamp
    with TestClient(create_app(settings, provider), headers=dict(client.headers)) as reopened:
        stored = reopened.get(PREFIX + route(pid)).json()
        assert stored["runs"][0] == run and stored["assets"][0]["id"] == asset["id"]
    backup = settings.data_dir / "patch-backups" / run["id"]
    assert (backup / "0.before").read_text() == "original first\n"
    assert (backup / "0.before").stat().st_mode & 0o777 == 0o600
    with app.state.sessions() as s:
        assert len(patches.runs(s, pid)) == 1
        from sqlalchemy import select

        for event in s.scalars(select(AuditEvent).where(AuditEvent.project_id == pid)):
            assert "candidate first" not in str(event.details) and "original first" not in str(event.details)
        assert not list(s.scalars(select(ModelBudget))) or all(
            b.spent_micro == b.reserved_micro == b.requests_used == b.requests_reserved == 0
            for b in s.scalars(select(ModelBudget))
        )
    assert provider.calls == 0


def test_external_conflict_preserves_all_three_versions(candidate):
    asset = preview(candidate)
    root = candidate[-1]
    (root / "one.txt").write_text("external third revision\n")
    result = apply(candidate, asset)
    assert result.status_code == 409 and result.json()["error"]["code"] == "FILE_CONFLICT"
    assert (root / "one.txt").read_text() == "external third revision\n" and not (root / "two.txt").exists()
    stored = candidate[0].get(PREFIX + route(candidate[1], "/assets/" + asset["id"])).json()
    assert not stored["can_apply"] and stored["payload"]["files"][0]["before"] == "original first\n"
    assert stored["payload"]["files"][0]["after"] == "candidate first\n"


@pytest.mark.parametrize(
    "mutation", ["revoked", "expired", "new_execution", "dependency", "nonzero", "wrong_hash", "unconfirmed"]
)
def test_stale_authorization_and_scope_block(candidate, mutation):
    client, pid, _, app, _, _, row, grant, _, root = candidate
    asset = preview(candidate)
    change = {}
    if mutation == "revoked":
        post(client, eh.api(pid, "/revoke"), {"authorization_id": grant["id"], "reason": "测试撤销文件范围"})
    elif mutation == "expired":
        with app.state.sessions.begin() as s:
            planning.owned(s, pid, row["payload"]["inputs"]["environment_id"]).created_at = datetime.now(
                UTC
            ) - timedelta(minutes=16)
    elif mutation in {"dependency", "nonzero"}:
        # Corrupting durable source simulates unexpected upstream changes; never trust a client flag.
        with app.state.sessions.begin() as s:
            record = patches.execution.owned(s, pid, row["id"])
            content = copy.deepcopy(record.payload)
            if mutation == "dependency":
                content["inputs"]["task_dependencies"] = ["T999"]
            else:
                content["text"]["estimate_micro"] = 1
            record.payload = content
    elif mutation == "new_execution":
        assert eh.save((*candidate[:6], row)).status_code == 200
    elif mutation == "wrong_hash":
        change["expected_hash"] = "0" * 64
    else:
        change["confirmed"] = False
    result = apply(candidate, asset, **change)
    assert result.status_code in {400, 409}, result.json()
    assert (root / "one.txt").read_text() == "original first\n" and not (root / "two.txt").exists()


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        "/tmp/a.txt",
        ".env",
        "data/a.txt",
        "alembic/a.py",
        "migrations/a.py",
        "package.json",
        "AGENTS.md",
        "node_modules/a.js",
        "secrets.txt",
        "backup.sqlite3",
        "file.sql",
        "docs//a.txt",
        "uploads/a.txt",
        "Data/a.txt",
        "NODE_MODULES/a.js",
        "MIGRATIONS/a.py",
    ],
)
def test_path_allowlist_independently_enforced(candidate, path):
    client, pid, _, app, _, _, row, _, body, root = candidate
    # Inject into old A4 persisted assets so A5 must independently validate, even with bad upstream data.
    with app.state.sessions.begin() as s:
        r = patches.execution.owned(s, pid, row["id"])
        payload = copy.deepcopy(r.payload)
        payload["text"]["files"] = [path]
        r.payload = payload
        body = {
            **body,
            "expected_hash": patches.digest(payload),
            "files": [{"path": path, "content": "new text"}],
        }
    result = post(client, route(pid, "/preview"), body)
    assert result.status_code in {400, 409}, result.json()
    assert (root / "one.txt").read_text() == "original first\n"


@pytest.mark.parametrize(
    "content",
    [
        "\x00binary",
        "x" * 65537,
        "中" * 23000,
        '{"api_key":"fake-only-secret-value"}',
        "ALTER TABLE accounts ADD COLUMN balance INT",
        "\ud800",
    ],
)
def test_unsafe_candidate_not_stored_or_echoed(candidate, content):
    client, pid, _, app, *_ = candidate
    body = {**candidate[-2], "files": [{"path": "one.txt", "content": content}]}
    result = client.post(
        PREFIX + route(pid, "/preview"),
        content=json.dumps(body, ensure_ascii=True),
        headers={"Content-Type": "application/json", "Idempotency-Key": "unsafe-candidate-key"},
    )
    assert result.status_code in {400, 409, 422}, result.json()
    assert "fake-only-secret-value" not in result.text
    from sqlalchemy import select

    with app.state.sessions() as s:
        assert not list(s.scalars(select(PatchAsset)))


@pytest.mark.parametrize(
    "kind",
    [
        "symlink",
        "parent_link",
        "hardlink",
        "fifo",
        "binary",
        "oversized",
        "missing_parent",
        "mode_changed",
        "root_replaced",
        "parent_replaced",
    ],
)
def test_file_races_and_unsafe_sources_stop_without_escaping(candidate, kind, tmp_path):
    client, pid, _, _, _, _, _, _, body, root = candidate
    asset = preview(candidate)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside preserved")
    target = root / "one.txt"
    if kind in {"symlink", "hardlink", "fifo"}:
        target.unlink()
        if kind == "symlink":
            target.symlink_to(outside)
        elif kind == "hardlink":
            os.link(outside, target)
        else:
            os.mkfifo(target)
    elif kind == "binary":
        target.write_bytes(b"\xff\xfe")
    elif kind == "oversized":
        target.write_bytes(b"x" * 65537)
    elif kind == "mode_changed":
        target.chmod(0o644)
    elif kind == "root_replaced":
        root.rename(root.with_name("old-root"))
        root.mkdir()
        target.write_text("original first\n")
        target.chmod(0o640)
    else:
        sub = root / "sub"
        if kind == "parent_link":
            sub.symlink_to(tmp_path, target_is_directory=True)
        elif kind == "parent_replaced":
            sub.mkdir()
            (sub / "one.txt").write_text("original sub\n")
        names = ["sub/one.txt"]
        row = eh.save((*candidate[:6], candidate[6]), {**candidate[6]["payload"]["text"], "files": names})
        if kind == "parent_replaced":
            row = row.json()
            eh.review(client, pid, row, "authorize")
            body = {
                **body,
                "execution_id": row["id"],
                "expected_hash": row["content_hash"],
                "files": [{"path": names[0], "content": "candidate sub\n"}],
            }
            asset = post(client, route(pid, "/preview"), body).json()
            sub.rename(root / "old-sub")
            sub.mkdir()
            (sub / "one.txt").write_text("original sub\n")
        else:
            if row.status_code == 200:
                r = row.json()
                body = {
                    **body,
                    "execution_id": r["id"],
                    "expected_hash": r["content_hash"],
                    "files": [{"path": names[0], "content": "candidate sub\n"}],
                }
                assert post(client, route(pid, "/preview"), body).status_code == 409
            else:
                assert row.status_code == 409
            assert outside.read_text() == "outside preserved"
            return
    assert apply(candidate, asset).status_code == 409
    assert outside.read_text() == "outside preserved"


def test_multiple_file_failure_restores_before_and_new_file_removed(candidate, monkeypatch):
    asset = preview(candidate)
    real = Files.write
    counter = 0

    def fail_once(self, name, content, mode, expected):
        nonlocal counter
        counter += 1
        real(self, name, content, mode, expected)
        if counter == 2:
            raise OSError("injected failure after second replace")

    monkeypatch.setattr(Files, "write", fail_once)
    result = apply(candidate, asset)
    assert result.status_code == 200, result.json()
    assert result.json()["state"] == "rolled_back"
    assert (candidate[-1] / "one.txt").read_text() == "original first\n"
    assert not (candidate[-1] / "two.txt").exists()
    assert apply(candidate, asset).json() == result.json()


def test_external_change_during_failure_stops_recovery(candidate, monkeypatch):
    asset = preview(candidate)
    real = Files.write

    def fail(self, name, content, mode, expected):
        if name == "two.txt":
            (candidate[-1] / "one.txt").write_text("external after first step\n")
            raise OSError("injected second-step failure")
        return real(self, name, content, mode, expected)

    monkeypatch.setattr(Files, "write", fail)
    result = apply(candidate, asset).json()
    assert result["state"] == "rollback_required"
    assert (candidate[-1] / "one.txt").read_text() == "external after first step\n"
    c, pid = candidate[:2]
    assert (
        post(c, route(pid, "/recover"), {"run_id": result["id"], "confirmed": True}).json()["state"]
        == "rollback_required"
    )
    # Manual resolution to known candidate allows safe compensation; unknown content was never overwritten.
    (candidate[-1] / "one.txt").write_text("candidate first\n")
    monkeypatch.setattr(Files, "write", real)
    assert (
        post(c, route(pid, "/recover"), {"run_id": result["id"], "confirmed": True}).json()["state"]
        == "rolled_back"
    )
    assert (candidate[-1] / "one.txt").read_text() == "original first\n"


def test_workspace_lock_and_active_run_block(candidate):
    asset = preview(candidate)
    with workspace_lock(candidate[2], candidate[-1]):
        assert apply(candidate, asset).status_code == 409
    with candidate[3].state.sessions.begin() as s:
        s.add(
            PatchRun(
                id="pending-run",
                project_id=candidate[1],
                patch_id=asset["id"],
                state="rollback_required",
                details={"message": "pending", "completed": [], "reason": "test", "authorization_id": None},
            )
        )
    second = preview(candidate)
    assert apply(candidate, second).json()["error"]["code"] == "RECOVERY_REQUIRED"


@pytest.mark.parametrize("target", ["self", "ancestor", "child_project"])
def test_factory_and_other_project_protected(candidate, target):
    from backend.app.core import ROOT

    client, pid, settings, app = candidate[:4]
    if target == "child_project":
        with app.state.sessions.begin() as s:
            s.add(Project(id="nested-project", canonical_path=str(candidate[-1].parent), payload={}))
    else:
        # Settings are mutable only for this test's app fixture.
        from dataclasses import replace

        from backend.app.patch_files import root_check
        from backend.app.scanner import BusinessError

        with pytest.raises(BusinessError):
            root_check(ROOT if target == "self" else ROOT.parent, settings)
        settings = replace(settings, allowed_roots=(ROOT.parent,))
        alternate = create_app(settings, candidate[4])
        with app.state.sessions.begin() as s:
            s.get(Project, pid).canonical_path = str(ROOT if target == "self" else ROOT.parent)
    if target != "child_project":
        with TestClient(alternate, headers=dict(client.headers)) as other:
            result = post(other, route(pid, "/preview"), candidate[-2])
    else:
        result = post(client, route(pid, "/preview"), candidate[-2])
    assert result.status_code == 409, result.json()


def test_idempotency_contract_and_cross_project_isolation(candidate):
    c, pid, _, app = candidate[:4]
    asset = preview(candidate)
    run = apply(candidate, asset, "same-patch-idempotency").json()
    assert (
        apply(candidate, asset, "same-patch-idempotency", reason="changed reason").json()["error"]["code"]
        == "IDEMPOTENCY_CONFLICT"
    )
    with app.state.sessions.begin() as s:
        s.add(Project(id="other-project", canonical_path=str(candidate[-1].parent / "other"), payload={}))
    assert c.get(PREFIX + route("other-project", "/assets/" + asset["id"])).status_code == 404
    assert (
        post(
            c,
            route("other-project", "/apply"),
            {
                "patch_id": asset["id"],
                "expected_hash": asset["content_hash"],
                "confirmed": True,
                "reason": "不能跨项目",
            },
        ).status_code
        == 404
    )
    assert (
        post(c, route("other-project", "/recover"), {"run_id": run["id"], "confirmed": True}).status_code
        == 404
    )


@pytest.mark.parametrize("mutation", ["duplicate", "extra", "too_many", "string_confirmation", "no_key"])
def test_strict_contract(candidate, mutation):
    c, pid = candidate[:2]
    body = copy.deepcopy(candidate[-2])
    if mutation == "duplicate":
        body["files"] *= 2
    elif mutation == "extra":
        body["can_apply"] = True
    elif mutation == "too_many":
        body["files"] *= 3
    elif mutation == "no_key":
        assert c.post(PREFIX + route(pid, "/preview"), json=body).status_code == 422
        return
    else:
        asset = preview(candidate)
        assert apply(candidate, asset, confirmed="true").status_code == 422
        return
    assert post(c, route(pid, "/preview"), body).status_code == 422


@pytest.mark.parametrize("crash_at", ["before_write", "after_replace"])
def test_actual_process_exit_then_startup_compensation(candidate, crash_at, tmp_path):
    asset = preview(candidate)
    c, pid, settings, _, provider = candidate[:5]
    # Separate OS process exits before its DB step commits. Journal is durable independently.
    script = tmp_path / "crash.py"
    script.write_text("""import os,sys,json
from pathlib import Path
from backend.app.core import Settings
from backend.app.db import connect
from backend.app import patches,planning
from backend.app.patch_files import Files
from backend.app.patch_schemas import Apply
v=json.loads(sys.argv[1]);settings=Settings(Path(v['data']),tuple(Path(p) for p in v['roots']),'test-only-local-token')
engine,sessions=connect(settings.data_dir)
planning.trusted_version=lambda name: '22.23.2' if name=='node' else '2.54.0'
real=Files.write
def crash(self,*args):
 if v['crash']=='before_write': os._exit(71)
 real(self,*args);os._exit(72)
Files.write=crash
patches.apply(sessions,settings,v['pid'],Apply(patch_id=v['asset'],expected_hash=v['hash'],confirmed=True,reason='process interruption test'),'crash-test-key')
""")
    args = {
        "data": str(settings.data_dir),
        "roots": [str(p) for p in settings.allowed_roots],
        "pid": pid,
        "asset": asset["id"],
        "hash": asset["content_hash"],
        "crash": crash_at,
    }
    result = subprocess.run(
        [sys.executable, str(script), json.dumps(args)],
        cwd=Path.cwd(),
        env={**os.environ, "PYTHONPATH": str(Path.cwd())},
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == (71 if crash_at == "before_write" else 72), result.stderr.decode()
    assert (candidate[-1] / "one.txt").read_text() == (
        "original first\n" if crash_at == "before_write" else "candidate first\n"
    )
    with TestClient(create_app(settings, provider), headers=dict(c.headers)) as reopened:
        stored = reopened.get(PREFIX + route(pid)).json()
        assert stored["runs"][0]["state"] == "rolled_back"
    assert (candidate[-1] / "one.txt").read_text() == "original first\n" and not (
        candidate[-1] / "two.txt"
    ).exists()
    assert (
        apply(candidate, asset, "crash-test-key", reason="process interruption test").json()["state"]
        == "rolled_back"
    )


def test_revoke_between_committed_steps_stops_and_restores(candidate, monkeypatch):
    from contextlib import contextmanager

    asset = preview(candidate)
    real = patches.locked
    revoked = False

    @contextmanager
    def after_commit(sessions):
        nonlocal revoked
        with real(sessions) as session:
            yield session
        with sessions() as session:
            run = next(iter(patches.runs(session, candidate[1])), None)
            should_revoke = run and run.state == "applying" and run.details["completed"] == ["one.txt"]
        if should_revoke and not revoked:
            revoked = True
            with real(sessions) as session:
                patches.execution.append(
                    session,
                    candidate[1],
                    "T1",
                    "revoke",
                    candidate[5]["id"],
                    {"authorization_id": candidate[7]["id"], "reason": "撤销下一写步骤"},
                )

    monkeypatch.setattr(patches, "locked", after_commit)
    result = apply(candidate, asset).json()
    assert revoked and result["state"] == "rolled_back"
    assert result["details"]["completed"] == ["one.txt"]
    assert (candidate[-1] / "one.txt").read_text() == "original first\n"
    assert not (candidate[-1] / "two.txt").exists()


def test_recovery_rejects_new_nested_project(candidate):
    asset = preview(candidate)
    root = candidate[-1]
    (root / "one.txt").write_text("candidate first\n")
    with candidate[3].state.sessions.begin() as session:
        session.add(
            PatchRun(
                id="interrupted",
                project_id=candidate[1],
                patch_id=asset["id"],
                state="applying",
                details={
                    "message": "interrupted",
                    "completed": [],
                    "reason": "test",
                    "authorization_id": None,
                },
            )
        )
        session.add(Project(id="new-other-project", canonical_path=str(root / "one.txt"), payload={}))
    patches.recover_pending(candidate[3].state.sessions, candidate[2])
    with candidate[3].state.sessions() as session:
        assert session.get(PatchRun, "interrupted").state == "rollback_required"
    assert (root / "one.txt").read_text() == "candidate first\n"


def test_preview_replay_and_noop_or_unselected_file(candidate):
    c, pid = candidate[:2]
    asset = post(c, route(pid, "/preview"), candidate[-2], "preview-same-key").json()
    assert post(c, route(pid, "/preview"), candidate[-2], "preview-same-key").json() == asset
    for files in (
        [{"path": "one.txt", "content": "original first\n"}],
        [{"path": "not-selected.txt", "content": "unselected candidate"}],
    ):
        assert post(c, route(pid, "/preview"), {**candidate[-2], "files": files}).status_code in {400, 409}


def test_launcher_distinguishes_idle_and_listening_ports():
    import socket

    from scripts.run_local import available

    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen()
        assert not available(port)
    assert available(port)


def test_io_error_returns_safe_contract(candidate, monkeypatch):
    asset = preview(candidate)
    from contextlib import contextmanager

    @contextmanager
    def unavailable(*args):
        raise OSError("private filesystem diagnostic must not be returned")
        yield

    monkeypatch.setattr(patches, "workspace_lock", unavailable)
    response = apply(candidate, asset)
    assert response.status_code == 409 and response.json()["error"]["code"] == "FILE_UNAVAILABLE"
    assert "private filesystem" not in response.text
    assert (candidate[-1] / "one.txt").read_text() == "original first\n"


def test_new_empty_file_and_newline_only_diff(candidate):
    asset = preview(candidate, files=[{"path": "two.txt", "content": ""}])
    assert "新增空文件" in asset["payload"]["files"][0]["diff"]
    assert apply(candidate, asset).json()["state"] == "applied"
    assert (candidate[-1] / "two.txt").read_bytes() == b""
    # Another preview against unchanged selected originals exposes newline deletion explicitly.
    row = eh.save((*candidate[:6], candidate[6])).json()
    body = {
        **candidate[-2],
        "execution_id": row["id"],
        "expected_hash": row["content_hash"],
        "files": [{"path": "one.txt", "content": "original first"}],
    }
    response = post(candidate[0], route(candidate[1], "/preview"), body)
    assert response.status_code == 200, response.json()
    assert "No newline at end of file" in response.json()["payload"]["files"][0]["diff"]


def test_storage_failure_preserves_safe_error_contract(candidate, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    def unavailable(*args):
        raise SQLAlchemyError("private content must not appear in response")

    monkeypatch.setattr(patches, "runs", unavailable)
    result = candidate[0].get(PREFIX + route(candidate[1]))
    assert result.status_code == 503 and result.json()["error"]["code"] == "STORAGE_UNAVAILABLE"
    assert "private content" not in result.text


def test_workspace_alias_uses_the_same_inode_lock(candidate):
    asset = preview(candidate)
    root = candidate[-1]
    alias = root / ".." / root.name
    assert root.samefile(alias)
    with workspace_lock(candidate[2], alias):
        response = apply(candidate, asset)
        assert response.status_code == 409 and response.json()["error"]["code"] == "WORKSPACE_BUSY"
