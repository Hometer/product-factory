"""Bounded selective local deliveries and explicit manual production reports."""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, StrictBool, field_validator

from .defect_schemas import Confirmed
from .patch_schemas import Contract

CHECKS = ("isolation", "persistence", "secrets", "errors", "monitoring", "recovery")


class PrivateConfirmed(Confirmed):
    sensitive_checked: StrictBool

    @field_validator("sensitive_checked")
    @classmethod
    def privacy(cls, value):
        if not value:
            raise ValueError("必须核对选择文件及文档不含秘密或隐私")
        return value


class Preview(PrivateConfirmed):
    stage_id: str = Field(pattern=r"^S[0-9]{1,4}$")
    expected_index_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: list[str] = Field(min_length=1, max_length=20)
    startup_steps: list[str] = Field(min_length=1, max_length=10)
    config_names: list[str] = Field(max_length=20)
    limitations: str = Field(min_length=8, max_length=3000)
    backup_recovery: str = Field(min_length=8, max_length=3000)

    @field_validator("files", "config_names")
    @classmethod
    def distinct(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("选择项不能重复")
        return value

    @field_validator("files")
    @classmethod
    def safe_names(cls, value):
        for name in value:
            name.encode("utf-8")
            if not 1 <= len(name) <= 240 or any(ord(c) < 32 or ord(c) == 127 for c in name):
                raise ValueError("文件名须安全且不能包含控制字符")
        return value

    @field_validator("config_names")
    @classmethod
    def names(cls, value):
        import re

        if any(not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", v) for v in value):
            raise ValueError("配置只填环境变量名称，不填值")
        return value

    @field_validator("startup_steps")
    @classmethod
    def steps(cls, value):
        for step in value:
            step.encode("utf-8")
        if any(not 8 <= len(v.strip()) <= 1000 for v in value):
            raise ValueError("启动步骤须具体")
        return value

    @field_validator("limitations", "backup_recovery")
    @classmethod
    def prose(cls, value):
        if len(value.strip()) < 8:
            raise ValueError("限制与恢复方法须具体")
        return value


class Package(PrivateConfirmed):
    preview_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class Check(Contract):
    status: Literal["pending", "passed", "failed"]
    reason: str = Field(min_length=4, max_length=1000)
    evidence: list[str] = Field(max_length=10)

    @field_validator("reason")
    @classmethod
    def specific(cls, value):
        if len(value.strip()) < 4:
            raise ValueError("检查依据须具体")
        return value

    @field_validator("evidence")
    @classmethod
    def locations(cls, value):
        for location in value:
            location.encode("utf-8")
        if any(not 4 <= len(v.strip()) <= 500 for v in value):
            raise ValueError("证据定位须具体")
        return value


class Release(Confirmed):
    package_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_id: str | None = Field(default=None, pattern=r"^[0-9a-f-]{36}$")
    outcome: Literal["partial", "reported_release"]
    address: str = Field(max_length=500)
    checks: dict[str, Check]

    @field_validator("*")
    @classmethod
    def meaningful(cls, value):
        if isinstance(value, str) and value and not value.strip():
            raise ValueError("内容不能为空白")
        return value

    @field_validator("checks")
    @classmethod
    def complete(cls, value):
        if set(value) != set(CHECKS):
            raise ValueError("必须保留六项生产检查")
        if any(c.status != "pending" and not c.evidence for c in value.values()):
            raise ValueError("已检查项需要证据定位")
        return value

    @field_validator("address")
    @classmethod
    def safe_address(cls, value):
        if not value:
            return value
        try:
            u = urlsplit(value)
            if (
                u.scheme not in {"http", "https"}
                or not u.hostname
                or u.username
                or u.password
                or u.query
                or u.fragment
                or any(ord(c) <= 32 for c in value)
                or "\\" in value
            ):
                raise ValueError("invalid")
            _ = u.port
        except ValueError:
            raise ValueError("地址只能填写无凭证、查询参数或片段的HTTP(S)定位") from None
        return value


class Asset(Contract):
    id: str
    project_id: str
    kind: Literal["delivery_preview", "delivery_package", "release_record"]
    payload: dict
    content_hash: str
    created_at: str
    current: bool
    issues: list[str]
    blockers: list[str]


class Workspace(Contract):
    stages: list[dict]
    files: list[str]
    index_hash: str
    previews: list[Asset]
    packages: list[Asset]
    releases: list[Asset]
    notice: str
