"""Small local text recipe; paths are checked through retained directory descriptors."""

import fcntl
import hashlib
import os
import re
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path
from uuid import uuid4

from . import execution
from .core import ROOT
from .scanner import EXCLUDED, BusinessError

MAX_TEXT = 65536
EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".css", ".html", ".md", ".txt", ".json"}
PROTECTED = {
    "package.json",
    "package-lock.json",
    "pyproject.toml",
    "requirements.txt",
    "AGENTS.md",
    "CLAUDE.md",
    "alembic.ini",
    "schema.prisma",
    "migrations",
    "alembic",
    "backups",
    "uploads",
    "fixtures",
    "datasets",
    "original",
    "originals",
    "raw",
    "原始数据",
    "venv",
}


def identity(metadata):
    return [metadata.st_dev, metadata.st_ino]


def name_check(name):
    rel = Path(name)
    protected_names = {p.casefold() for p in EXCLUDED | PROTECTED}
    if (
        not rel.parts
        or rel.is_absolute()
        or name != rel.as_posix()
        or "\\" in name
        or any(
            p.casefold() in protected_names
            or p in {".", ".."}
            or p.startswith(".")
            or re.search(r"(?i)(secret|credential|password|access.?key|api.?key|backend_token)", p)
            for p in rel.parts
        )
        or rel.suffix.lower() not in EXTENSIONS
    ):
        raise BusinessError("PATH_FORBIDDEN", "文件路径受保护或不属于本轮文本写入类型")
    return rel


def text_check(content):
    try:
        size = len(content.encode("utf-8"))
    except UnicodeEncodeError:
        raise BusinessError("TEXT_FORBIDDEN", "文本编码无效，不保存原文", 409) from None
    if (
        size > MAX_TEXT
        or "\x00" in content
        or any(ord(c) < 32 and c not in "\n\r\t" for c in content)
        or execution.contains_secret(content)
    ):
        raise BusinessError("TEXT_FORBIDDEN", "文件不是安全的限量文本或疑似包含秘密，不保存原文", 409)
    if re.search(r"(?i)\b(?:ALTER\s+TABLE|DROP\s+TABLE|CREATE\s+TABLE|DELETE\s+FROM)\b", content):
        raise BusinessError("MIGRATION_UNSUPPORTED", "数据库变更需另列迁移和数据备份方案，本配方不执行", 409)


def root_check(root, settings):
    if (
        ROOT == root
        or ROOT.is_relative_to(root)
        or settings.data_dir == root
        or settings.data_dir.is_relative_to(root)
    ):
        raise BusinessError("PATH_FORBIDDEN", "产品工厂自身工程与运行资料不能作为业务补丁目标", 409)


@contextmanager
def workspace_lock(settings, root):
    directory = settings.data_dir / "patch-locks"
    directory.mkdir(mode=0o700, exist_ok=True)
    # Canonical spelling alone is insufficient on macOS's usual case-insensitive filesystem.
    root_stat = root.stat()
    key = f"{root_stat.st_dev}:{root_stat.st_ino}"
    file = directory / (hashlib.sha256(key.encode()).hexdigest() + ".lock")
    fd = os.open(file, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BusinessError(
                "WORKSPACE_BUSY", "本项目有文件操作正在进行，请稍后查询运行结果", 409
            ) from None
        yield
    finally:
        os.close(fd)


class Files:
    def __init__(self, root, names):
        self.root, self.names = Path(root), names
        self.stack = ExitStack()
        self.parents = {}

    def __enter__(self):
        try:
            self.root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.stack.callback(os.close, self.root_fd)
            self.root_identity = identity(os.fstat(self.root_fd))
            for name in self.names:
                rel = name_check(name)
                fd = self.root_fd
                for part in rel.parts[:-1]:
                    fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    self.stack.callback(os.close, fd)
                self.parents[name] = fd
            self.verify()
            return self
        except (OSError, BusinessError):
            self.stack.close()
            raise BusinessError("FILE_UNAVAILABLE", "文件目录缺失、被替换或含链接，已停止操作", 409) from None

    def __exit__(self, *args):
        self.stack.close()

    def verify(self):
        # Re-open the directory chain without following links; retained fds cannot authorize a moved directory.
        with ExitStack() as check:
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            check.callback(os.close, fd)
            if identity(os.fstat(fd)) != self.root_identity:
                raise BusinessError("FILE_CONFLICT", "项目目录发生外部变化，已停止操作", 409)
            for name, parent in self.parents.items():
                child = fd
                for part in Path(name).parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=child)
                    check.callback(os.close, child)
                if identity(os.fstat(child)) != identity(os.fstat(parent)):
                    raise BusinessError("FILE_CONFLICT", "文件目录发生外部变化，已停止操作", 409)

    def read(self, name):
        self.verify()
        fd = self.parents[name]
        try:
            file = os.open(Path(name).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError:
            return None, 0o600
        with os.fdopen(file, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size > MAX_TEXT
                or before.st_mode & 0o7000
            ):
                raise BusinessError("FILE_UNAVAILABLE", "文件类型、链接或大小不满足安全条件", 409)
            raw = stream.read(MAX_TEXT + 1)
            after = os.fstat(stream.fileno())
            current = os.stat(Path(name).name, dir_fd=fd, follow_symlinks=False)
            if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ) or identity(current) != identity(before):
                raise BusinessError("FILE_CONFLICT", "读取时文件发生外部变化，已停止操作", 409)
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise BusinessError("TEXT_FORBIDDEN", "文件不是 UTF-8 文本", 409) from None
        text_check(content)
        return content, stat.S_IMODE(before.st_mode)

    def write(self, name, content, mode, expected):
        # Expected-byte comparison is repeated immediately before replace/unlink.
        if self.read(name)[0] != expected:
            raise BusinessError("FILE_CONFLICT", "外部文件与补丁冲突，双方内容保留", 409)
        parent, basename = self.parents[name], Path(name).name
        if content is None:
            self.verify()
            if self.read(name)[0] != expected:
                raise BusinessError("FILE_CONFLICT", "恢复时文件被外部修改，已保留", 409)
            os.unlink(basename, dir_fd=parent)
        else:
            temporary = ".factory-patch-" + str(uuid4())
            fd = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
            )
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content.encode())
                    stream.flush()
                    os.fchmod(stream.fileno(), mode & 0o777)
                    os.fsync(stream.fileno())
                self.verify()
                if self.read(name)[0] != expected:
                    raise BusinessError("FILE_CONFLICT", "写入前文件发生变化，已保留双方内容", 409)
                os.replace(temporary, basename, src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
        os.fsync(parent)
