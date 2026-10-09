import concurrent.futures
import copy
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.app import backups, requirements
from backend.app.db import (
    CommandAsset,
    CommandRun,
    ModelBudget,
    PatchAsset,
    PatchRun,
    Project,
    RequirementRecord,
)
from backend.app.main import PREFIX, create_app
from backend.app.scanner import BusinessError, digest
from backend.tests import test_commands as ch
from backend.tests.test_requirements import post

setup = ch.rh.setup

prepared = ch.prepared
ready = ch.ready
executable = ch.executable


def route(pid, suffix=""):
    return f"/projects/{pid}/backups{suffix}"


def state(c, pid):
    r = c.get(PREFIX + route(pid))
    assert r.status_code == 200, r.json()
    return r.json()


def export(c, pid, code=False, key=None):
    return post(
        c,
        route(pid, "/exports"),
        dict(
            expected_state_hash=state(c, pid)["state_hash"],
            include_code=code,
            confirmed=True,
            reason="隔离备份核对",
        ),
        key,
    )


def package(c, pid, code=False):
    r = export(c, pid, code)
    assert r.status_code == 200, r.json()
    a = r.json()
    d = c.get(PREFIX + route(pid, f"/exports/{a['id']}/download"))
    assert d.status_code == 200
    return a, d.json()


def inspect(c, pid, value, target, key=None):
    return post(
        c,
        route(pid, "/previews"),
        dict(
            filename="sample.pfbackup.json",
            content=json.dumps(value, ensure_ascii=False),
            target_path=str(target),
            confirmed=True,
            reason="预检新目录完整性",
        ),
        key,
    )


def archive(c, pid, value=True, key=None):
    return post(
        c,
        route(pid, "/archives"),
        dict(
            expected_state_hash=state(c, pid)["state_hash"],
            archived=value,
            confirmed=True,
            reason="仅归档可见性核对",
        ),
        key,
    )


def rehash(value):
    value["sha256"] = digest(value["snapshot"])
    return value


def restore(c, pid, a, key=None):
    return post(
        c,
        route(pid, "/restores"),
        dict(
            asset_id=a["id"], expected_hash=a["content_hash"], confirmed=True, reason="明确恢复全新项目身份"
        ),
        key,
    )


def root_of(c, pid):
    return Path(c.get(f"{PREFIX}/projects/{pid}").json()["path"])


def test_roundtrip_new_identity_inert_history_and_zero_billing(setup):
    c, pid, settings, app, provider = setup
    root = root_of(c, pid)
    (root / "app.py").write_text("print('safe')\n")
    c.get(PREFIX + "/model/status")
    with requirements.locked(app.state.sessions) as s:
        requirements.record(s, pid, "run", dict(state="result_unknown", cost_micro=2300))
        requirements.record(s, pid, "authorization", dict(status="active", approved=True))
        b = s.scalar(select(ModelBudget))
        b.spent_micro = 2300
        b.requests_used = 2
    _, value = package(c, pid, True)
    target = root.parent / "recovered"
    p = inspect(c, pid, value, target).json()
    assert p["payload"]["replaced"] == [] and not target.exists()
    r = restore(c, pid, p).json()
    new_id = r["payload"]["restored_project_id"]
    assert new_id != pid
    assert (target / "app.py").read_bytes() == (root / "app.py").read_bytes()
    source = c.get(PREFIX + route(new_id, "/source")).json()
    assert source == value["snapshot"]
    with app.state.sessions() as s:
        rows = list(s.scalars(select(RequirementRecord).where(RequirementRecord.project_id == new_id)))
        assert {r.kind for r in rows} == {"template_binding", "restore_source"}
        b = s.scalar(select(ModelBudget))
        assert (b.spent_micro, b.reserved_micro, b.requests_used, b.requests_reserved) == (2300, 0, 2, 0)
    assert not provider.calls
    assert state(c, new_id)["source"]["historical_only"]
    assert all(isinstance(f, dict) and "path" in f for f in state(c, new_id)["source"]["files"])


def test_default_code_references_and_binary_large_secrets_omitted(setup):
    c, pid, settings, app, _ = setup
    root = root_of(c, pid)
    (root / "safe.py").write_text("print(123)\n")
    (root / "report.md").write_text("合成证据报告\n")
    (root / "large.txt").write_bytes(b"a" * (512 * 1024 + 1))
    (root / "evidence.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (root / ".env").write_text("password=do-not-export-12345")
    (root / "private.md").write_text("password=do-not-export-12345")
    _, v = package(c, pid)
    files = {f["path"]: f for f in v["snapshot"]["files"]}
    assert files["report.md"]["included"]
    assert not files["safe.py"]["included"] and files["safe.py"]["sha256"]
    assert not files["large.txt"]["included"] and not files["evidence.png"]["included"]
    assert not files["private.md"]["included"] and ".env" not in files
    assert "do-not-export" not in json.dumps(v)


@pytest.mark.parametrize(
    "field,value", [("version", 2), ("database_version", "9999"), ("format", "another-backup")]
)
def test_incompatible_versions(setup, field, value):
    c, pid, *_ = setup
    _, v = package(c, pid)
    v["snapshot"][field] = value
    r = inspect(c, pid, rehash(v), root_of(c, pid).parent / "restore")
    assert r.status_code == 409 and r.json()["error"]["code"] == "BACKUP_VERSION"


@pytest.mark.parametrize(
    "change",
    [
        "envelope",
        "file_hash",
        "file_size",
        "missing_content",
        "excluded_content",
        "unknown_table",
        "foreign_row",
        "duplicate_row",
    ],
)
def test_corruption_and_untrusted_history(setup, change):
    c, pid, *_ = setup
    _, v = package(c, pid, True)
    s = v["snapshot"]
    f = next(f for f in s["files"] if f["included"])
    if change == "envelope":
        v["sha256"] = "0" * 64
    elif change == "file_hash":
        f["sha256"] = "0" * 64
        rehash(v)
    elif change == "file_size":
        f["size"] += 1
        rehash(v)
    elif change == "missing_content":
        f["content"] = None
        rehash(v)
    elif change == "excluded_content":
        f["included"] = False
        rehash(v)
    elif change == "unknown_table":
        s["tables"]["evil"] = []
        rehash(v)
    elif change == "foreign_row":
        s["tables"]["requirement_records"][0]["project_id"] = str(uuid4())
        rehash(v)
    else:
        s["tables"]["requirement_records"].append(copy.deepcopy(s["tables"]["requirement_records"][0]))
        rehash(v)
    r = inspect(c, pid, v, root_of(c, pid).parent / "restore")
    assert r.status_code == 409


@pytest.mark.parametrize(
    "name", ["../evil.txt", "/tmp/evil.txt", "a\\b.txt", ".env", "data/key.txt", "api_key.txt", "a/../b.md"]
)
def test_unsafe_restore_paths(setup, name):
    c, pid, *_ = setup
    _, v = package(c, pid)
    v["snapshot"]["files"][0]["path"] = name
    assert inspect(c, pid, rehash(v), root_of(c, pid).parent / "restore").status_code == 409


@pytest.mark.parametrize("kind", ["existing", "nested", "outside", "missing_parent", "linked", "registered"])
def test_directory_conflicts_preserve_original(setup, kind):
    c, pid, settings, *_ = setup
    root = root_of(c, pid)
    _, v = package(c, pid)
    target = root.parent / "new"
    if kind == "existing":
        target.mkdir()
        (target / "keep.txt").write_text("keep")
    elif kind == "nested":
        target = root / "child"
    elif kind == "outside":
        target = root.parent.parent / "outside"
    elif kind == "missing_parent":
        target = root.parent / "missing" / "new"
    elif kind == "linked":
        target.symlink_to(root, target_is_directory=True)
    else:
        target = root
    before = (root / "README.md").read_bytes()
    assert inspect(c, pid, v, target).status_code in (400, 409)
    assert (root / "README.md").read_bytes() == before
    if kind == "existing":
        assert (target / "keep.txt").read_text() == "keep"


def test_restore_rechecks_target_after_preview(setup):
    c, pid, *_ = setup
    _, v = package(c, pid)
    target = root_of(c, pid).parent / "new"
    p = inspect(c, pid, v, target).json()
    target.mkdir()
    (target / "keep.txt").write_text("outside update")
    assert restore(c, pid, p).status_code == 409
    assert (target / "keep.txt").read_text() == "outside update"


def test_partial_write_failure_cleans_only_new_files(setup, monkeypatch):
    c, pid, *_ = setup
    _, v = package(c, pid)
    root = root_of(c, pid)
    target = root.parent / "new"
    p = inspect(c, pid, v, target).json()
    original = (root / "README.md").read_bytes()
    link = backups.os.link
    count = 0

    def fail(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("synthetic failure")
        return link(*args, **kwargs)

    monkeypatch.setattr(backups.os, "link", fail)
    assert restore(c, pid, p).status_code == 409
    assert not target.exists() and (root / "README.md").read_bytes() == original
    with setup[3].state.sessions() as s:
        assert s.scalar(select(Project).where(Project.canonical_path == str(target))) is None


def test_restore_and_export_idempotence_and_cross_project_guard(setup):
    c, pid, *_ = setup
    _, v = package(c, pid)
    target = root_of(c, pid).parent / "new"
    p = inspect(c, pid, v, target).json()
    key = str(uuid4())
    r = restore(c, pid, p, key)
    assert r.status_code == 200
    assert restore(c, pid, p, key).json() == r.json()
    assert restore(c, pid, p).status_code == 409
    new_id = r.json()["payload"]["restored_project_id"]
    assert c.get(PREFIX + route(new_id, f"/exports/{p['id']}/download")).status_code == 404
    assert restore(c, new_id, p).status_code == 404


def test_archive_filters_blocks_business_writes_and_unarchive_no_replay(setup):
    c, pid, _, app, provider = setup
    root = root_of(c, pid)
    before = (root / "README.md").read_bytes()
    assert archive(c, pid).status_code == 200
    assert state(c, pid)["archived"]
    assert not c.get(PREFIX + "/projects").json()["items"]
    assert c.get(PREFIX + "/projects?archived=true").json()["items"][0]["id"] == pid
    assert c.get(PREFIX + f"/projects/{pid}").json()["archived"]
    with pytest.raises(BusinessError) as e:
        requirements.mutate(app.state.sessions, pid, "new_task", {}, str(uuid4()), lambda s: {"ran": True})
    assert e.value.code == "PROJECT_ARCHIVED"
    assert export(c, pid).status_code == 200
    assert archive(c, pid, False).status_code == 200
    assert c.get(PREFIX + "/projects").json()["items"][0]["id"] == pid
    assert (root / "README.md").read_bytes() == before and not provider.calls


@pytest.mark.parametrize("status", ["queued", "running", "result_unknown"])
def test_model_active_or_unknown_archive_blocked(setup, status):
    c, pid, _, app, _ = setup
    with requirements.locked(app.state.sessions) as s:
        requirements.record(s, pid, "run", dict(state=status))
    r = archive(c, pid)
    assert r.status_code == 409 and r.json()["error"]["code"] == "ARCHIVE_BUSY"
    assert not state(c, pid)["archived"]


def test_stale_sources_conflict_and_historical_download_immutable(setup):
    c, pid, *_ = setup
    h = state(c, pid)["state_hash"]
    a, v = package(c, pid)
    (root_of(c, pid) / "README.md").write_text("外部新文本\n")
    for suffix, extra in [("/exports", dict(include_code=False)), ("/archives", dict(archived=True))]:
        r = post(
            c,
            route(pid, suffix),
            dict(expected_state_hash=h, confirmed=True, reason="保留旧输入核对", **extra),
        )
        assert r.status_code == 409 and r.json()["error"]["code"] == "REVISION_CONFLICT"
    assert c.get(PREFIX + route(pid, f"/exports/{a['id']}/download")).json() == v


@pytest.mark.parametrize(
    "fault",
    [
        "invalid_json",
        "filename",
        "false_confirm",
        "secret",
        "oversize",
        "extra",
        "duplicate_json",
        "empty_rows",
    ],
)
def test_upload_and_confirmation_validation(setup, fault):
    c, pid, *_ = setup
    _, v = package(c, pid)
    body = dict(
        filename="backup.pfbackup.json",
        content=json.dumps(v),
        target_path=str(root_of(c, pid).parent / "new"),
        confirmed=True,
        reason="隔离检查确认",
    )
    if fault == "invalid_json":
        body["content"] = "not json"
    elif fault == "filename":
        body["filename"] = "../a.pfbackup.json"
    elif fault == "false_confirm":
        body["confirmed"] = False
    elif fault == "secret":
        v["snapshot"]["project"]["description"] = "password=do-not-export-12345"
        body["content"] = json.dumps(rehash(v))
    elif fault == "oversize":
        body["content"] = "x" * (8 * 1024 * 1024 + 1)
    elif fault == "extra":
        body["extra"] = True
    elif fault == "duplicate_json":
        body["content"] = '{"snapshot":{},"snapshot":{}}'
    else:
        v["snapshot"]["tables"]["requirement_records"][0] = {}
        body["content"] = json.dumps(rehash(v))
    assert post(c, route(pid, "/previews"), body).status_code in (409, 413, 422)


def test_concurrent_same_archive_request_one_record(setup):
    c, pid, _, app, _ = setup
    h = state(c, pid)["state_hash"]
    key = str(uuid4())
    body = dict(expected_state_hash=h, archived=True, confirmed=True, reason="并发相同确认请求")
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: post(c, route(pid, "/archives"), body, key), range(2)))
    assert all(r.status_code == 200 for r in results)
    assert results[0].json() == results[1].json()
    with app.state.sessions() as s:
        assert (
            len(list(s.scalars(select(RequirementRecord).where(RequirementRecord.kind == "project_archive"))))
            == 1
        )


def test_restart_preserves_archived_and_source(setup):
    c, pid, settings, app, _ = setup
    _, v = package(c, pid)
    p = inspect(c, pid, v, root_of(c, pid).parent / "new").json()
    r = restore(c, pid, p).json()
    new_id = r["payload"]["restored_project_id"]
    archive(c, pid)
    reopened = create_app(settings)
    with reopened.state.sessions() as s:
        assert backups.archived(s, pid)
        assert backups.latest(s, new_id, "restore_source").payload["snapshot"] == v["snapshot"]


@pytest.mark.parametrize("status", ["queued", "starting", "running", "serving", "interrupted", "paused"])
def test_command_active_pause_unknown_archive_blocked(executable, status):
    c, pid, _, app, *_ = executable
    row = executable[6]
    aid = str(uuid4())
    with requirements.locked(app.state.sessions) as s:
        s.add(CommandAsset(id=aid, project_id=pid, execution_id=row["id"], payload={}))
        s.flush()
        s.add(CommandRun(id=str(uuid4()), project_id=pid, asset_id=aid, state=status, details={}))
    r = archive(c, pid)
    assert r.status_code == 409 and r.json()["error"]["code"] == "ARCHIVE_BUSY"


@pytest.mark.parametrize(
    "status", ["preparing", "applying", "rollback_required", "result_unknown", "interrupted"]
)
def test_patch_active_unknown_archive_blocked(executable, status):
    c, pid, _, app, *_ = executable
    aid = str(uuid4())
    row = executable[6]
    with requirements.locked(app.state.sessions) as s:
        s.add(PatchAsset(id=aid, project_id=pid, execution_id=row["id"], payload={}))
        s.flush()
        s.add(PatchRun(id=str(uuid4()), project_id=pid, patch_id=aid, state=status, details={}))
    r = archive(c, pid)
    assert r.status_code == 409 and r.json()["error"]["code"] == "ARCHIVE_BUSY"


def test_backup_request_stream_limit(setup):
    c, pid, *_ = setup
    r = c.post(
        PREFIX + route(pid, "/previews"),
        content=b"x" * (12 * 1024 * 1024 + 1),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 413 and r.json()["error"]["code"] == "INPUT_INVALID"


def test_sensitive_business_record_exclusion(setup):
    c, pid, _, app, _ = setup
    with requirements.locked(app.state.sessions) as s:
        requirements.record(s, pid, "synthetic_private", dict(password="not-exported-12345"))
    a, v = package(c, pid)
    assert "not-exported" not in json.dumps(v) and a["payload"]["exclusions"]


def test_rollback_keeps_concurrently_changed_new_file(setup, monkeypatch):
    c, pid, *_ = setup
    _, v = package(c, pid)
    target = root_of(c, pid).parent / "new"
    p = inspect(c, pid, v, target).json()
    link = backups.os.link
    count = 0

    def fail(src, *args, **kw):
        nonlocal count
        count += 1
        if count == 2:
            first = next(f["path"] for f in v["snapshot"]["files"] if f["included"])
            (target / first).write_text("external change")
            raise OSError("synthetic failure")
        return link(src, *args, **kw)

    monkeypatch.setattr(backups.os, "link", fail)
    assert restore(c, pid, p).status_code == 409
    assert any(f.read_text() == "external change" for f in target.rglob("*") if f.is_file())


def test_reexport_restored_project_keeps_original_source_snapshot(setup):
    c, pid, *_ = setup
    _, v = package(c, pid)
    p = inspect(c, pid, v, root_of(c, pid).parent / "new").json()
    new_id = restore(c, pid, p).json()["payload"]["restored_project_id"]
    _, new = package(c, new_id)
    sources = [r for r in new["snapshot"]["tables"]["requirement_records"] if r["kind"] == "restore_source"]
    assert sources[0]["payload"]["snapshot"] == v["snapshot"]


def test_factory_managed_container_exception_does_not_allow_business_nesting(setup, monkeypatch):
    c, pid, settings, app, _ = setup
    root = root_of(c, pid)
    container = root / "state"
    container.mkdir()
    monkeypatch.setattr(backups, "ROOT", root)
    configured = replace(settings, data_dir=container)
    with app.state.sessions() as s:
        assert backups.target_check(s, configured, str(container / "new")) == container / "new"
        with pytest.raises(BusinessError):
            backups.target_check(s, configured, str(root / "business-child"))
        with pytest.raises(BusinessError):
            backups.target_check(s, configured, str(container))


def test_restore_database_failure_rolls_back_new_identity_and_files(setup, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    c, pid, _, app, _ = setup
    _, v = package(c, pid)
    target = root_of(c, pid).parent / "new"
    p = inspect(c, pid, v, target).json()
    audit = requirements.audit

    def fail(s, identifier, action, details):
        if action == "backup_restore":
            raise SQLAlchemyError("synthetic database failure")
        return audit(s, identifier, action, details)

    monkeypatch.setattr(requirements, "audit", fail)
    assert restore(c, pid, p).status_code == 503
    assert not target.exists()
    with app.state.sessions() as s:
        assert s.scalar(select(Project).where(Project.canonical_path == str(target))) is None


def test_failed_restore_does_not_clean_through_replaced_root_link(setup, monkeypatch):
    c, pid, *_ = setup
    root = root_of(c, pid)
    original = {str(f.relative_to(root)): f.read_bytes() for f in root.rglob("*") if f.is_file()}
    _, value = package(c, pid)
    target = root.parent / "new"
    moved = root.parent / "externally-moved"
    preview = inspect(c, pid, value, target).json()
    link = backups.os.link
    count = 0

    def fail(src, *args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            target.rename(moved)
            target.symlink_to(root, target_is_directory=True)
            raise OSError("synthetic concurrent root replacement")
        return link(src, *args, **kwargs)

    monkeypatch.setattr(backups.os, "link", fail)
    assert restore(c, pid, preview).status_code == 409
    assert target.is_symlink() and moved.exists()
    assert {str(f.relative_to(root)): f.read_bytes() for f in root.rglob("*") if f.is_file()} == original
