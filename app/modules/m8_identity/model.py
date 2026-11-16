"""M8 纯规则：名称分段、整批核对、按时点解析身份。

一条映射是「某身份在某段时间里叫某个名称」，区间半开 `[valid_from, valid_to)`。
更名与名称复用都用同一套分段表达，不额外造第二套概念：

- 更名：同一身份前后两段名称，前段 `valid_to` 等于后段 `valid_from`；
- 名称复用：不同身份先后使用同一名称，且前者 `valid_to` 不晚于后者 `valid_from`。

关键约束：**身份只由 identity 决定，名称只在指定日期上解析**。任何「把同名
分段并进同一身份」的写法，都会让后来复用名称的新指标顶替旧身份，在依赖图上
凭空造出自环/环（TRAP-29）。因此本模块只提供两个方向的查询：

- `resolve_name(name, day)` → 该日该名称属于哪个身份（旧报表按发布时间用）；
- `name_at(identity, day)` → 该日该身份叫什么名字（展示与新版定义用）。

校验只做两件事：同一身份的名称区间不许重叠；同一名称在同一时刻不许属于两个
身份。相邻分段端点相接是合法的，不受影响。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re

from ... import config, reasons
from ..m2_syntax import coerce
from ..m5_versions import intervals

IDENTITY_RE = re.compile(r"^[A-Za-z0-9_.\-一-鿿:]+$")
ROUND_RE = re.compile(r"^[A-Za-z0-9_.\-一-鿿]+$")
NAME_PREFIX = "name:"
OPEN_END = config.OPEN_END


def rank(code: str) -> int:
    try:
        return config.DIAG_ORDER.index(code)
    except ValueError:
        return len(config.DIAG_ORDER)


class PayloadError(ValueError):
    """投递结构错误；带原因码，接口层直接转 400。"""

    def __init__(self, message: str, code: str = "I_ENTRY_INVALID"):
        super().__init__(message)
        self.code = code


def diag(code: str, *, source: str | None = None, identity: str | None = None,
         name: str | None = None, report_id: str | None = None,
         detail: str = "") -> dict:
    reason = reasons.get(code)
    return {"code": code, "level": reason.level, "stage": reason.stage,
            "source": source, "identity": identity, "name": name,
            "report_id": report_id, "detail": detail or reason.title,
            "rank": rank(code)}


def sort_diags(diags: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out: list[dict] = []
    for item in diags:
        key = (item["code"], item.get("identity") or "",
               item.get("name") or "", item.get("report_id") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return sorted(out, key=lambda item: (
        item.get("rank", len(config.DIAG_ORDER)), item["code"],
        item.get("identity") or "", item.get("name") or "",
        item.get("report_id") or ""))


# ---------------- 投递解析 ----------------
def _text(raw, key: str) -> str:
    if not isinstance(raw, dict):
        return ""
    return str(raw.get(key) or "").strip()


def split_sources(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        parts = [str(item) for item in raw]
    else:
        parts = re.split(r"[,，、]", str(raw))
    out: list[str] = []
    for part in parts:
        body = part.strip()
        if body and body not in out:
            out.append(body)
    return out


def _parse_entry(raw: dict, seq: int, source: str) -> dict:
    identity = _text(raw, "identity")
    name = _text(raw, "name")
    entry_source = _text(raw, "source") or source
    if not identity or not IDENTITY_RE.match(identity):
        raise PayloadError(f"第 {seq} 条映射的 identity 缺失或含非法字符：{identity or '空'}")
    if identity.startswith(NAME_PREFIX):
        # `name:` 前缀是兜底身份的保留命名，外部映射不许占用，避免两者撞号
        raise PayloadError(f"第 {seq} 条映射的 identity 使用了保留前缀 "
                           f"{NAME_PREFIX}：{identity}")
    if not name or not coerce.is_identifier(name):
        raise PayloadError(f"第 {seq} 条映射的 name 不是合法指标名：{name or '空'}")
    start = intervals.parse_date(_text(raw, "valid_from"))
    if start is None:
        raise PayloadError(f"第 {seq} 条映射的 valid_from 缺失或不是日期："
                           f"{_text(raw, 'valid_from') or '空'}")
    end = intervals.parse_date(_text(raw, "valid_to"))
    if end is not None and end <= start:
        raise PayloadError(
            f"第 {seq} 条映射的 valid_to 不晚于 valid_from："
            f"{start.isoformat()} ~ {end.isoformat()}", code="I_INTERVAL_INVERTED")
    return {
        "seq": seq,
        "identity": identity,
        "name": name,
        "source": entry_source,
        "start": start,
        "end": end or dt.date.max,
        "valid_from": start.isoformat(),
        "valid_to": end.isoformat() if end else "",
    }


def _parse_report(raw: dict, seq: int) -> dict:
    report_id = _text(raw, "report_id")
    metric = _text(raw, "metric")
    if not report_id or not metric:
        raise PayloadError(f"第 {seq} 条报表缺 report_id 或 metric")
    published = _text(raw, "published_on")
    return {"report_id": report_id,
            "report_name": _text(raw, "report_name") or report_id,
            "metric": metric,
            "weight": _text(raw, "weight") or "0",
            "published_on": published}


def parse_payload(payload) -> dict:
    """把一次投递整理成可落库的结构；结构问题抛 ValueError（接口层转 400）。"""
    if not isinstance(payload, dict):
        raise PayloadError("请求体必须是对象")
    round_id = _text(payload, "round") or _text(payload, "round_no")
    if not round_id or not ROUND_RE.match(round_id):
        raise PayloadError("round 缺失或不是合法批次号")
    source = _text(payload, "source")
    if not source:
        raise PayloadError("source 缺失：每次投递必须说明来自哪个 BI")
    raw_entries = payload.get("entries")
    if raw_entries is None:
        raw_entries = []
    if not isinstance(raw_entries, list):
        raise PayloadError("entries 必须是数组")
    entries: list[dict] = []
    for seq, raw in enumerate(raw_entries, start=1):
        if not isinstance(raw, dict):
            raise PayloadError(f"第 {seq} 条映射不是对象")
        entries.append(_parse_entry(raw, seq, source))
    raw_reports = payload.get("reports")
    if raw_reports is None:
        raw_reports = []
    if not isinstance(raw_reports, list):
        raise PayloadError("reports 必须是数组")
    reports: list[dict] = []
    for seq, raw in enumerate(raw_reports, start=1):
        if not isinstance(raw, dict):
            raise PayloadError(f"第 {seq} 条报表不是对象")
        reports.append(_parse_report(raw, seq))
    expected = split_sources(payload.get("expected_sources"))
    if source not in expected:
        expected.append(source)
    return {"round_id": round_id, "source": source,
            "expected_sources": expected, "entries": entries,
            "reports": reports}


def entry_signature(entry: dict) -> dict:
    return {"identity": entry["identity"], "name": entry["name"],
            "valid_from": entry["valid_from"], "valid_to": entry["valid_to"]}


def payload_digest(parsed: dict) -> str:
    """同批同来源的投递指纹：内容一致即重试，不一致即冲突。"""
    canonical = {
        "round": parsed["round_id"],
        "source": parsed["source"],
        "expected_sources": sorted(parsed["expected_sources"]),
        "entries": sorted(
            (entry_signature(entry) for entry in parsed["entries"]),
            key=lambda item: (item["identity"], item["name"],
                              item["valid_from"], item["valid_to"])),
        "reports": sorted(parsed.get("reports") or [],
                          key=lambda item: (item["report_id"], item["metric"],
                                            item["published_on"])),
    }
    blob = json.dumps(canonical, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def stored_entries(entries: list[dict]) -> list[dict]:
    return [entry_signature(entry) for entry in entries]


def rows_to_entries(rows: list[dict]) -> list[dict]:
    """存储/快照行 → 解析用条目；OPEN_END 还原成上限日期，语义与校验期一致。"""
    entries: list[dict] = []
    for row in rows:
        start = dt.date.fromisoformat(row["valid_from"])
        raw_end = row.get("valid_to") or ""
        if not raw_end or raw_end == OPEN_END:
            end = dt.date.max
        else:
            end = dt.date.fromisoformat(raw_end)
        entries.append({
            "seq": row.get("seq", 0),
            "identity": row["identity"],
            "name": row["name"],
            "source": row.get("source", ""),
            "start": start,
            "end": end,
            "valid_from": row["valid_from"],
            "valid_to": "" if end == dt.date.max else end.isoformat(),
        })
    return entries


# ---------------- 整批核对 ----------------
def _overlap(first: dict, second: dict) -> bool:
    return first["start"] < second["end"] and second["start"] < first["end"]


def _dedupe(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    seen: dict[tuple, dict] = {}
    duplicates = 0
    for entry in entries:
        key = (entry["identity"], entry["name"],
               entry["valid_from"], entry["valid_to"])
        if key in seen:
            duplicates += 1
            continue
        seen[key] = entry
    diags: list[dict] = []
    if duplicates:
        first = next(iter(seen.values()))
        diags.append(diag(
            "I_ENTRY_DUPLICATE", source=first["source"],
            identity=first["identity"], name=first["name"],
            detail=f"{duplicates} 条完全相同的映射已合并"))
    ordered = sorted(seen.values(), key=lambda item: (
        item["name"], item["start"], item["identity"]))
    return ordered, diags


def _transitions(items: list[dict], field: str) -> list[tuple]:
    """按时间排序后取相邻且取值发生变化的段对，后一段起点即变化时点。"""
    ordered = sorted(items, key=lambda item: (item["start"], item[field]))
    pairs = []
    for first, second in zip(ordered, ordered[1:]):
        if first[field] != second[field]:
            pairs.append((first, second))
    return pairs


def validate(entries: list[dict]) -> dict:
    """整批核对：合并完全重复的条目，再查身份区间与名称占用的冲突。"""
    cleaned, diags = _dedupe(entries)

    by_identity: dict[str, list[dict]] = {}
    by_name: dict[str, list[dict]] = {}
    for entry in cleaned:
        by_identity.setdefault(entry["identity"], []).append(entry)
        by_name.setdefault(entry["name"], []).append(entry)

    for identity, group in sorted(by_identity.items()):
        ordered = sorted(group, key=lambda item: (item["start"], item["name"]))
        for index, first in enumerate(ordered):
            for second in ordered[index + 1:]:
                if not _overlap(first, second):
                    continue
                diags.append(diag(
                    "I_IDENTITY_OVERLAP", source=second["source"],
                    identity=identity, name=second["name"],
                    detail=f"{identity} 在 {second['valid_from']} 前后同时叫 "
                           f"{first['name']} 与 {second['name']}"))

    for name, group in sorted(by_name.items()):
        ordered = sorted(group, key=lambda item: (item["start"], item["identity"]))
        for index, first in enumerate(ordered):
            for second in ordered[index + 1:]:
                if not _overlap(first, second):
                    continue
                if first["identity"] == second["identity"]:
                    continue
                diags.append(diag(
                    "I_NAME_REUSE_OVERLAP", source=second["source"],
                    identity=second["identity"], name=name,
                    detail=f"名称 {name} 在 {second['valid_from']} 前后同时属于 "
                           f"{first['identity']} 与 {second['identity']}"))
        for first, second in _transitions(ordered, "identity"):
            diags.append(diag(
                "I_NAME_REUSED", source=second["source"],
                identity=second["identity"], name=name,
                detail=f"名称 {name} 自 {second['valid_from']} 起由 "
                       f"{second['identity']} 复用，此前属于 {first['identity']}"))

    for identity, group in sorted(by_identity.items()):
        ordered = sorted(group, key=lambda item: (item["start"], item["name"]))
        for first, second in _transitions(ordered, "name"):
            diags.append(diag(
                "I_IDENTITY_RENAMED", source=second["source"],
                identity=identity, name=second["name"],
                detail=f"{identity} 自 {second['valid_from']} 起由 "
                       f"{first['name']} 更名为 {second['name']}"))

    diags = sort_diags(diags)
    return {"segments": cleaned, "diagnostics": diags,
            "has_error": any(item["level"] == reasons.ERROR for item in diags)}


# ---------------- 按时点解析 ----------------
def name_index(segments: list[dict]) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    for segment in segments:
        index.setdefault(segment["name"], []).append(segment)
    for key in index:
        index[key] = sorted(index[key], key=lambda item: item["start"])
    return index


def identity_index(segments: list[dict]) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    for segment in segments:
        index.setdefault(segment["identity"], []).append(segment)
    for key in index:
        index[key] = sorted(index[key], key=lambda item: item["start"])
    return index


def covers(segment: dict, day: dt.date) -> bool:
    return segment["start"] <= day < segment["end"]


def resolve_name(index: dict[str, list[dict]], name: str,
                 day: dt.date) -> dict | None:
    """名称在指定日期上解析到唯一身份；半开区间保证相邻分段不会两头都命中。"""
    for segment in index.get(name, []):
        if covers(segment, day):
            return segment
    return None


def name_at(index: dict[str, list[dict]], identity: str,
            day: dt.date) -> str | None:
    """身份在指定日期的名称；当天没有覆盖分段时退到最近一段，便于展示。"""
    segments = index.get(identity, [])
    if not segments:
        return None
    for segment in segments:
        if covers(segment, day):
            return segment["name"]
    past = [segment for segment in segments if segment["start"] <= day]
    return (past[-1] if past else segments[-1])["name"]


def stale_segment(index: dict[str, list[dict]], name: str,
                  day: dt.date) -> dict | None:
    """名称当天没有分段覆盖，但更早存在过：退到最后一个早于该日的分段。"""
    past = [segment for segment in index.get(name, [])
            if segment["start"] <= day]
    return past[-1] if past else None


def fallback_identity(name: str) -> str:
    """没有任何映射时，名称本身充当身份；显式前缀避免和解复用身份混淆。"""
    return NAME_PREFIX + name


def is_fallback(identity: str) -> bool:
    return identity.startswith(NAME_PREFIX)
