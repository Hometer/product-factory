import concurrent.futures
import os
import sqlite3
from uuid import uuid4

import pytest
from docx import Document
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from sqlalchemy import select

from backend.app.core import Settings
from backend.app.db import AuditEvent
from backend.app.main import PREFIX, create_app


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "授权目录"
    root.mkdir()
    settings = Settings(
        tmp_path / "state",
        (root,),
        "test-only-placeholder-credential",
        hosts=("testserver", "127.0.0.1:8765"),
    )
    app = create_app(settings)
    with TestClient(
        app, headers={"Authorization": f"Bearer {settings.token}", "Origin": "http://127.0.0.1:3000"}
    ) as client:
        yield client, root, settings, app


def payload(path):
    return {
        "name": "示例产品",
        "description": "",
        "target_user": "产品负责人",
        "core_task": "管理研发资料",
        "path": str(path),
    }


def preview(client, path, kind="import"):
    response = client.post(f"{PREFIX}/projects/{kind}-preview", json=payload(path))
    assert response.status_code == 200, response.json()
    return response.json()


def confirm(client, item, key=None):
    return client.post(
        f"{PREFIX}/projects/confirm",
        headers={"Idempotency-Key": key or str(uuid4())},
        json={"preview_id": item["preview_id"], "expected_hash": item["content_hash"]},
    )


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"Authorization": ""}, 401),
        ({"Origin": "https://other.invalid"}, 403),
        ({"Host": "attacker.invalid"}, 403),
    ],
)
def test_security(setup, headers, status):
    client, *_ = setup
    result = client.get(f"{PREFIX}/projects", headers=headers)
    assert result.status_code == status
    assert set(result.json()) == {"error"}


def test_mutation_requires_origin(setup):
    client, root, *_ = setup
    client.headers.pop("origin")
    assert client.post(f"{PREFIX}/projects/import-preview", json=payload(root)).status_code == 403


def test_validation_does_not_echo_input(setup):
    client, root, *_ = setup
    body = payload(root) | {"authorized": "confidential-value"}
    response = client.post(f"{PREFIX}/projects/import-preview", json=body)
    assert response.status_code == 422
    assert "confidential-value" not in response.text


@pytest.mark.parametrize("change", [{"name": " "}, {"path": "relative"}, {"template_version": "other"}])
def test_invalid_fields(setup, change):
    client, root, *_ = setup
    result = client.post(f"{PREFIX}/projects/import-preview", json=payload(root) | change)
    assert result.status_code in {400, 422}


def test_create_preview_and_confirm_are_safe(setup):
    client, root, _, app = setup
    path = root / "中文 空格项目"
    item = preview(client, path, "create")
    assert not path.exists()
    assert "README.md" in item["planned_files"]
    response = confirm(client, item)
    assert response.status_code == 200
    assert (path / "README.md").is_file()
    assert (path / "docs/PRD/PRD模板.md").is_file()
    assert response.json()["status"] == "registered"
    with app.state.sessions() as session:
        assert session.scalar(select(AuditEvent)).action == "project_created"


def test_create_does_not_overwrite(setup):
    client, root, *_ = setup
    (root / "README.md").write_text("original")
    result = client.post(f"{PREFIX}/projects/create-preview", json=payload(root))
    assert result.status_code == 409
    assert (root / "README.md").read_text() == "original"


def test_directory_appearing_after_preview_is_preserved(setup):
    client, root, *_ = setup
    path = root / "new"
    item = preview(client, path, "create")
    path.mkdir()
    (path / "user.txt").write_text("user data")
    assert confirm(client, item).status_code == 409
    assert (path / "user.txt").read_text() == "user data"


def test_import_is_read_only_and_excludes_secrets(setup):
    client, root, *_ = setup
    (root / "README.md").write_text("hello")
    (root / ".env").write_text("never index")
    (root / "private.pem").write_text("never index")
    (root / "node_modules").mkdir()
    (root / "node_modules/hidden.md").write_text("dependency")
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    item = preview(client, root)
    assert [f["path"] for f in item["manifest"]["files"]] == ["README.md"]
    assert confirm(client, item).status_code == 200
    after = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert before == after


@pytest.mark.parametrize("attack", ["outside", "traversal", "symlink"])
def test_path_attacks(setup, tmp_path, attack):
    client, root, *_ = setup
    if attack == "outside":
        target = tmp_path
    elif attack == "traversal":
        target = root / ".." / "outside"
    else:
        target = root / "link"
        target.symlink_to(tmp_path, target_is_directory=True)
    result = client.post(f"{PREFIX}/projects/import-preview", json=payload(target))
    assert result.status_code == 400
    assert result.json()["error"]["code"] == "PATH_FORBIDDEN"


def test_symlink_documents_excluded(setup, tmp_path):
    client, root, *_ = setup
    outside = tmp_path / "outside.md"
    outside.write_text("must not be read")
    (root / "linked.md").symlink_to(outside)
    item = preview(client, root)
    assert item["manifest"]["files"] == []


def test_external_change_blocks_confirmation(setup):
    client, root, *_ = setup
    file = root / "README.md"
    file.write_text("before")
    item = preview(client, root)
    file.write_text("after")
    assert confirm(client, item).json()["error"]["code"] == "PREVIEW_CHANGED"
    assert client.get(f"{PREFIX}/projects").json()["total"] == 0
    assert file.read_text() == "after"


def test_idempotency_and_duplicate_path(setup):
    client, root, *_ = setup
    item = preview(client, root)
    first = confirm(client, item, "request-key-1")
    replay = confirm(client, item, "request-key-1")
    assert first.json() == replay.json()
    duplicate = confirm(client, preview(client, root))
    assert duplicate.json()["id"] == first.json()["id"]
    other = preview(client, root)
    assert confirm(client, other, "request-key-1").status_code == 409
    assert client.get(f"{PREFIX}/projects").json()["total"] == 1


def test_concurrent_confirmation_single_project(setup):
    client, root, *_ = setup
    item = preview(client, root / "parallel", "create")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: confirm(client, item, "parallel-request"), range(4)))
    assert all(r.status_code == 200 for r in results)
    assert len({r.json()["id"] for r in results}) == 1


def test_database_and_preview_survive_restart(setup):
    client, root, settings, _ = setup
    item = preview(client, root)
    app2 = create_app(settings)
    with TestClient(app2, headers=dict(client.headers)) as second:
        project = confirm(second, item).json()
        assert second.get(f"{PREFIX}/projects/{project['id']}").json() == project
    app3 = create_app(settings)
    with TestClient(app3, headers=dict(client.headers)) as third:
        assert third.get(f"{PREFIX}/projects").json()["items"][0]["id"] == project["id"]
    with sqlite3.connect(settings.data_dir / "factory.sqlite3") as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0001"


def test_formats_and_sensitive_content(setup):
    client, root, *_ = setup
    doc = Document()
    doc.add_paragraph("真实 DOCX 文档")
    doc.save(root / "valid.docx")
    (root / "fake.pdf").write_text("fake PDF")
    (root / "bad.docx").write_bytes(b"invalid")
    (root / "binary.txt").write_bytes(b"binary\0data")
    (root / "secret.md").write_text("-----BEGIN PRIVATE KEY-----")
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    with (root / "scan.pdf").open("wb") as file:
        writer.write(file)
    files = {f["path"]: f for f in preview(client, root)["manifest"]["files"]}
    assert files["valid.docx"]["status"] == "parsed"
    assert files["fake.pdf"]["status"] == "invalid_type"
    assert files["bad.docx"]["status"] == "parse_failed"
    assert files["binary.txt"]["status"] == "invalid_type"
    assert files["secret.md"]["status"] == "sensitive_excluded"
    assert files["scan.pdf"]["status"] == "needs_transcription"
    assert "真实 DOCX" not in str(files)


def test_scan_limits(setup, monkeypatch):
    client, root, *_ = setup
    (root / "a.md").write_text("hello")
    monkeypatch.setattr("backend.app.scanner.MAX_FILE", 4)
    response = client.post(f"{PREFIX}/projects/import-preview", json=payload(root))
    assert response.json()["error"]["code"] == "SCAN_LIMIT"


def test_batch_limit(setup, monkeypatch):
    client, root, *_ = setup
    (root / "a.md").write_text("hello")
    (root / "b.md").write_text("hello")
    monkeypatch.setattr("backend.app.scanner.MAX_BATCH", 9)
    assert (
        client.post(f"{PREFIX}/projects/import-preview", json=payload(root)).json()["error"]["code"]
        == "SCAN_LIMIT"
    )


def test_file_count_limit(setup, monkeypatch):
    client, root, *_ = setup
    (root / "a.md").write_text("a")
    (root / "b.md").write_text("b")
    monkeypatch.setattr("backend.app.scanner.MAX_FILES", 1)
    assert (
        client.post(f"{PREFIX}/projects/import-preview", json=payload(root)).json()["error"]["code"]
        == "SCAN_LIMIT"
    )


def test_preview_expiry(setup):
    from datetime import timedelta

    from backend.app.db import Preview

    client, root, _, app = setup
    item = preview(client, root)
    with app.state.sessions.begin() as session:
        stored = session.get(Preview, item["preview_id"])
        stored.created_at -= timedelta(days=2)
    assert confirm(client, item).json()["error"]["code"] == "PREVIEW_EXPIRED"


def test_unknown_project_has_standard_error(setup):
    client, *_ = setup
    result = client.get(f"{PREFIX}/projects/{uuid4()}")
    assert result.status_code == 404
    assert result.json()["error"]["code"] == "NOT_FOUND"


def test_failure_rolls_back_only_new_files(setup, monkeypatch):
    client, root, *_ = setup
    target = root / "rollback"
    item = preview(client, target, "create")
    original = os.link

    def fail(source, destination, *args, **kwargs):
        if destination == target / "AGENTS.md":
            raise OSError("simulated write failure")
        return original(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "link", fail)
    assert confirm(client, item).status_code == 503
    assert not target.exists()
    assert client.get(f"{PREFIX}/projects").json()["total"] == 0
