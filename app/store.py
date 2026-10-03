"""SQLite 持久化：表结构与连接初始化。

设计要点：
- submissions 以 (单位, 指标, 周期, 口径) 为自然键，重复导入只会更新或判重，
  不会产生第二份数据；
- snapshots / snapshot_lines 在锁定时刻固化各单位数值，之后任何写入都被拒绝；
- recalcs / recalc_impacts 留痕每次重算的来源口径、目标口径、变更原因与
  逐单位影响，原快照与原口径数据均不改动；
- requests 记录每个幂等键的首次响应，重放时原样返回。
"""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS orgs (
    org_id      TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    parent_id   TEXT REFERENCES orgs(org_id),
    level       INTEGER NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS actors (
    actor_id    TEXT PRIMARY KEY,
    role        TEXT NOT NULL CHECK (role IN ('group_admin', 'admin', 'reviewer', 'unit_reporter')),
    org_id      TEXT NOT NULL REFERENCES orgs(org_id)
);

CREATE TABLE IF NOT EXISTS metrics (
    metric_code TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    unit        TEXT NOT NULL DEFAULT '',
    aggregation TEXT NOT NULL DEFAULT 'SUM' CHECK (aggregation IN ('SUM', 'AVG'))
);

CREATE TABLE IF NOT EXISTS calibers (
    caliber_id  TEXT PRIMARY KEY,
    metric_code TEXT NOT NULL REFERENCES metrics(metric_code),
    version     INTEGER NOT NULL,
    formula     TEXT NOT NULL,
    inputs_json TEXT NOT NULL,
    org_level   INTEGER NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('draft', 'active', 'archived')),
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (metric_code, version)
);

CREATE TABLE IF NOT EXISTS submissions (
    unit_id        TEXT NOT NULL REFERENCES orgs(org_id),
    metric_code    TEXT NOT NULL REFERENCES metrics(metric_code),
    period         TEXT NOT NULL,
    caliber_id     TEXT NOT NULL REFERENCES calibers(caliber_id),
    values_json    TEXT NOT NULL,
    computed_value REAL NOT NULL,
    submitted_by   TEXT NOT NULL,
    request_id     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (unit_id, metric_code, period, caliber_id)
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    metric_code TEXT NOT NULL,
    period      TEXT NOT NULL,
    org_id      TEXT NOT NULL REFERENCES orgs(org_id),
    caliber_id  TEXT NOT NULL REFERENCES calibers(caliber_id),
    locked_by   TEXT NOT NULL,
    locked_at   TEXT NOT NULL,
    UNIQUE (metric_code, period, org_id, caliber_id)
);

CREATE TABLE IF NOT EXISTS snapshot_lines (
    snapshot_id    TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    unit_id        TEXT NOT NULL,
    computed_value REAL NOT NULL,
    PRIMARY KEY (snapshot_id, unit_id)
);

CREATE TABLE IF NOT EXISTS recalcs (
    recalc_id       TEXT PRIMARY KEY,
    metric_code     TEXT NOT NULL,
    period          TEXT NOT NULL,
    from_caliber_id TEXT NOT NULL REFERENCES calibers(caliber_id),
    to_caliber_id   TEXT NOT NULL REFERENCES calibers(caliber_id),
    reason          TEXT NOT NULL,
    initiated_by    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (metric_code, period, to_caliber_id)
);

CREATE TABLE IF NOT EXISTS recalc_impacts (
    recalc_id TEXT NOT NULL REFERENCES recalcs(recalc_id),
    unit_id   TEXT NOT NULL,
    old_value REAL,
    new_value REAL,
    status    TEXT NOT NULL CHECK (status IN ('recalculated', 'missing_input', 'kept_existing')),
    PRIMARY KEY (recalc_id, unit_id)
);

CREATE TABLE IF NOT EXISTS requests (
    request_id   TEXT PRIMARY KEY,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    result_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
