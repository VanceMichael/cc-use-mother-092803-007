"""央企指标管理 的输入输出约定。"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

@dataclass(frozen=True)
class Request:
    actor: str
    action: str
    payload: dict[str, Any]
    request_id: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

@dataclass
class Result:
    accepted: bool
    state: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"accepted": self.accepted, "state": self.state, "message": self.message, "data": self.data}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Result":
        return cls(bool(raw["accepted"]), str(raw["state"]), str(raw["message"]), dict(raw.get("data") or {}))

def validate_request(request: Request) -> None:
    if not request.actor or not request.action or not request.request_id:
        raise ValueError("请求缺少身份、动作或幂等键")
    if not isinstance(request.payload, dict):
        raise TypeError("请求数据必须是对象")
