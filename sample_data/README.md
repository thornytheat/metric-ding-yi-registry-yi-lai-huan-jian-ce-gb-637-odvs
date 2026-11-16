# 样例数据

| 文件 | 行数 | 真实问题 |
|---|---|---|
| metrics_dirty.dsl | 见文件 | UTF-8 BOM、CRLF 与 LF 混用、全角逗号与全角括号、别名列名、空值哨兵、括号不配对、多余记号、聚合白名单外写法、未注册字段、自引用、2 环与 3 环、同名不同定义、U+2028 分隔符 |
| metrics_clean.dsl | 11 | 无问题，用于对照同一代码路径 |
| metric_versions.csv | 10 | 版本有效期重叠（销售额 v1/v2）、起止倒挂（毛利额 v2）、未注册指标 |
| metric_identity.csv | 6 | 更名（I-SALES 三段名称）、名称复用（`销售额` 先前属于 I-SALES，后由 I-SALES-NEW 复用）；按 source 拆成两批投递 |
| report_usage.csv | 10 | 报表引用未注册指标（R-10）；旧报表带 `published_on`，新报表留空 |

脏样例与干净样例走同一条解析链路，没有任何特判分支。

`metric_identity.csv` 的 `销售额` 一行会出现在两个批次里：BI-报表 投递 2026-03-01~2026-09-01
属于 I-SALES，BI-看板 投递 2026-09-01 起属于 I-SALES-NEW。映射前后衔接，按发布时间查
2026-05-01 得到 I-SALES，查 2026-10-01 得到 I-SALES-NEW，两批未齐时不切换现网。
