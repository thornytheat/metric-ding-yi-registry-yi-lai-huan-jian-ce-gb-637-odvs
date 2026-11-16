"""现网快照：把已核对通过的名称分段与当前定义批次合成身份空间的依赖与影响面。

三件事分开做，互不借用对方的中间结果：

1. **定义 → 身份**：每条定义按名称在评估日解析到身份；名称已改走的退到最后一个
   旧分段（stale），从未登记过的用 `name:<名称>` 兜底。身份一旦确定，后续所有
   引用与报表都只用身份编号，不再用名称做节点键——这是不伪造依赖环的关键。
2. **报表 → 身份**：旧报表有发布时间，按发布时间解析；新报表没有发布时间，按
   评估日解析。名称在发布时间没有任何分段时**不**退到后来复用它的身份，而是记为
   未映射，避免旧报表被并进新身份。
3. **身份图 → 影响面**：身份→身份边做反向可达，报表按绑定身份计入权重合计；
   影响分仍是「下游身份数 × 权重合计」，Decimal 两位。

快照只输出可 JSON 化的结构，时间一律 ISO 字符串、权重一律字符串。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal

from ... import config
from ..m4_graph import tarjan
from ..m5_versions import intervals as version_intervals
from . import model


def _dec(value) -> Decimal:
    return Decimal(str(value)).quantize(config.DECIMAL_QUANT)


def _resolve_context(index: dict[str, list[dict]], name: str,
                     day: dt.date) -> tuple[str, str, dict | None]:
    """定义/引用使用的名称：当前 → 旧段（stale）→ 兜底身份（future/unmapped）。"""
    segment = model.resolve_name(index, name, day)
    if segment is not None:
        return segment["identity"], "current", segment
    stale = model.stale_segment(index, name, day)
    if stale is not None:
        return stale["identity"], "stale", stale
    if name in index:
        return model.fallback_identity(name), "future", None
    return model.fallback_identity(name), "unmapped", None


def _resolve_report(index: dict[str, list[dict]], name: str,
                    day: dt.date) -> tuple[str | None, str]:
    """报表名称：严格按发布时口径；名称已无历史分段时退到兜底身份。"""
    segment = model.resolve_name(index, name, day)
    if segment is not None:
        return segment["identity"], "segment"
    if name in index:
        return None, "unmapped"
    return model.fallback_identity(name), "fallback"


def _definition_identity(index: dict[str, list[dict]], name: str,
                         day: dt.date) -> tuple[str, str, dict | None]:
    return _resolve_context(index, name, day)


def _definition_day(identity: str, name: str, state: str, segment: dict | None,
                    iindex: dict[str, list[dict]], day: dt.date) -> dt.date:
    """定义里引用名称的解析时点：定义写作时所用名称生效的那个时点。

    当期名称 → 评估日；旧名称（stale）→ 该名称最后仍生效的那天，而不是评估日。
    这一步是「不伪造环」的关键：老定义里的 ref 若按评估日解析，会被改写成后来
    复用同名的身份，凭空连出一条本不存在的边。
    """
    if state == "current" or segment is None:
        return day
    same_name = [seg for seg in iindex.get(identity, []) if seg["name"] == name]
    if not same_name:
        return day
    last = max(same_name, key=lambda seg: seg["start"])
    if last["end"] == dt.date.max:
        return day
    return min(last["end"] - dt.timedelta(days=1), day)


def _keep_definitions(metrics: list[dict], nindex: dict[str, list[dict]],
                      iindex: dict[str, list[dict]], day: dt.date) -> dict:
    """同名多定义合并到身份：优先当期名称，其次行号最小。"""
    candidates: list[dict] = []
    diags: list[dict] = []
    for metric in sorted(metrics, key=lambda item: item["line_no"]):
        identity, state, segment = _definition_identity(nindex, metric["name"], day)
        candidates.append({
            "identity": identity,
            "name": metric["name"],
            "line_no": metric["line_no"],
            "name_state": state,
            "segment": segment,
            "ref_day": _definition_day(identity, metric["name"], state, segment,
                                       iindex, day),
            "metric": metric,
        })

    by_identity: dict[str, list[dict]] = {}
    for item in candidates:
        by_identity.setdefault(item["identity"], []).append(item)

    kept: list[dict] = []
    dropped: list[dict] = []
    for identity in sorted(by_identity):
        group = by_identity[identity]
        current_name = model.name_at(iindex, identity, day)
        preferred = [item for item in group if item["name"] == current_name]
        pool = preferred or group
        keeper = min(pool, key=lambda item: item["line_no"])
        kept.append(keeper)
        for item in group:
            if item is keeper:
                continue
            dropped.append({"identity": identity, "name": item["name"],
                            "line_no": item["line_no"],
                            "kept_line": keeper["line_no"],
                            "kept_name": keeper["name"]})
            code = ("I_DEFINITION_ALIAS"
                    if len(group) > 1 and item["name"] != keeper["name"]
                    else "I_DEFINITION_DUPLICATE")
            diags.append(model.diag(
                code, identity=identity, name=item["name"],
                detail=f"身份 {identity} 同时有 {item['name']}（第 {item['line_no']} 行）"
                       f"与 {keeper['name']}（第 {keeper['line_no']} 行），"
                       f"保留当期名称对应的一条"))

    for item in candidates:
        if item["name_state"] == "stale":
            diags.append(model.diag(
                "I_NAME_STALE", identity=item["identity"], name=item["name"],
                detail=f"定义使用的名称 {item['name']} 已不在当期，"
                       f"按最后所属身份 {item['identity']} 解析"))
        elif item["name_state"] in {"future", "unmapped"}:
            diags.append(model.diag(
                "I_METRIC_UNMAPPED", identity=item["identity"], name=item["name"],
                detail=f"名称 {item['name']} 没有覆盖评估日的映射，"
                       f"按名称本身作身份 {item['identity']}"))
    return {"kept": kept, "dropped": dropped, "diagnostics": diags}


def _identity_edges(kept: list[dict], nindex: dict[str, list[dict]],
                    iindex: dict[str, list[dict]],
                    day: dt.date) -> tuple[list[dict], list[dict]]:
    edges: dict[tuple[str, str], dict] = {}
    diags: list[dict] = []
    aliases: list[dict] = []
    for item in kept:
        source = item["identity"]
        ref_day = item.get("ref_day", day)
        own_names = {segment["name"] for segment in iindex.get(source, [])}
        for target_name in sorted(set(item["metric"].get("metrics") or [])):
            if target_name in own_names and target_name != item["name"]:
                # 更名过渡期的迁移别名：引用的名称是本身份自己的旧名称。
                # 按名称在评估日再解析一次会被后来复用同名的另一个身份接走，
                # 于是 A→B 与 B→A 两条假边拼成一个假环。这里直接按同一身份合并。
                aliases.append({"identity": source, "name": item["name"],
                                "target_name": target_name,
                                "resolved_on": ref_day.isoformat()})
                continue
            target, state, _segment = _resolve_context(nindex, target_name, ref_day)
            edges[(source, target)] = {"src": source, "dst": target,
                                       "kind": "identity_identity",
                                       "via": target_name,
                                       "resolved_on": ref_day.isoformat()}
            if state == "future":
                diags.append(model.diag(
                    "I_REF_UNMAPPED", identity=source, name=target_name,
                    detail=f"定义 {item['name']} 引用的 {target_name} 在 "
                           f"{ref_day.isoformat()} 尚无身份，按名称本身作身份 "
                           f"{target}，不并入后来复用者"))
    if aliases:
        first = aliases[0]
        diags.append(model.diag(
            "I_ALIAS_SELF_REF", identity=first["identity"], name=first["name"],
            detail=f"{len(aliases)} 条引用指向本身份的旧名称（如 "
                   f"{first['name']} → {first['target_name']}），"
                   f"按同一身份合并，不生成依赖边"))
    return ([edges[key] for key in sorted(edges)], diags)


def _report_bindings(report_rows: list[dict], nindex: dict[str, list[dict]],
                     definitions: dict[str, dict], day: dt.date) -> dict:
    bindings: list[dict] = []
    diags: list[dict] = []
    for row in sorted(report_rows, key=lambda item: item["report_id"]):
        name = row["metric"]
        published = (row.get("published_on") or "").strip()
        identity = None
        has_definition = False
        if published:
            day_used = version_intervals.parse_date(published)
            matched_on = "published_on"
            if day_used is None:
                # 发布时间坏掉时绝不退到评估日：那会把旧报表并进后来复用名称的新身份
                diags.append(model.diag(
                    "I_REPORT_TIME_INVALID", report_id=row["report_id"], name=name,
                    detail=f"报表 {row['report_id']} 的发布时间 {published} 不是日期，"
                           f"未绑定，避免误并到复用同一名称的新身份"))
                bindings.append({
                    "report_id": row["report_id"],
                    "report_name": row["report_name"], "name": name,
                    "published_on": published, "resolved_on": "",
                    "matched_on": matched_on, "time_valid": False,
                    "weight": str(_dec(row["weight"])), "identity": None,
                    "identity_name": None, "has_definition": False,
                    "status": "unmapped",
                })
                continue
        else:
            day_used = day
            matched_on = "current"
        identity, how = _resolve_report(nindex, name, day_used)
        has_definition = identity in definitions
        if identity is None:
            status = "unmapped"
            diags.append(model.diag(
                "I_REPORT_UNMAPPED", report_id=row["report_id"], name=name,
                detail=f"报表 {row['report_id']} 发布时 {day_used.isoformat()} "
                       f"名称 {name} 没有身份，未绑定"))
        elif how == "fallback":
            status = "bound" if has_definition else "unregistered"
            if not has_definition:
                diags.append(model.diag(
                    "I_REPORT_UNREGISTERED", report_id=row["report_id"], name=name,
                    detail=f"报表 {row['report_id']} 引用的 {name} 没有映射也没有定义"))
        else:
            status = "bound"
        bindings.append({
            "report_id": row["report_id"],
            "report_name": row["report_name"],
            "name": name,
            "published_on": published,
            "resolved_on": day_used.isoformat(),
            "matched_on": matched_on,
            "time_valid": True,
            "weight": str(_dec(row["weight"])),
            "identity": identity,
            "identity_name": (model.name_at(nindex, identity, day_used)
                              if identity else None),
            "has_definition": has_definition,
            "status": status,
        })
    return {"rows": bindings, "diagnostics": diags}


def _components(names: list[str], adj: dict[str, list[str]]) -> list[dict]:
    components: list[dict] = []
    for order, comp in enumerate(tarjan.strongly_connected(sorted(names), adj)):
        cyclic = tarjan.is_cycle(comp, adj)
        components.append({
            "comp_id": order,
            "size": len(comp),
            "members": comp,
            "is_cycle": cyclic,
            "path": tarjan.cycle_path(comp, adj) if cyclic else [],
        })
    return components


def _impact(identities: list[str], edges: list[dict], bindings: list[dict]) -> list[dict]:
    reverse: dict[str, list[str]] = {}
    for edge in edges:
        reverse.setdefault(edge["dst"], []).append(edge["src"])
    for key in reverse:
        reverse[key] = sorted(set(reverse[key]))

    bound_by_identity: dict[str, list[dict]] = {}
    for row in bindings:
        if row["status"] == "bound" and row["identity"]:
            bound_by_identity.setdefault(row["identity"], []).append(row)

    results: list[dict] = []
    for identity in sorted(identities):
        seen: set[str] = set()
        queue = [identity]
        while queue:
            node = queue.pop()
            for parent in reverse.get(node, []):
                if parent in seen or parent == identity:
                    continue
                seen.add(parent)
                queue.append(parent)
        reach = seen | {identity}
        hits = [row for member in sorted(reach)
                for row in bound_by_identity.get(member, [])]
        hits.sort(key=lambda item: item["report_id"])
        weight = _dec(sum((Decimal(row["weight"]) for row in hits), Decimal("0")))
        score = _dec(Decimal(len(seen)) * weight)
        results.append({
            "identity": identity,
            "downstream": sorted(seen),
            "downstream_count": len(seen),
            "reports": [{"report_id": row["report_id"],
                         "report_name": row["report_name"],
                         "name": row["name"],
                         "published_on": row["published_on"],
                         "weight": row["weight"]} for row in hits],
            "report_count": len(hits),
            "weight_sum": str(weight),
            "score": str(score),
        })
    return results


def _accounts(metrics_total: int, kept: list[dict], dropped: list[dict],
              bindings: list[dict], edges: list[dict], report_edges: list[dict],
              identities: list[dict], cycles: list[dict]) -> dict:
    line_terms = [
        {"label": "保留身份定义", "value": len(kept)},
        {"label": "合并丢弃定义", "value": len(dropped)},
    ]
    report_counts = {"bound": 0, "unmapped": 0, "unregistered": 0}
    for row in bindings:
        report_counts[row["status"]] = report_counts.get(row["status"], 0) + 1
    report_terms = [
        {"label": "已绑定", "value": report_counts["bound"]},
        {"label": "未映射", "value": report_counts["unmapped"]},
        {"label": "未注册", "value": report_counts["unregistered"]},
    ]
    edge_terms = [
        {"label": "身份→身份", "value": len(edges)},
        {"label": "报表→身份", "value": len(report_edges)},
    ]
    cycle_members = sum(component["size"] for component in cycles)
    identity_terms = [
        {"label": "环内身份", "value": cycle_members},
        {"label": "非环身份", "value": len(identities) - cycle_members},
    ]
    rows = []
    for label, total, terms in (
        ("定义账", metrics_total, line_terms),
        ("报表账", len(bindings), report_terms),
        ("边账", len(edges) + len(report_edges), edge_terms),
        ("身份账", len(identities), identity_terms),
    ):
        terms_total = sum(term["value"] for term in terms)
        rows.append({"account": label, "total": total, "terms": terms,
                     "balanced": terms_total == total})
    return {"accounts": rows,
            "all_balanced": all(row["balanced"] for row in rows)}


def build(*, segments: list[dict], metrics: list[dict], report_rows: list[dict],
          eval_day: dt.date) -> dict:
    nindex = model.name_index(segments)
    iindex = model.identity_index(segments)

    kept_result = _keep_definitions(metrics, nindex, iindex, eval_day)
    kept = kept_result["kept"]
    definitions = {item["identity"]: item for item in kept}
    edges, edge_diags = _identity_edges(kept, nindex, iindex, eval_day)
    report_result = _report_bindings(report_rows, nindex, definitions, eval_day)
    bindings = report_result["rows"]

    adj: dict[str, list[str]] = {}
    for edge in edges:
        adj.setdefault(edge["src"], []).append(edge["dst"])
        adj.setdefault(edge["dst"], [])
    for key in adj:
        adj[key] = sorted(set(adj[key]))

    segment_identities = set(iindex)
    definition_identities = set(definitions)
    identity_names = sorted(segment_identities | definition_identities | set(adj))
    identities: list[dict] = []
    for identity in identity_names:
        history = [{"name": segment["name"],
                    "valid_from": segment["valid_from"],
                    "valid_to": segment["valid_to"]}
                   for segment in iindex.get(identity, [])]
        identities.append({
            "identity": identity,
            "name_today": model.name_at(iindex, identity, eval_day),
            "has_definition": identity in definitions,
            "definition_name": definitions[identity]["name"] if identity in definitions else None,
            "names": history,
        })
        if identity not in definitions and iindex.get(identity):
            today = identities[-1]["name_today"]
            resolved = model.resolve_name(nindex, today, eval_day) if today else None
            if resolved is not None and resolved["identity"] == identity:
                kept_result["diagnostics"].append(model.diag(
                    "I_IDENTITY_NO_DEFINITION", identity=identity, name=today,
                    detail=f"身份 {identity} 当期名称 {today} 没有定义，"
                           f"旧口径仍可按发布时解析"))

    components = _components(list(adj), adj)
    cycles = [component for component in components if component["is_cycle"]]

    report_edges = [
        {"src": f"report:{row['report_id']}", "dst": f"identity:{row['identity']}",
         "kind": "report_identity"}
        for row in bindings if row["status"] == "bound" and row["identity"]
    ]
    report_edges.sort(key=lambda item: (item["src"], item["dst"]))

    impact_rows = _impact(identity_names, edges, bindings)
    impact_by_identity = {row["identity"]: row for row in impact_rows}

    definitions_payload = []
    for item in sorted(kept, key=lambda entry: entry["metric"]["line_no"]):
        metric = item["metric"]
        impact = impact_by_identity.get(item["identity"], {})
        definitions_payload.append({
            "identity": item["identity"],
            "name": item["name"],
            "name_today": model.name_at(iindex, item["identity"], eval_day),
            "name_state": item["name_state"],
            "line_no": metric["line_no"],
            "expr_text": metric.get("expr_text", ""),
            "fields": sorted(metric.get("fields") or []),
            "group_by": list(metric.get("group_by") or []),
            "aggregators": sorted(metric.get("aggregators") or []),
            "depends_declared": list(metric.get("depends_declared") or []),
            "refs": sorted(set(metric.get("metrics") or [])),
            "downstream_count": impact.get("downstream_count", 0),
            "report_count": impact.get("report_count", 0),
            "weight_sum": impact.get("weight_sum", "0.00"),
            "score": impact.get("score", "0.00"),
        })

    diagnostics = model.sort_diags(
        kept_result["diagnostics"] + edge_diags + report_result["diagnostics"])
    accounts = _accounts(len(metrics), kept, kept_result["dropped"], bindings,
                         edges, report_edges, identities, cycles)
    if not accounts["all_balanced"]:
        diagnostics.append(model.diag("I_ACCOUNT_UNBALANCED",
                                      detail="身份快照四账不平，拒绝切换"))
        accounts["all_balanced"] = False

    counters = {
        "segments": len(segments),
        "identities": len(identities),
        "identities_with_definition": sum(
            1 for item in identities if item["has_definition"]),
        "definitions": len(kept),
        "definitions_dropped": len(kept_result["dropped"]),
        "edges": len(edges),
        "report_edges": len(report_edges),
        "cycles": len(cycles),
        "cycle_identities": sum(component["size"] for component in cycles),
        "reports": len(bindings),
        "reports_bound": sum(1 for row in bindings if row["status"] == "bound"),
        "reports_unmapped": sum(1 for row in bindings if row["status"] == "unmapped"),
        "reports_unregistered": sum(
            1 for row in bindings if row["status"] == "unregistered"),
        "renamed_identities": sum(1 for item in identities
                                  if len({seg["name"] for seg in item["names"]}) > 1),
        "reused_names": len({segment["name"] for segment in segments
                             if len({seg["identity"] for seg in segments
                                     if seg["name"] == segment["name"]}) > 1}),
    }
    return {
        "eval_date": eval_day.isoformat(),
        "segments": [{"identity": segment["identity"], "name": segment["name"],
                      "valid_from": segment["valid_from"],
                      "valid_to": segment["valid_to"] or config.OPEN_END,
                      "source": segment.get("source", "")}
                     for segment in segments],
        "identities": identities,
        "definitions": definitions_payload,
        "dropped_definitions": kept_result["dropped"],
        "edges": edges,
        "report_edges": report_edges,
        "components": components,
        "cycles": cycles,
        "reports": bindings,
        "impact": impact_rows,
        "accounts": accounts,
        "diagnostics": diagnostics,
        "counters": counters,
    }


def digest(snapshot: dict) -> str:
    """快照内容指纹：不含生成时间，同内容重复切换得到同一指纹。"""
    blob = json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                      default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
