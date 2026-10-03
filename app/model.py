"""指标管理领域模型：组织层级、角色、口径版本、周期上报、快照、重算。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Role(str, Enum):
    HQ_ADMIN = "hq_admin"        # 集团管理员：维护组织树、指标与口径
    UNIT_ADMIN = "unit_admin"    # 下属单位管理员：提交/重复导入本单位数据
    REVIEWER = "reviewer"        # 复核人员：锁定快照、发起重算
    VIEWER = "viewer"            # 只读查询


# 口径版本状态机：draft → active → retired（重算后旧版本进入 retired，原快照不动）
class CaliberState(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    RETIRED = "retired"


# 周期上报状态机：submitted → verified（被快照锁定后转为 verified/frozen 标记）
class SubmissionState(str, Enum):
    SUBMITTED = "submitted"
    LOCKED = "locked"


class PermissionError_(Exception):
    """角色或数据范围越界。"""


class DomainError(Exception):
    """业务规则拒绝。"""


@dataclass
class Organization:
    org_id: str
    name: str
    parent_id: Optional[str] = None


@dataclass
class User:
    user_id: str
    role: Role
    org_id: Optional[str] = None  # 单位管理员的数据范围；集团/复核/只读可为 None


@dataclass
class Metric:
    metric_code: str
    name: str
    unit: str
    inputs: list[str] = field(default_factory=list)  # 允许引用的输入指标编码


@dataclass
class CaliberVersion:
    """一个指标的一版公式口径。"""
    metric_code: str
    version: int
    formula: str
    state: CaliberState = CaliberState.DRAFT
    reason: str = ""
    created_by: str = ""
    activated_by: str = ""
    superseded_by: Optional[int] = None  # 被哪个新版本替代


@dataclass
class Submission:
    """下属单位就某指标某周期提交的可校验数据。"""
    metric_code: str
    period: str
    org_id: str
    inputs: dict[str, float]
    stated_value: float           # 上报自算值
    computed_value: float         # 服务按当时激活口径重算值
    caliber_version: int
    state: SubmissionState = SubmissionState.SUBMITTED
    submitted_by: str = ""
    import_ref: str = ""          # 导入批次号（重复导入去重依据）
    request_id: str = ""          # 首次提交的幂等键


@dataclass
class Snapshot:
    """复核锁定的周期快照；一经锁定，值、单位列表、口径全部冻结。"""
    metric_code: str
    period: str
    caliber_version: int
    formula: str
    aggregate_value: float
    org_ids: list[str]
    contributions: dict[str, float]  # org_id -> 冻结值
    frozen_inputs: dict[str, dict[str, float]]  # org_id -> 冻结输入，供重算复现
    locked_by: str
    reason: str = ""
    recalculation_id: Optional[str] = None  # 由哪次重算产生（原始快照为 None）
    excluded_org_ids: list[str] = field(default_factory=list)  # 重算时缺新输入、未计入的单位


@dataclass
class Recalculation:
    recalc_id: str
    metric_code: str
    period: str
    old_caliber_version: int
    new_caliber_version: int
    old_value: float
    new_value: float
    affected_org_ids: list[str]
    reason: str
    requested_by: str
    old_snapshot_ref: str  # 原快照的存储键，保证原快照保留可查
    excluded_org_ids: list[str] = field(default_factory=list)  # 缺新输入、待补报的单位
