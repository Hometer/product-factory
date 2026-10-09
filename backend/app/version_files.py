"""Safe local file snapshots and bounded read-only Git metadata. No write commands."""

import difflib
import hashlib
import os
import selectors
import stat
import struct
import subprocess
import time
import zlib
from pathlib import Path

from . import contexts, patch_files, patches
from .scanner import BusinessError, canonical, digest, extract

LIMIT = 512 * 1024


def image_valid(raw, suffix):
    """Validate PNG chunk structure/CRC or JPEG marker structure; no pixel bytes served."""
    if suffix == ".png":
        if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return False
        offset, seen_header, seen_data = 8, False, False
        while offset + 12 <= len(raw):
            length = int.from_bytes(raw[offset : offset + 4], "big")
            kind = raw[offset + 4 : offset + 8]
            end = offset + 12 + length
            if end > len(raw) or zlib.crc32(raw[offset + 4 : end - 4]) != int.from_bytes(
                raw[end - 4 : end], "big"
            ):
                return False
            payload = raw[offset + 8 : end - 4]
            if not seen_header:
                if kind != b"IHDR" or length != 13 or not all(struct.unpack(">II", payload[:8])):
                    return False
                seen_header = True
            if kind == b"IDAT":
                seen_data = True
            if kind == b"IEND":
                return length == 0 and seen_data and end == len(raw)
            offset = end
        return False
    if not raw.startswith(b"\xff\xd8") or not raw.endswith(b"\xff\xd9"):
        return False
    offset, frame = 2, False
    while offset < len(raw) - 2:
        if raw[offset] != 255:
            return False
        while offset < len(raw) and raw[offset] == 255:
            offset += 1
        if offset >= len(raw):
            return False
        marker = raw[offset]
        offset += 1
        if marker in {0, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            return False
        if offset + 2 > len(raw):
            return False
        length = int.from_bytes(raw[offset : offset + 2], "big")
        if length < 2 or offset + length > len(raw):
            return False
        if marker in {0xC0, 0xC1, 0xC2}:
            if length < 8 or not all(struct.unpack(">HH", raw[offset + 3 : offset + 7])):
                return False
            frame = True
        if marker == 0xDA:
            return frame and offset + length < len(raw) - 2
        offset += length
    return False


def file_source(session, settings, pid, root, name, snapshot=False):
    rel = Path(name)
    if (
        not rel.parts
        or rel.is_absolute()
        or name != rel.as_posix()
        or "\\" in name
        or len(name) > 240
        or any(
            p.startswith(".")
            or contexts.private_name(p)
            or p.casefold() in {x.casefold() for x in patch_files.EXCLUDED | patch_files.PROTECTED}
            for p in rel.parts
        )
    ):
        raise BusinessError("PATH_FORBIDDEN", "资料路径受保护或不安全", 409)
    patches.target_scope(session, pid, root, [name])
    if snapshot:
        patch_files.name_check(name)
    try:
        raw = contexts.safe_file(root, name)
        suffix = rel.suffix.lower()
        mime = "text/plain"
        if suffix in {".png", ".jpg", ".jpeg"} and not snapshot:
            if not image_valid(raw, suffix):
                raise ValueError("signature/structure")
            mime = "image/png" if suffix == ".png" else "image/jpeg"
            content = None
        elif suffix in {".docx", ".pdf"} and not snapshot:
            status, content = extract(raw, suffix)
            if status != "parsed":
                raise ValueError("not safe parsed")
        elif suffix in patch_files.EXTENSIONS:
            content = raw.decode("utf-8")
            patch_files.text_check(content)
        else:
            raise ValueError("unsupported")
        if content is not None and not contexts.safe(content, settings):
            raise ValueError("sensitive")
        return dict(
            id="file:" + name,
            kind="file",
            title=name,
            locator=name,
            sha256=hashlib.sha256(raw).hexdigest(),
            size=len(raw),
            mime=mime,
            task_id=None,
            current=True,
        ), content
    except (OSError, ValueError, UnicodeError):
        raise BusinessError(
            "SOURCE_UNAVAILABLE", "文件已移走、变更或不可安全读取，请核对后重新定位", 409
        ) from None


def selected(session, settings, pid, names):
    from . import requirements

    root = canonical(requirements.project(session, pid).canonical_path, settings.allowed_roots)
    result = []
    for name in names:
        source, content = file_source(session, settings, pid, root, name, True)
        result.append(dict(path=name, sha256=source["sha256"], content=content))
    if sum(len(r["content"].encode()) for r in result) > LIMIT:
        raise BusinessError("SNAPSHOT_LIMIT", "代码快照合计不能超过 512 KiB", 409)
    return result


def bounded(argv, cwd):
    env = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
    }
    with subprocess.Popen(
        argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    ) as process:
        assert process.stdout is not None
        data = bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + 3
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise ValueError("timeout")
                    for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                        chunk = os.read(key.fd, 8192)
                        if not chunk:
                            selector.unregister(key.fd)
                        else:
                            data.extend(chunk)
                            if len(data) > 131072:
                                raise ValueError("limit")
            if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
                raise ValueError("git failed")
            return bytes(data)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def git_view(root, names, settings):
    unknown = dict(
        status="unknown",
        head=None,
        changes=[],
        scope=names,
        notice="Git 不可安全读取；不表示工作区干净，文件快照仍独立保存。",
    )
    try:
        directory = root / ".git"
        if not directory.is_dir() or directory.is_symlink():
            return unknown
        # Refuse alternate/worktree/linked object layouts and symlinks in metadata.
        count = 0
        for current, dirs, files in os.walk(directory, followlinks=False):
            for name in dirs + files:
                count += 1
                p = Path(current) / name
                if count > 5000 or p.is_symlink() or name in {"commondir", "alternates", "http-alternates"}:
                    return unknown
                if p.is_file() and (not stat.S_ISREG(p.stat().st_mode) or p.stat().st_nlink != 1):
                    return unknown
        binary = next((p for p in ("/usr/bin/git", "/bin/git") if Path(p).is_file()), None)
        if not binary or not names or len(names) > 500:
            return unknown
        args = [
            binary,
            "--literal-pathspecs",
            "--git-dir=" + str(directory),
            "--work-tree=" + str(root),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.untrackedCache=false",
            "-c",
            "core.preloadIndex=false",
        ]
        head = bounded(args + ["rev-parse", "--verify", "HEAD"], root).decode("ascii").strip()
        if len(head) not in {40, 64} or any(c not in "0123456789abcdef" for c in head):
            return unknown
        # Git status/diff can invoke clean filters; inspect raw index/tree instead.
        indexed = bounded(args + ["ls-files", "--stage", "-z", "--", *names], root)
        tree = bounded(args + ["ls-tree", "-rz", "HEAD", "--", *names], root)

        def parse(raw, is_index):
            records = {}
            for record in raw.split(b"\0"):
                if not record:
                    continue
                metadata, path = record.split(b"\t", 1)
                name = path.decode("utf-8")
                bits = metadata.decode("ascii").split()
                if name not in names or not contexts.safe(name, settings):
                    raise ValueError("outside scope")
                if is_index and bits[2] != "0":
                    raise ValueError("unmerged index")
                records[name] = (bits[0], bits[1] if is_index else bits[2])
            return records

        staged, committed = parse(indexed, True), parse(tree, False)
        changes: list[dict] = []
        diff_bytes = 0
        deadline = time.monotonic() + 6
        for name in names:
            before, stage = committed.get(name), staged.get(name)
            try:
                content = contexts.safe_file(root, name)
                hash_fn = hashlib.sha1 if len(head) == 40 else hashlib.sha256
                blob = hash_fn(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
            except FileNotFoundError:
                blob = None
                content = b""
            if not before and not stage:
                status = "??" if blob else "  "
            else:
                left = " " if before == stage else "A" if not before else "D" if not stage else "M"
                right = "D" if blob is None else " " if stage and blob == stage[1] else "M"
                status = left + right
            if status != "  ":
                difference = "未加载文本差异（范围或安全限制）；文件状态仍单独记录。"
                if len(changes) < 20 and len(content) <= patch_files.MAX_TEXT and time.monotonic() < deadline:
                    old = b""
                    try:
                        if before:
                            size = int(bounded(args + ["cat-file", "-s", before[1]], root))
                            if size > patch_files.MAX_TEXT or size < 0:
                                raise ValueError("oversized")
                            old = bounded(args + ["cat-file", "blob", before[1]], root)
                        left, right = old.decode("utf-8"), content.decode("utf-8")
                        patch_files.text_check(left)
                        patch_files.text_check(right)
                        if not contexts.safe(left + right, settings):
                            raise ValueError("private")
                        if diff_bytes + len(old) + len(content) > LIMIT:
                            raise ValueError("diff limit")
                        difference = "".join(
                            difflib.unified_diff(
                                left.splitlines(True),
                                right.splitlines(True),
                                fromfile="HEAD/" + name,
                                tofile="工作区/" + name,
                            )
                        )[:131072]
                        diff_bytes += len(old) + len(content)
                    except (BusinessError, OSError, ValueError, UnicodeError, subprocess.SubprocessError):
                        pass
                changes.append(dict(path=name, status=status, diff=difference))
        return dict(
            status="recorded",
            head=head,
            changes=changes,
            scope=names,
            notice="仅所列安全文件的原始字节 Git 状态（不运行过滤器）；前20个变化安全文本显示HEAD差异，合计512KiB。其余和受保护文件不检查，不表示整个仓库干净。",
        )
    except (OSError, ValueError, subprocess.SubprocessError, UnicodeError):
        return unknown


def index(session, settings, pid):
    from . import requirements

    value, sources = contexts.collect(session, settings, pid, "planning", None)
    root = canonical(requirements.project(session, pid).canonical_path, settings.allowed_roots)
    safe_names = []
    for identifier, item in sources.items():
        if identifier.startswith("file:") and item["source"]["status"] == "ready":
            try:
                patch_files.name_check(identifier[5:])
                safe_names.append(identifier[5:])
            except BusinessError:
                pass
    from sqlalchemy import select

    from .db import RequirementRecord

    for version in session.scalars(
        select(RequirementRecord).where(
            RequirementRecord.project_id == pid, RequirementRecord.kind == "code_version"
        )
    ):
        for item in version.payload["files"]:
            patch_files.name_check(item["path"])
            if item["path"] not in safe_names:
                safe_names.append(item["path"])
    git = git_view(root, sorted(safe_names), settings)
    return value, sources, git, digest(dict(context=value["index_hash"], git=git))
