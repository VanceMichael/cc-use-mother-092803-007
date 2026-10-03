# 央企指标管理

面向口径版本与周期快照的指标管理服务：集团按组织层级维护公式与口径版本，
下属单位提交可校验的周期数据，复核人员锁定快照后任何人不得覆盖，新口径启用后
可发起重算，原快照、受影响单位与变更原因全部留痕。

服务以本地 Python 模块运行，数据保存在调用方提供的 SQLite 文件中
（`SoeMetricsService(db_path)`，默认 `:memory:`）。

## 角色与权限

| 角色 | 权限 |
| --- | --- |
| `group_admin` 集团管理员 | 组织登记、授权、指标与口径维护、发起重算、代任意单位填报 |
| `admin` 普通管理员 | 管辖组织范围内填报与查询；快照锁定后同样不得覆盖 |
| `reviewer` 复核人员 | 锁定快照、查询 |
| `unit_reporter` 单位填报员 | 仅为本单位填报、查询 |

未注册的操作人一律拒绝；越权操作返回 `reason_code=FORBIDDEN`。
系统通过 `system.bootstrap` 一次性引导出根组织与首位集团管理员。

## 动作目录

| action | 角色 | 说明 |
| --- | --- | --- |
| `system.bootstrap` | 空库任意 | `{root_org_id, root_name, admin_actor}`，仅可执行一次 |
| `org.register` | group_admin | 按层级登记组织，层级由上级推导 |
| `actor.grant` | group_admin | 授予/调整角色与所属组织 |
| `metric.define` | group_admin | 定义指标（汇总方式 SUM/AVG、单位） |
| `caliber.create` | group_admin | 新建口径版本：公式 + inputs 字段规格 + 适用组织层级，草稿态 |
| `caliber.activate` | group_admin | 启用口径版本，同时归档上一启用版本 |
| `data.submit` | 填报类角色 | 单条周期数据，按启用口径校验并计算 |
| `data.import` | 填报类角色 | 批量导入，全部校验通过才落库（全有或全无） |
| `snapshot.lock` | reviewer | 锁定 (指标, 周期, 组织) 范围快照，固化各单位数值 |
| `recalc.start` | group_admin | 按新口径重算周期数据，必须填写变更原因 |
| `query.aggregate` | 任意已注册角色 | 汇总值 + 参与单位 + 口径版本 + 重算影响 |

口径公式只允许数字、字段名与四则运算；inputs 规格支持
`required / default / min / max`，填报值逐项校验，除零等异常同样拒绝。

## 一致性约定

- **幂等**：每个请求携带 `request_id`，首次响应被记录，重放原样返回；
  同一 `request_id` 携带不同内容会被拒绝（`IDEMPOTENCY_CONFLICT`）。
- **去重**：周期数据以 (单位, 指标, 周期, 口径) 为自然键，重复导入只会
  判重（`unchanged`）或更正（`updated`），不会产生第二份数据。
- **锁定**：快照锁定后该范围任何写入均被拒绝（`LOCKED`），包括集团管理员；
  调整只能启用新口径并发起重算。
- **重算**：原口径数据与快照保持不变；逐单位记录影响
  （`recalculated / missing_input / kept_existing`，已按新口径自行填报的单位
  不会被重算覆盖）；同一 (指标, 周期, 目标口径) 只能重算一次。

## 端到端示例

```bash
# 引导 → 建组织/授权 → 定义指标与口径 v1 → 启用
echo '{"actor":"installer","action":"system.bootstrap","request_id":"b1",
       "payload":{"root_org_id":"GRP","root_name":"集团总部","admin_actor":"ga"}}' \
  | python3 -m app.api /tmp/soe.db
# 单位填报 → 复核锁定 → 启用口径 v2 → 发起重算 → 查询汇总（含重算影响）
echo '{"actor":"ga","action":"query.aggregate","request_id":"q1",
       "payload":{"metric_code":"SEI","period":"2030-01","org_id":"GRP"}}' \
  | python3 -m app.api /tmp/soe.db
```

数据库文件默认取环境变量 `SOE_METRICS_DB`，缺省 `./soe_metrics.db`。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 构建检查

```bash
python3 -m compileall app
```

## 目录

`app/contracts.py` 请求/结果约定；`app/formula.py` 公式与填报校验；
`app/store.py` 表结构；`app/service.py` 领域服务；`app/api.py` 本地调用入口；
`tests/` 行为测试。
