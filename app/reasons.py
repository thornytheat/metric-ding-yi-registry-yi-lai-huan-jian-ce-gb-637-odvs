"""原因码唯一来源：页面、导出、测试都从这里取文案与级别。

一个原因码 = 级别 + 阶段 + 中文结论 + 复核建议。级别三档：
info（记录）、warn（需看一眼）、error（已隔离，不进注册表）。
"""
from __future__ import annotations

from typing import NamedTuple

from . import config

INFO = "info"
WARN = "warn"
ERROR = "error"
LEVEL_ORDER = {ERROR: 0, WARN: 1, INFO: 2}
LEVEL_LABELS = {ERROR: "隔离", WARN: "提示", INFO: "记录"}
LEVEL_MARKS = {ERROR: "x", WARN: "!", INFO: "."}
STAGE_LABELS = config.STAGE_LABELS


class Reason(NamedTuple):
    code: str
    level: str
    stage: str
    title: str
    hint: str


def _r(code, level, stage, title, hint):
    return Reason(code, level, stage, title, hint)


REASONS = {
    # ---- M1 接入 ----
    "E_EMPTY_INPUT": _r(
        "E_EMPTY_INPUT", ERROR, "ingest",
        "输入没有任何字符", "粘贴指标定义或改用样例数据"),
    "E_EMPTY_BLANKS": _r(
        "E_EMPTY_BLANKS", ERROR, "ingest",
        "输入只有空行", "粘贴指标定义或改用样例数据"),
    "E_EMPTY_COMMENTS": _r(
        "E_EMPTY_COMMENTS", ERROR, "ingest",
        "输入只有注释行，没有定义", "写至少一行 name = expr"),
    "E_BOM_STRIPPED": _r(
        "E_BOM_STRIPPED", INFO, "ingest",
        "输入带 UTF-8 BOM，已剥离", "导出给表格软件时自身也会带 BOM"),
    "E_CRLF_NORMALIZED": _r(
        "E_CRLF_NORMALIZED", INFO, "ingest",
        "行尾 CRLF 与 LF 混用，已归一", "统一用 LF 提交可减少差异行"),
    "E_CHARSET_FALLBACK": _r(
        "E_CHARSET_FALLBACK", WARN, "ingest",
        "UTF-8 解码失败，已回退 gb18030", "确认源系统导出编码后统一"),
    "E_DECODE_FAILED": _r(
        "E_DECODE_FAILED", ERROR, "ingest",
        "回退链内所有编码都无法解码", "另存为 UTF-8 后重新提交"),
    # ---- M3 解析诊断 ----
    "Q_NO_ASSIGN": _r(
        "Q_NO_ASSIGN", ERROR, "diagnose",
        "缺少赋值号 =", "定义必须写成 name = expr"),
    "Q_NAME_INVALID": _r(
        "Q_NAME_INVALID", ERROR, "diagnose",
        "指标名不是合法标识符", "指标名只用字母、数字、下划线、汉字，且不能是关键字"),
    "Q_EXPR_EMPTY": _r(
        "Q_EXPR_EMPTY", ERROR, "diagnose",
        "赋值号右侧没有表达式", "补全聚合项或常量"),
    "Q_PAREN_UNBALANCED": _r(
        "Q_PAREN_UNBALANCED", ERROR, "diagnose",
        "括号不配对", "按左右括号数量补齐，注意全角括号已归一"),
    "Q_TOKEN_EXTRA": _r(
        "Q_TOKEN_EXTRA", ERROR, "diagnose",
        "表达式之后还有多余记号", "删除尾部多余分隔符或说明文字"),
    "Q_AGG_ARITY": _r(
        "Q_AGG_ARITY", ERROR, "diagnose",
        "聚合函数参数不是单个字段名", "写成 agg(field)，不嵌套、不带常量"),
    "Q_AGG_UNKNOWN": _r(
        "Q_AGG_UNKNOWN", ERROR, "diagnose",
        "聚合函数不在白名单", "改用 SUM/AVG/MIN/MAX/COUNT/COUNT_DISTINCT"),
    "Q_SENTINEL_VALUE": _r(
        "Q_SENTINEL_VALUE", ERROR, "diagnose",
        "字段位置是空值哨兵", "哨兵不是 0，确认取数字段后再写"),
    "Q_FIELD_UNKNOWN": _r(
        "Q_FIELD_UNKNOWN", ERROR, "diagnose",
        "字段不在字段登记表，别名也未命中", "在字段登记表登记或改用登记名"),
    "Q_WHERE_INVALID": _r(
        "Q_WHERE_INVALID", ERROR, "diagnose",
        "过滤条件不是 field = value", "条件只支持单字段等值"),
    "Q_GROUP_INVALID": _r(
        "Q_GROUP_INVALID", ERROR, "diagnose",
        "分组字段不是可用维度", "分组只用 store_id/channel/category/city/order_date"),
    "Q_DUP_LINE": _r(
        "Q_DUP_LINE", ERROR, "diagnose",
        "与前面某行定义完全相同", "本行隔离，保留首次出现的定义"),
    # ---- M4 依赖图 ----
    "C_DUP_NAME_BLOCK": _r(
        "C_DUP_NAME_BLOCK", ERROR, "graph",
        "同名指标聚合方式不同", "保留首次定义，其余入冲突表"),
    "C_DUP_NAME_REVIEW": _r(
        "C_DUP_NAME_REVIEW", ERROR, "graph",
        "同名指标过滤条件不同", "保留首次定义，冲突交业务确认"),
    "C_DUP_NAME_INFO": _r(
        "C_DUP_NAME_INFO", ERROR, "graph",
        "同名指标分组维度不同", "保留首次定义，下游拼装需知会"),
    "G_SELF_REF": _r(
        "G_SELF_REF", ERROR, "graph",
        "指标引用自身", "改成引用上游基础指标"),
    "G_CYCLE": _r(
        "G_CYCLE", ERROR, "graph",
        "指标依赖成环", "按环路径拆掉一条引用边后重跑"),
    "G_REF_UNKNOWN": _r(
        "G_REF_UNKNOWN", ERROR, "graph",
        "ref 指向未注册的指标", "先注册被引用指标，或改成字段聚合"),
    "G_DEP_UNDECLARED": _r(
        "G_DEP_UNDECLARED", WARN, "graph",
        "表达式引用了指标但未声明依赖", "补 depends 声明，血缘才能对齐"),
    "G_DEP_DECLARED_UNUSED": _r(
        "G_DEP_DECLARED_UNUSED", WARN, "graph",
        "声明了依赖但表达式未使用", "删掉多余声明或补上 ref"),
    # ---- M5 版本 ----
    "V_INTERVAL_OVERLAP": _r(
        "V_INTERVAL_OVERLAP", ERROR, "version",
        "同一指标两个版本的有效期重叠", "收窄较早版本的 valid_to 后重跑"),
    "V_INTERVAL_INVERTED": _r(
        "V_INTERVAL_INVERTED", ERROR, "version",
        "版本有效期起止倒挂或为空区间", "valid_to 必须晚于 valid_from"),
    "V_STALE_SUBMIT": _r(
        "V_STALE_SUBMIT", ERROR, "version",
        "提交版本不晚于库中最新版本", "版本号只能递增，先取新版号再提交"),
    # ---- M6 影响面与导出 ----
    "X_EMPTY_SELECTION": _r(
        "X_EMPTY_SELECTION", WARN, "impact",
        "没有可计算影响面的目标指标", "先解析成功再选择指标"),
    # ---- M7 下线影响与迁移顺序 ----
    "R_NO_TARGETS": _r(
        "R_NO_TARGETS", ERROR, "retire",
        "没有指定下线指标", "选至少一个已注册指标再算"),
    "R_TARGET_UNKNOWN": _r(
        "R_TARGET_UNKNOWN", ERROR, "retire",
        "下线目标未注册", "先在注册表登记，或改成已注册指标"),
    "R_CYCLE_BLOCKED": _r(
        "R_CYCLE_BLOCKED", ERROR, "retire",
        "指标在依赖环内，迁移次序无法确定", "先按环路径拆一条引用边再排"),
    "R_NO_REPLACEMENT": _r(
        "R_NO_REPLACEMENT", ERROR, "retire",
        "没有等价替代，且仍有报表引用", "先补替代指标，再停用旧指标"),
    "R_VERSION_AMBIGUOUS": _r(
        "R_VERSION_AMBIGUOUS", ERROR, "retire",
        "同一指标多个版本同时生效", "收窄较早版本的 valid_to 后重排"),
    "R_REPLACEMENT_PARTIAL": _r(
        "R_REPLACEMENT_PARTIAL", WARN, "retire",
        "只有分组或过滤不同的近似替代", "确认差异后再切换报表"),
    "R_VERSION_NONE": _r(
        "R_VERSION_NONE", WARN, "retire",
        "评估日没有生效版本", "补一条覆盖评估日的版本"),
    "R_REPORT_DANGLING": _r(
        "R_REPORT_DANGLING", WARN, "retire",
        "报表引用的指标未注册", "改指向替代指标，或把报表下架"),
    "R_REQUEST_DUPLICATE": _r(
        "R_REQUEST_DUPLICATE", WARN, "retire",
        "下线请求重复，保留首条", "删掉请求台账里的重复行"),
    "R_RING_MERGED": _r(
        "R_RING_MERGED", INFO, "retire",
        "同一强连通分量只计一个环", "环数按分量计，不按起点重复计"),
    "R_ISOLATED_SKIPPED": _r(
        "R_ISOLATED_SKIPPED", INFO, "retire",
        "孤立指标没有依赖边，不进迁移次序", "孤立指标可直接下线"),
    "B_IDEMPOTENT_REPLAY": _r(
        "B_IDEMPOTENT_REPLAY", INFO, "ingest",
        "输入摘要命中已完成批次，复用结果", "同一输入重跑不新增行"),
    # ---- M8 指标身份与名称复用 ----
    "I_ENTRY_INVALID": _r(
        "I_ENTRY_INVALID", ERROR, "identity",
        "映射条目缺字段或字段不合法", "identity/name/valid_from 必须齐全且名称是合法标识符"),
    "I_INTERVAL_INVERTED": _r(
        "I_INTERVAL_INVERTED", ERROR, "identity",
        "名称区间起止倒挂", "valid_to 必须晚于 valid_from，留空表示未关闭"),
    "I_IDENTITY_OVERLAP": _r(
        "I_IDENTITY_OVERLAP", ERROR, "identity",
        "同一身份的两个名称区间重叠", "同一时刻一个身份只能有一个名称"),
    "I_NAME_REUSE_OVERLAP": _r(
        "I_NAME_REUSE_OVERLAP", ERROR, "identity",
        "同一名称在同一时刻属于两个身份", "名称复用必须前后衔接，不能并行占用"),
    "I_ROUND_CONFLICT": _r(
        "I_ROUND_CONFLICT", ERROR, "identity",
        "整批映射冲突，未切换现网", "修正冲突后换新批次号重新投递"),
    "I_DELIVERY_CONFLICT": _r(
        "I_DELIVERY_CONFLICT", ERROR, "identity",
        "同一批次同一来源重复投递了不同内容", "同批重试只接受原样内容，改内容请换批次号"),
    "I_ACCOUNT_UNBALANCED": _r(
        "I_ACCOUNT_UNBALANCED", ERROR, "identity",
        "身份账目不平", "检查身份、边与报表的映射是否漏项"),
    "I_ROUND_INCOMPLETE": _r(
        "I_ROUND_INCOMPLETE", WARN, "identity",
        "批次尚未收齐，保留上一版本", "等其余 BI 投递到齐后再自动切换"),
    "I_REF_UNMAPPED": _r(
        "I_REF_UNMAPPED", WARN, "identity",
        "引用名称在该定义发布的时点没有身份", "补一条覆盖该时点的名称映射"),
    "I_REPORT_UNMAPPED": _r(
        "I_REPORT_UNMAPPED", WARN, "identity",
        "报表名称在发布时间没有身份", "补映射或人工确认报表口径"),
    "I_REPORT_UNREGISTERED": _r(
        "I_REPORT_UNREGISTERED", WARN, "identity",
        "报表引用的指标既无映射也无定义", "先登记指标，或把报表下架"),
    "I_REPORT_TIME_INVALID": _r(
        "I_REPORT_TIME_INVALID", WARN, "identity",
        "报表发布时间不是合法日期", "统一成 YYYY-MM-DD 后重试"),
    "I_DEFINITION_DUPLICATE": _r(
        "I_DEFINITION_DUPLICATE", WARN, "identity",
        "同一身份有两条定义", "保留当期名称对应的一条，另一条视为历史遗留"),
    "I_METRIC_UNMAPPED": _r(
        "I_METRIC_UNMAPPED", INFO, "identity",
        "指标名称没有任何映射，按名称本身作身份", "登记更名或复用映射后口径更精确"),
    "I_NO_LIVE": _r(
        "I_NO_LIVE", WARN, "identity",
        "还没有整批一致的身份映射", "先投递齐一个批次"),
    "I_ENTRY_DUPLICATE": _r(
        "I_ENTRY_DUPLICATE", INFO, "identity",
        "完全相同的映射条目已合并", "同身份同名称同区间只保留一条"),
    "I_NAME_REUSED": _r(
        "I_NAME_REUSED", INFO, "identity",
        "名称被后来的身份复用", "旧报表按发布时间解析到旧身份，不并入新身份"),
    "I_IDENTITY_RENAMED": _r(
        "I_IDENTITY_RENAMED", INFO, "identity",
        "同一身份改了名称", "身份不变，按名称区间解析历史口径"),
    "I_ROUND_PROMOTED": _r(
        "I_ROUND_PROMOTED", INFO, "identity",
        "整批一致，已原子切换现网", "现网依赖与影响面整体替换为新版本"),
    "I_ROUND_REPLAY": _r(
        "I_ROUND_REPLAY", INFO, "identity",
        "同一批同来源原样重试，不新增结果", "重试命中已存投递，直接返回原状态"),
    "I_ROUND_SUPERSEDED": _r(
        "I_ROUND_SUPERSEDED", INFO, "identity",
        "现网已切换到更晚的批次", "重放旧批次不覆盖现网，避免回退"),
    "I_IDENTITY_NO_DEFINITION": _r(
        "I_IDENTITY_NO_DEFINITION", INFO, "identity",
        "身份有名称映射但没有当前定义", "旧口径仍可解析，当前定义可能在其它 BI"),
    "I_NAME_STALE": _r(
        "I_NAME_STALE", INFO, "identity",
        "定义使用的名称已不在当期", "按该名称最后所属的身份解析，避免并入复用者"),
    "I_ALIAS_SELF_REF": _r(
        "I_ALIAS_SELF_REF", INFO, "identity",
        "迁移别名指向同一身份的旧名称", "同一身份的两个名称互指不构成自环"),
    "I_DEFINITION_ALIAS": _r(
        "I_DEFINITION_ALIAS", INFO, "identity",
        "同一身份的新旧定义并存，已保留当期名称的一条", "迁移完成后删掉旧名称的定义"),
}


def get(code: str) -> Reason:
    return REASONS.get(code, Reason(code, WARN, "unknown", code, "未登记的原因码"))


def level_of(code: str) -> str:
    return get(code).level


def label(code: str) -> str:
    return get(code).title


def hint_of(code: str) -> str:
    return get(code).hint


def stage_of(code: str) -> str:
    return get(code).stage


def is_error(code: str) -> bool:
    return level_of(code) == ERROR


def catalog() -> list[dict]:
    rows = sorted(REASONS.values(), key=lambda r: (r.stage, LEVEL_ORDER[r.level], r.code))
    return [
        {"code": r.code, "level": r.level, "stage": r.stage,
         "stage_label": STAGE_LABELS.get(r.stage, r.stage),
         "level_label": LEVEL_LABELS[r.level], "title": r.title, "hint": r.hint}
        for r in rows
    ]
