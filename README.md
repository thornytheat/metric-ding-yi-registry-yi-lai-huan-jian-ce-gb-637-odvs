# metricreg 指标定义注册与依赖环检测

星野零售指标体系组用的一次性解析工具：把散落在 6 个 BI 里的指标定义粘贴进来，
得到「注册表 + 诊断隔离 + 依赖环 + 同名冲突 + 影响面 + 变更单导出 + 下线迁移清单」。

## 能力

| 模块 | 动作 | 入口 |
|---|---|---|
| M1 指标接入 | 粘贴/样例 → 逐行状态与阶段计数 | POST /api/import、/ |
| M2 语法规则 | 聚合/字段/维度登记表、文法、单行试解析 | `GET /api/contract`、`POST /api/syntax/preview`、`/contract` |
| M3 解析诊断 | 原因码分布、隔离行、隔离 CSV | `GET /api/diagnostics`、`/diagnostics` |
| M4 依赖图与环 | 节点/边、强连通分量、环路径、同名冲突 | `GET /api/graph`、`/graph` |
| M5 版本区间与批次 | 版本台账、提交新版、拒绝陈旧、批次历史 | `GET /api/versions`、`POST /api/versions/submit`、`GET /api/batches`、`/versions` |
| M6 影响面与导出 | 影响分、四账、指标/边/冲突/清单导出 | `GET /api/impact`、`GET /api/reconcile`、`GET /api/export/*`、`/impact` |
| M7 下线影响 | 受影响面、替代建议、迁移次序、阻断项、清单导出 | `GET /api/retire`、`POST /api/retire`、`GET /api/retire/order.csv`、`/retire` |
| M8 身份与名称复用 | 分批投递名称映射、整批核对后原子切换、按「名称+发布时间」回溯旧口径 | `POST /api/identity/deliver`、`GET /api/identity/live`、`GET /api/identity/round`、`GET /api/identity/impact`、`GET /api/identity/reports`、`GET /api/identity/lookup`、`GET /api/identity/rounds`、`/identity` |

## 核心规则

1. **DSL 解析**：一行一条 `name = expr [where …] [group by …] [depends: …]`；先全角归一，
   再分词与递归下降解析，见 `docs/DSL-GRAMMAR.md`。
2. **八条独立扫描 + 固定优先级**：括号、哨兵、聚合白名单、字段登记、分组维度等各写一条扫描，
   互不短路，最后按 `config.DIAG_ORDER` 排序；换扫描顺序结论不变。
3. **迭代 Tarjan 求环**：显式栈实现，长链不爆栈；单点自环单独判，环路路径可导出。
4. **同名冲突三级**：保留行号最小的一条，其余按聚合/表达式/过滤/分组差异分 BLOCK、REVIEW、INFO。
5. **版本半开区间**：`[valid_from, valid_to)`，重叠标记，起止倒挂标记，陈旧提交拒绝。
6. **影响面**：反向可达（带 visited 防环）× 受影响报表权重合计，Decimal 两位。
7. **下线迁移**：环按强连通分量计数，一个分量只出一个环，单点只有自环才算环；
   迁移次序按「依赖方先迁、被依赖方后下线」做确定性排序，环内指标进阻断表，
   同名指标按评估日生效版本说清下的是哪一版。
8. **身份与名称复用**：一条映射是「某身份在某段时间叫某名称」，半开区间；
   身份只由 `identity` 决定、名称只在指定日期解析——同名分段绝不并进同一身份，
   否则老定义里的 `ref(旧名)` 会被后复用的新身份接走，凭空造出依赖环（TRAP-29）。
   分批投递按批次收齐、整批一致才单条 UPSERT 切换现网；冲突或未齐保留上一版，
   同批同来源原样重试不新增投递也不新增结果。

定义、单位、空值、去重键、账目等式见 `docs/DATA-DICTIONARY.md`；模块与陷阱登记见 `docs/DEPTH.md`。

## 启动

```bash
docker compose up -d --build
curl -s -X POST 127.0.0.1:18206/api/import -H 'content-type: application/json' -d '{}'
curl -s 127.0.0.1:18206/api/graph | head -c 400
curl -s "127.0.0.1:18206/api/impact?metric=销售额"
```

页面：<http://127.0.0.1:18206/>（接入）、`/contract`、`/diagnostics`、`/graph`、`/versions`、`/impact`、`/retire`、`/identity`。

测试：

```bash
docker compose exec -T app pytest -q
# 或本机
python3 -m pytest -q
```

重建库：`docker compose down -v` 后重跑即可，SQLite 落在命名卷 `metricreg-data`。

## 样例数据

| 文件 | 真实问题 |
|---|---|
| `sample_data/metrics_dirty.dsl` | BOM、CRLF 混用、全角标点、别名列名、空值哨兵、括号不配对、多余记号、白名单外聚合、未注册字段、自引用、2 环、3 环、同名不同定义、U+2028 |
| `sample_data/metrics_clean.dsl` | 干净对照，走同一条代码路径 |
| `sample_data/metric_versions.csv` | 区间重叠、起止倒挂、未注册指标 |
| `sample_data/metric_identity.csv` | 更名（同一身份两段名称）、名称复用（同名前后的身份不同）；按 source 拆成两批投递 |
| `sample_data/report_usage.csv` | 报表引用未注册指标（R-10）；旧报表带发布时间、新报表不带 |
| `sample_data/retire_requests.csv` | 重复请求行、请求指向未注册指标（已下线指标） |

## 已知不覆盖

- 不求值：DSL 只解析结构、血缘与版本，不执行聚合，因此没有数据量误差问题。
- 环只按指标→指标边判定，报表与维度为叶子节点不参与。
- 全角归一覆盖 `U+FF01`–`U+FF5E`、顿号、句号、全角空格；竖排标点不处理。
- 版本提交写入当前批次，不回溯历史批次；无登录与权限，供内网 6 人使用。
- 下线只出计划不改数据：不写回注册表、不自动改报表，清单导出 CSV 交人工执行。
- 迁移次序按指标→指标边排，不细分报表内部的字段级改动。
- 身份映射只认「identity / name / valid_from / valid_to」四列；没有映射的名称按
  `name:<名称>` 兜底，绝不把同名分段并进同一身份。
- 报表发布时间缺失时按评估日解析；发布时间非法时不猜，直接记未映射。
- 现网切换只做「整批一致 → 单条 UPSERT」，不保留历史现网版本，需要复算时重放原批次。

## 目录

```text
app/
  main.py config.py errors.py reasons.py manifest.py store.py web.py
  modules/m1_ingest … m7_retire      # 每模块：handler + service + 规则文件
  modules/m8_identity                # 身份与名称复用：model + snapshot + service
  templates/                         # base + 8 页
  static/                            # theme/layout/components.css + 4 个 js
docs/  DEPTH.md DATA-DICTIONARY.md DSL-GRAMMAR.md
tests/ conftest.py + 每模块测试 + test_e2e.py + fixtures/
sample_data/
```
