import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    allowed_roots: tuple[Path, ...]
    token: str
    origins: tuple[str, ...] = ("http://127.0.0.1:3000", "http://localhost:3000")
    hosts: tuple[str, ...] = ("127.0.0.1:8765", "localhost:8765")
    model_env: Path | None = None


def load_settings() -> Settings:
    data = Path(os.getenv("FACTORY_DATA_DIR", str(ROOT / "data"))).resolve()
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    token_file = data / "backend_token"
    try:
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(secrets.token_urlsafe(48))
    except FileExistsError:
        pass
    if token_file.is_symlink():
        raise RuntimeError("安全配置文件不能是符号链接")
    token_file.chmod(0o600)
    token = token_file.read_text().strip()
    if len(token) < 32:
        raise RuntimeError("本机访问配置无效，请重新配置")
    roots = json.loads(os.getenv("FACTORY_ALLOWED_ROOTS", json.dumps([str(ROOT)])))
    if not isinstance(roots, list) or not roots or not all(isinstance(p, str) for p in roots):
        raise RuntimeError("授权根目录配置无效")
    return Settings(data, tuple(Path(p).resolve(strict=True) for p in roots), token)
