# 深度登记 metricreg（指标定义注册与依赖环检测）

- 规模：中型（8 个功能模块 / 8 个页面 / 91 个文件 / 133 条测试）
- 核心规则一：**DSL 解析与登记**——一行一条 `name = expr [where …] [group by …] [depends: …]`，先做全角归一，再词法/语法分析；结构扫描与语法解析两条独立路径，检出项按固定优先级排序，规则书写顺序不影响结论。
- 核心规则二：**依赖成环检测**——按指标→指标边建图，用显式栈迭代 Tarjan 求强连通分量；分量大小 >1 或单点自环即判环，给出环路路径；同名指标按聚合/过滤/分组差异分三级冲突，保留行号最小的一条。
- 核心规则三：**下线与迁移次序**——环按分量计数（一个环只出一条，TRAP-27），单点只有自环才算环（孤立指标不进环，TRAP-28）；迁移次序按「依赖方先迁、被依赖方后下线」做确定性 Kahn 排序，环内指标进阻断表；同名指标按评估日生效版本说清下的是哪一版。
- 使用者与工作流：星野零售指标体系组 6 人，180 个指标散落 6 个 BI，同名不同定义最耗时；现在一次粘贴即得到「注册表 + 诊断隔离 + 环路径 + 同名冲突 + 影响面 + 变更单导出」。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 指标接入 | 粘贴 DSL 或选样例，看到逐行状态与阶段计数 | POST /api/import、页面 / | `app/modules/m1_ingest/service.py` `app/modules/m1_ingest/decode.py` `app/modules/m1_ingest/reader.py` `app/modules/m1_ingest/lines.py` | `tests/test_ingest.py` |
| M2 语法规则 | 查聚合/字段/维度登记表与文法，试解析单行 | GET /api/contract、POST /api/syntax/preview、页面 /contract | `app/modules/m2_syntax/grammar.py` `app/modules/m2_syntax/contract.py` `app/modules/m2_syntax/coerce.py` | `tests/test_syntax.py` |
| M3 解析诊断 | 看原因码分布与隔离行，导出隔离 CSV | GET /api/diagnostics、页面 /diagnostics | `app/modules/m3_diagnostics/rules.py` `app/modules/m3_diagnostics/metrics.py` `app/modules/m3_diagnostics/quarantine.py` | `tests/test_diagnostics.py` |
| M4 依赖图与环 | 看依赖图、环路径、同名冲突表 | GET /api/graph、页面 /graph | `app/modules/m4_graph/builder.py` `app/modules/m4_graph/tarjan.py` `app/modules/m4_graph/conflicts.py` | `tests/test_graph.py` `tests/test_tarjan.py` |
| M5 版本区间与批次 | 查版本台账、提交新版、拒绝陈旧提交、看批次历史 | GET /api/versions、POST /api/versions/submit、GET /api/batches、页面 /versions | `app/modules/m5_versions/intervals.py` `app/modules/m5_versions/stale.py` `app/modules/m5_versions/batches.py` | `tests/test_versions.py` |
| M6 影响面与导出 | 算影响分、核四账、导出指标/边/冲突/清单 | GET /api/impact、GET /api/reconcile、GET /api/export/*、页面 /impact | `app/modules/m6_impact/impact.py` `app/modules/m6_impact/export_out.py` `app/modules/m6_impact/json_out.py` | `tests/test_impact.py` |
| M7 下线影响 | 选下线指标，看受影响面、迁移次序与阻断项，导出清单 | GET /api/retire、POST /api/retire、GET /api/retire/order.csv、页面 /retire | `app/modules/m7_retire/cycles.py` `app/modules/m7_retire/order.py` `app/modules/m7_retire/plan.py` `app/modules/m7_retire/service.py` | `tests/test_retire.py` `tests/test_retire_cycles.py` `tests/test_retire_versions.py` |
| M8 身份与名称复用 | 多个 BI 分批投递名称映射，整批一致后原子切换现网；按「名称+发布时间」回溯旧报表口径 | POST /api/identity/deliver、GET /api/identity/live、/api/identity/round、/api/identity/impact、/api/identity/reports、/api/identity/lookup、/api/identity/rounds、页面 /identity | `app/modules/m8_identity/model.py` `app/modules/m8_identity/snapshot.py` `app/modules/m8_identity/service.py` | `tests/test_identity.py` |

端到端：`tests/test_e2e.py`（样例 → 接入 → 诊断 → 图 → 版本 → 影响面 → 导出，含 4xx 失败输入与幂等重跑）；
下线计划走同一批次数据：`tests/test_retire.py`（环最小化、阻断项、版本含义、接口与页面）。

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-02 | 样例文件头带 UTF-8 BOM | 首个指标名变成 `\ufeff销售额`，与报表引用对不上 | 解码第一步走 `utf-8-sig` 剥掉 BOM 并记 `E_BOM_STRIPPED` | `app/modules/m1_ingest/decode.py` | `tests/test_ingest.py::test_bom_stripped_before_first_name` |
| TRAP-05 | 行尾多一个分隔符、括号少一个、聚合带两个参数 | 静默截断成「能解析」，把错定义登记进注册表 | 结构扫描独立统计括号与记号，多余记号整行隔离 | `app/modules/m3_diagnostics/rules.py` | `tests/test_diagnostics.py::test_trailing_token_isolated` |
| TRAP-07 | `SUM(N/A)`、`where channel = 未知` | 哨兵被当成字段名或 0 参与统计 | 哨兵清单集中在配置，命中即隔离，不当 0 | `app/reasons.py`、`app/modules/m3_diagnostics/rules.py` | `tests/test_diagnostics.py::test_sentinel_value_isolated` |
| TRAP-14 | 同一指标名在三个 BI 里各写一遍，聚合/过滤/分组不同 | 后写覆盖，注册表里悄悄换成一个版本 | 保留行号最小的一条，其余按差异分 BLOCK/REVIEW/INFO 入冲突表并可导出 | `app/modules/m4_graph/conflicts.py` | `tests/test_graph.py::test_same_name_conflict_levels` |
| TRAP-17 | 字段改名：老定义写 `order_amount`，登记表是 `order_amt` | `KeyError` 或整行判成未注册字段 | 别名表收口在契约模块，命中即映射到登记名 | `app/modules/m2_syntax/contract.py` | `tests/test_syntax.py::test_alias_maps_renamed_field` |
| TRAP-19 | 一行同时命中「未知字段」和「多余记号」 | 按代码书写顺序取第一条，换顺序结论就变 | 八条扫描互不短路，最后按 `DIAG_ORDER` 统一排序 | `app/modules/m3_diagnostics/rules.py` | `tests/test_diagnostics.py::test_scan_order_does_not_change_result` |
| TRAP-20 | 指标互相引用成环、指标引用自身 | 递归求环爆栈，或拓扑排序死循环 | 显式栈迭代 Tarjan，单点自环单独判；环路路径可导出 | `app/modules/m4_graph/tarjan.py` | `tests/test_tarjan.py::test_long_ring_without_recursion_limit` |
| TRAP-21 | `depends` 声明了 A 但表达式没引用，或反过来 | 只核单向，血缘图缺半边 | 声明集合与实际引用集合双向比对，两侧各给原因码 | `app/modules/m4_graph/builder.py` | `tests/test_graph.py::test_one_sided_declaration_detected` |
| TRAP-25 | 同一指标 v1 `[01-01, 04-01)`、v2 `[03-15, 07-01)` | 两版同时生效，取到哪版看排序运气 | 半开区间 + 两两相交判定，重叠行标 `OVERLAP`，相邻不算重叠 | `app/modules/m5_versions/intervals.py` | `tests/test_versions.py::test_overlap_flagged` |
| TRAP-26 | 两个组同时改同一指标，后提交的版本号更小 | 后提交无条件覆盖，先改的被静默丢掉 | 版本号必须大于库中最新值，否则 409 `V_STALE_SUBMIT` | `app/modules/m5_versions/stale.py` | `tests/test_versions.py::test_stale_submit_rejected` |
| TRAP-27 | 同一环按起点轮转被重复计为多个环 | 环内每个成员各报一次，3 元环报 3 个环，环数虚高 | 环键取成员集合摘要，一个强连通分量只出一个环；轮转次数单列成对照 | `app/modules/m7_retire/cycles.py` | `tests/test_retire.py::test_ring_count_is_one_per_component` |
| TRAP-28 | 孤立指标被误判为环 | 单点分量一律当环，21 个没有依赖边的指标全进阻断表 | 单点只有自环才算环；孤立指标单列，不进迁移次序 | `app/modules/m7_retire/order.py` | `tests/test_retire.py::test_isolated_nodes_are_not_rings` |
| TRAP-29 | 指标改名后旧名称被新指标复用 | 按名称把历史分段并进同一身份：老定义的 `ref(旧名)` 被后来复用者接走，`A→B` 与 `B→A` 拼成假依赖环；旧报表也被并进新身份 | 身份只由 identity 决定，名称只在指定日期解析；旧报表按发布时间绑定；迁移别名按同一身份合并 | `app/modules/m8_identity/model.py`、`snapshot.py` | `tests/test_identity.py::test_rename_alias_does_not_fabricate_cycle`、`test_old_report_binds_to_old_identity_by_publish_time` |
| TRAP-30 | 分批投递只到一半就切换 | 每个 BI 各自投递即改现网，半批映射把指标拆成两个身份，依赖与影响面忽大忽小 | 批次声明 expected_sources，缺来源保持 pending；整批无冲突且账目平才单条 UPSERT 切换 | `app/modules/m8_identity/service.py` | `tests/test_identity.py::test_round_incomplete_keeps_previous_version` |
| TRAP-31 | 同一批重试新增结果 | 网络重试把同一批写入两次，条目翻倍、指纹变化、现网被无意义重刷 | 同批同来源按内容指纹判重：一致直接返回原投递，不一致 409 `I_DELIVERY_CONFLICT`；更早批次不覆盖现网 | `app/modules/m8_identity/service.py`、`model.py` | `tests/test_identity.py::test_replay_same_delivery_adds_no_result`、`test_older_round_replay_does_not_roll_back_live` |
| TRAP-32 | 旧批次补交一个来源后盖掉新批次 | 批次次序按最后一次投递算，旧批次补交变「最新」，把已上线的新版本顶回去 | 批次次序取开户时点（最早一次投递）；现网次序更大时不覆盖，记 `I_ROUND_SUPERSEDED` | `app/modules/m8_identity/service.py` | `tests/test_identity.py::test_late_source_on_old_round_does_not_override_newer_round` |

## 四账

- 行账：输入行 = 注册指标行 + 隔离行 + 空行 + 注释行
- 节点账：图节点 = 指标 + 字段 + 维度 + 报表
- 边账：依赖边 = 指标→指标 + 指标→字段 + 指标→维度 + 报表→指标
- 分量账：指标节点 = 环内指标 + 非环指标
- 四账在 `GET /api/reconcile`、`/impact` 页面与 `accounts_json` 中显示，差额非 0 会显式为「不平」。

## 反例与不变量

- 反例（朴素实现会挂）：`tests/test_tarjan.py::test_long_ring_without_recursion_limit`（1200 个指标成环，递归实现撞递归上限）、`tests/test_ingest.py::test_naive_splitlines_splits_u2028`（`str.splitlines()` 在 U+2028 上多切一刀）、`tests/test_diagnostics.py::test_naive_first_error_only_loses_diagnostics`。
- 不变量：`tests/test_ingest.py::test_idempotent_rerun_adds_no_rows`（幂等）、`tests/test_impact.py::test_four_accounts_balance`（守恒）、`tests/test_tarjan.py::test_component_partition_is_total`（分量划分不重不漏）。
- 反例（M7）：`tests/test_retire_cycles.py::test_one_ring_per_component_not_per_start`（3 元环按起点会报 3 个）、`tests/test_retire_cycles.py::test_isolated_singleton_without_self_loop_is_not_ring`（单点无自环不是环）、`tests/test_impact.py::test_downstream_excludes_self_reference`（自环不算下游）。
- 不变量（M7）：`tests/test_retire.py::test_affected_is_split_between_order_and_blocked`（受影响 = 可排序 + 阻断）、`tests/test_retire_cycles.py::test_kahn_order_ignores_insertion_order`（次序与插入顺序无关）、`tests/test_retire_cycles.py::test_two_disjoint_rings_count_two`（环成员不重不漏）。
- 反例（M8）：`tests/test_identity.py::test_rename_alias_does_not_fabricate_cycle`（更名迁移别名曾被并成 A→B→A 假环）、`tests/test_identity.py::test_old_report_binds_to_old_identity_by_publish_time`（旧报表曾被并进复用同名的新身份）。
- 不变量（M8）：`tests/test_identity.py::test_replay_same_delivery_adds_no_result`（同批原样重试不新增结果）、`tests/test_identity.py::test_round_incomplete_keeps_previous_version`（未齐保留上一版）、`tests/test_identity.py::test_older_round_replay_does_not_roll_back_live`（重放旧批不回退现网）。
- 端到端：`tests/test_e2e.py::test_pipeline_from_sample_to_export`。

## 已知不覆盖

- 不做指标值计算：DSL 只解析结构、血缘与版本，不执行聚合，因此没有数据量与精度问题；这点故意收窄，避免依赖真实数仓。
- 版本台账只维护 4 个指标的 10 行样例，提交接口写同一批次的 `versions` 表，不回溯历史批次。
- 环检测按指标→指标边，不把报表或维度纳入环判定（报表是叶子节点）。
- 依赖图只按文本渲染成等宽视图，没有交互式拖拽布局；指标规模在几百条以内够用。
- 全角归一覆盖 `U+FF01`–`U+FF5E`、顿号、句号、全角空格；不处理竖排标点与半角片假名。
- M7 只出计划不改数据：不写回注册表、不自动改报表，迁移清单导出 CSV 交人工执行。
- 迁移次序按指标→指标边排，不细分报表内部的字段级改动；报表只作为受影响清单出现。
- 下线请求台账只认 metric/reason/owner/requested_on 四列，不校验申请人与审批状态。
- 身份映射只维护名称分段，不推断「同一 BI 内名称不变即同一身份」；没有映射的名称按
  `name:<名称>` 兜底，宁可分成两个身份也不并错。
- 现网槽位只保留一版（当前生效），不保留历史现网；需要复算时重放原批次。
- 报表发布时间缺失时按评估日解析，非法时不猜；不做报表内部字段级改名迁移。
