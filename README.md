# 央企指标管理

面向**战略性新兴产业等核心指标**的口径版本与周期快照管理服务，解决同一指标
因口径版本和上报周期不同而出现两个数字、差异无法归因的问题。

## 核心规则

- **集团按组织层级维护公式与口径版本**：指标定义输入项，公式只允许四则运算、
  括号与 `max/min/round/abs`，引用必须是已声明输入项；口径创建必须填写变更原因，
  新版本启用后旧版本自动退役，版本号只增不复用。
- **下属单位提交可校验的周期数据**：上报值必须等于按当前启用口径计算的值，
  输入项与指标定义必须完全一致，否则整份数据被拒绝。
- **复核人员锁定快照**：快照冻结口径版本、公式、各单位输入与贡献值；锁定后
  普通单位管理员的任何覆盖一律拒绝。
- **新口径启用可发起重算**：基于快照冻结的原始输入按新口径重算，原快照完整
  保留在版本链上，同时记录受影响单位、新旧汇总值、变更原因和发起人；新口径若
  需要单位未提供的新输入，该单位列入 `excluded_org_ids` 待补报，不计入新值。
- **重复导入不产生第二份数据**：所有写操作按 `request_id` 幂等；批量导入再按
  `import_ref + 指标 + 周期 + 单位` 去重，重复导入返回 `duplicate_ignored`。
- **权限越界即拒绝**：
  - `hq_admin` 集团管理员：组织、用户、指标与口径；
  - `unit_admin` 单位管理员：只能上报本单位及下级单位数据，查询范围同样受限；
  - `reviewer` 复核人员：锁定快照、发起重算；
  - `viewer` 只读查询。
- **查询汇总值**同时返回参与计算的单位及各自贡献、口径版本与公式、快照版本链
  （含原快照）和全部重算影响。

## 动作目录

| 动作 | 角色 | 说明 |
|---|---|---|
| `add_user` | 集团管理员（首个用户可引导登记） | 登记用户与角色 |
| `add_org` | 集团管理员 | 登记组织，指定 `parent_id` 形成层级 |
| `define_metric` | 集团管理员 | 定义指标及输入项编码 |
| `extend_metric_inputs` | 集团管理员 | 为新口径追加输入项（只增不删） |
| `create_caliber` | 集团管理员 | 新建口径版本（必填 `reason`），可 `activate: false` 暂存 |
| `activate_caliber` | 集团管理员 | 启用指定版本，旧版本退役 |
| `submit_data` | 单位管理员 | 提交周期数据并做口径校验 |
| `import_data` | 单位管理员 | 批量导入，需 `import_ref`，同批重复自动去重 |
| `lock_snapshot` | 复核人员 | 按全集团或 `scope_org` 子树锁定周期快照 |
| `recalculate` | 复核人员 | 按新口径重算已锁定快照（必填 `reason`） |
| `query_summary` | 全部角色（单位管理员限本单位子树） | 查询汇总值、参与单位、口径版本、重算影响 |

## 目录

- `app/model.py`：领域模型（角色、口径、上报、快照、重算）
- `app/formula.py`：安全公式编译与求值（AST 白名单）
- `app/service.py`：领域服务与 SQLite 事件重放持久化
- `app/contracts.py`：请求/结果约定
- `app/api.py`：本地 stdin/stdout 调用入口
- `tests/`：行为测试（24 个用例）

## 测试

```bash
python3 -m unittest discover -s tests
```

## 构建检查

```bash
python3 -m compileall app tests
```

## 使用

内存模式（数据不跨进程保留）：

```bash
echo '{"actor":"hq","action":"define_metric",
       "payload":{"metric_code":"sei","name":"战新产值","unit":"万元",
                  "inputs":["base","rate"]},
       "request_id":"r-1"}' | python3 -m app.api
```

持久化模式（SQLite；受理的命令以事件形式追加，重启时重放重建状态，
`request_log` 保证重启后请求幂等仍然生效）：

```bash
echo '{...}' | python3 -m app.api --db metrics.db
```
