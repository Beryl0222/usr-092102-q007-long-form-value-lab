# 长内容价值实验室后端

内容平台把**收藏、回访（收藏后打开/跨日看完）、公共讨论、兴趣拓展**加入推荐目标后，经典课文长视频曝光上升。但创作者质疑：这是作品长期价值被识别，还是运营活动、粉丝集中回访、反谣言降权造成的“表面提升”？直接改线上权重又无法向审查人员解释影响范围。

本仓库是一套**事件溯源（event sourcing）的实验后端**，让“长期价值是否真实”可被实验验证、可复算、可申诉、可对外报告而不泄露隐私。

## 设计原则

| 关切 | 机制 |
| --- | --- |
| 价值真实 vs 表面提升 | 只有 `provenance=organic` 的信号计入长期价值；**互刷 / 运营投放 / 粉丝集中回访单独标记、单独汇总、不进价值分** |
| 时间窗不能混用 | 五类信号各自独立窗口：点击 1h、收藏 72h、收藏后打开（锚=收藏）24h、跨日看完（次日且 7 日内、进度≥90%）、有效讨论（72h、质量≥0.6、未删除） |
| 迟到数据 | 窗口关闭后有 24h 宽限；宽限内只能以 `WINDOW_PARTIALLY_CORRECTED` **追加纠正**，不改写历史；封账后或实验结束后一律拒绝 |
| 有害内容不被重新放大 | `remove` 拦截一切曝光，`downrank` 拦截实验组自然流量；有害曝光率增量超门栏自动 `KILL_SWITCH_TRIGGERED` 并暂停分桶 |
| 规则变更 / 紧急止损 | 暂停期间拒绝一切新分桶；恢复强制沿用原盐，分桶确定性输出，老用户不可能跨组；全局 `audit_contamination` 审计 |
| 机会差异 | 监测**新作者、小众主题、低/中/高使用时长**三类人群的曝光占比差与有机转化率比，超门栏禁止扩量 |
| 可复算 | 每个 `POLICY_DECIDED` 带 `manifest_hash`（决策前事件序列+目标权重的指纹）；`audit.recompute_decision` 重放复现动作，并校验事件内容哈希链 |
| 创作者透明 | 看到中文可理解的信号贡献（哪些计入/为何剔除）与申诉结果；未封账窗口申诉成立可纠正，已封账只记录、不篡史 |
| 外部报告隐私 | k-匿名抑制（组合人群<10 人整行剔除）、计数按百位取整、比率两位小数；不含用户伪 ID/个体轨迹，不披露模型权重与风控阈值 |

## 目录

```
contracts/domain.schema.json   事件信封契约（additive 演进）
src/long_lab/
  contracts.py     事件→聚合映射与类型化 payload 注册表（与 schema 互校，SSOT）
  events.py        追加型 JSONL 事件库、确定性 event_id、聚合乐观版本
  identity.py      HMAC 去标识化伪 ID、确定性分桶哈希
  timekeeping.py   可注入时钟
  content.py       发布版本/版本更替/控制关系声明/风险处置
  catalog.py       内容版本、创作者控制关系并查集、风险处置时间线读模型
  experiments.py   开轮/暂停/恢复/结束、分桶、跨组污染审计
  signals.py       曝光与信号管线：时间窗、封账、迟到纠正、再分类、风险门栏
  detection.py     互刷/运营/粉丝回访来源判定
  objective.py     版本化目标函数与有机价值计算
  metrics.py       从事件完全重放的指标投影 + 决策指纹
  governance.py    止损熔断、公平性门栏、扩量/维持/回滚决策
  transparency.py  创作者解释、申诉、隐私安全外部报告
  audit.py         决策复算与日志完整性（哈希链）校验
examples/demo.py   端到端叙事演示
tests/             31 个测试
```

## 事件与聚合

沿用并 additive 扩展了原始契约。原有 5 个事件（`SIGNAL_RECORDED` 等）与 4 个聚合保持不变；新增内容版本、创作者关系、曝光、实验生命周期、目标版本、止损、公平性、申诉等事件，完整枚举见 `contracts/domain.schema.json` 与 `src/long_lab/contracts.py`，测试会双向校验二者不漂移。

## 运行

```bash
python3 -m unittest discover -s tests -t .   # 测试
python3 -m examples.demo                     # 端到端演示
```

演示覆盖：真实有机提升被判为 `scale`、运营/互刷/粉丝回访无法伪装价值、反谣言降权内容被门栏拦截、止损暂停与恢复零跨组污染、迟到数据只纠正未封账窗口、公平性快照、创作者解释与申诉、k 匿名外部报告，以及事后用 `manifest_hash` 复算“当初为什么扩大”。

## 典型代码入口

```python
from src.long_lab.experiments import ExperimentService
from src.long_lab.signals import SignalPipeline
from src.long_lab.governance import GovernanceService
from src.long_lab.audit import recompute_decision

experiments.assign(experiment_id, user_pseudo)          # 暂停时抛 AssignmentBlocked
pipeline.record_exposure(...)                           # 风险内容在此被硬门栏拦截
pipeline.record_signal(exposure, "cross_day_complete", ts, progress=0.96)
GovernanceService(store, catalog, objective_id).decide(experiment_id, decision_id)
recompute_decision(store, decision_id)                  # 审查人员复算
```

## 边界与取舍

- 事件库为单文件 JSONL，面向领域验证与联调；生产应替换为带追加锁的存储，`EventStore` 接口保持不变。
- 目标权重、公平性门栏（5 个百分点 / 80% 转化率比）、k=10 等为可在 `objective.py`/`governance.py`/`transparency.py` 调整的策略常量，变更需发新版本事件。
- 去标识化使用 HMAC 伪 ID，生产的 pepper 必须由密钥管理注入，禁止入库明文用户标识。
