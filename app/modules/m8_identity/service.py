"""M8 编排：逐批投递 → 整批核对 → 原子切换现网。

状态机只有三格，任何一格失败都停在上一版：

```
   pending（缺来源）──齐了且一致──▶ promoted（现网=本批快照）
        │                                   │
        └────────冲突/不平──────────────────▶ 保留上一版
```

三条硬规则：

- **整批一致才切换**：批次声明的来源必须全部到齐（缺一个就 `I_ROUND_INCOMPLETE`），
  合并后的分段再过一遍区间校验（重叠即 `I_ROUND_CONFLICT`），账户必须平。
- **切换是单条写入**：`identity_live` 只有一个槽位，一次 UPSERT 完成替换；
  快照内容相同则指纹相同，重放不会产生第二个结果。
- **同批同来源重试不新增结果**：指纹一致直接返回原投递记录（`I_ROUND_REPLAY`）；
  内容不同则按 `I_DELIVERY_CONFLICT` 拒绝，不覆盖已收到的内容。
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from ... import config, errors, reasons
from . import model, snapshot


def _now() -> str:
    return dt.datetime.now(config.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _round_rows(store, round_id: str) -> list[dict]:
    return store.list_deliveries(round_id)


def _combined_entries(deliveries: list[dict]) -> list[dict]:
    entries: list[dict] = []
    seq = 0
    for delivery in deliveries:
        for raw in delivery["entries"]:
            seq += 1
            start = dt.date.fromisoformat(raw["valid_from"])
            end = (dt.date.fromisoformat(raw["valid_to"])
                   if raw.get("valid_to") else dt.date.max)
            entries.append({
                "seq": seq,
                "identity": raw["identity"],
                "name": raw["name"],
                "source": delivery["source"],
                "start": start,
                "end": end,
                "valid_from": raw["valid_from"],
                "valid_to": raw.get("valid_to", ""),
            })
    return entries


def _expected_sources(deliveries: list[dict]) -> list[str]:
    expected: list[str] = []
    for delivery in deliveries:
        for source in delivery["expected_sources"]:
            if source not in expected:
                expected.append(source)
    return sorted(expected)


def _round_seq(store, round_id: str) -> int:
    """批次次序取「开户」时点（最早一次投递），而不是最后一次。

    否则旧批次补交一个来源时次序会被抬高，反过来盖掉已经上线的新批次。
    """
    rows = store.list_deliveries(round_id)
    return min((row["seq"] for row in rows), default=0)


def _current_inputs(store) -> tuple[list[dict], list[dict]]:
    batch = store.latest_batch()
    if batch is None:
        return [], []
    return (store.load_metrics(batch["batch_id"]),
            store.load_reports(batch["batch_id"]))


def _merge_round_reports(report_rows: list[dict],
                         extra: list[dict]) -> list[dict]:
    """批次报表 + 本轮投递里声明的报表：按 report_id+metric+发布时间去重。"""
    merged: dict[tuple, dict] = {}
    for row in report_rows:
        key = (row["report_id"], row["metric"],
               (row.get("published_on") or "").strip())
        merged.setdefault(key, row)
    for raw in extra:
        report_id = str(raw.get("report_id") or "").strip()
        metric = str(raw.get("metric") or "").strip()
        if not report_id or not metric:
            continue
        try:
            weight = Decimal(str(raw.get("weight") or "0").strip())
        except Exception:  # noqa: BLE001 - 报表权重坏值按 0，交给诊断呈现
            weight = Decimal("0")
        key = (report_id, metric, str(raw.get("published_on") or "").strip())
        merged.setdefault(key, {
            "report_id": report_id,
            "report_name": str(raw.get("report_name") or report_id).strip(),
            "metric": metric,
            "weight": weight,
            "published_on": str(raw.get("published_on") or "").strip(),
            "line_no": 0,
            "resolved": True,
        })
    return [merged[key] for key in sorted(merged)]


def _dedupe_round_reports(deliveries: list[dict]) -> list[dict]:
    """同一份报表被多个来源重复声明时只留一条（内容一致才留，否则取首条）。"""
    seen: dict[tuple, dict] = {}
    for delivery in deliveries:
        for raw in delivery.get("reports") or []:
            key = (str(raw.get("report_id") or "").strip(),
                   str(raw.get("metric") or "").strip(),
                   str(raw.get("published_on") or "").strip())
            seen.setdefault(key, raw)
    return [seen[key] for key in sorted(seen)]


def evaluate_round(store, settings, round_id: str, deliveries: list[dict]) -> dict:
    """算出一轮的状态与可切换快照；不写现网。"""
    expected = _expected_sources(deliveries)
    delivered = sorted({delivery["source"] for delivery in deliveries})
    missing = [source for source in expected if source not in delivered]
    entries = _combined_entries(deliveries)
    checked = model.validate(entries)
    diagnostics = list(checked["diagnostics"])

    status = "promoted"
    reason_codes: list[str] = []
    if missing:
        status = "pending"
        reason_codes.append("I_ROUND_INCOMPLETE")
        diagnostics.append(model.diag(
            "I_ROUND_INCOMPLETE",
            detail="还缺 " + "、".join(missing) + " 的投递，保留上一版本"))
    if checked["has_error"]:
        status = "conflict"
        reason_codes.append("I_ROUND_CONFLICT")
        diagnostics.append(model.diag(
            "I_ROUND_CONFLICT",
            detail="整批映射有冲突，未切换现网"))

    candidate = None
    if status == "promoted":
        metrics, reports = _current_inputs(store)
        reports = _merge_round_reports(reports, _dedupe_round_reports(deliveries))
        candidate = snapshot.build(segments=checked["segments"], metrics=metrics,
                                   report_rows=reports,
                                   eval_day=settings.eval_date)
        diagnostics.extend(candidate["diagnostics"])
        if any(item["level"] == reasons.ERROR for item in candidate["diagnostics"]):
            status = "conflict"
            reason_codes.append("I_ROUND_CONFLICT")
        elif not candidate["accounts"]["all_balanced"]:
            status = "conflict"
            reason_codes.append("I_ACCOUNT_UNBALANCED")
        else:
            candidate["round_id"] = round_id
            candidate["sources"] = delivered
            candidate["expected_sources"] = expected
            candidate["digest"] = snapshot.digest(
                {key: value for key, value in candidate.items()
                 if key not in {"round_id", "sources", "expected_sources",
                                "digest", "promoted_at", "seq"}})
        if status != "promoted":
            candidate = None

    diagnostics = model.sort_diags(diagnostics)
    return {
        "round_id": round_id,
        "status": status,
        "complete": not missing,
        "missing_sources": missing,
        "delivered_sources": delivered,
        "expected_sources": expected,
        "reason_codes": reason_codes,
        "entries": len(entries),
        "segments": checked["segments"],
        "diagnostics": diagnostics,
        "snapshot": candidate,
        "seq": _round_seq(store, round_id),
    }


def _round_payload(state: dict, live: dict | None, *, reused: bool) -> dict:
    candidate = state["snapshot"]
    promoted = (state["status"] == "promoted" and candidate is not None
                and live is not None
                and live["round_id"] == state["round_id"]
                and live["digest"] == candidate["digest"])
    return {
        "round_id": state["round_id"],
        "status": state["status"],
        "reused": reused,
        "promoted": promoted,
        "complete": state["complete"],
        "missing_sources": state["missing_sources"],
        "delivered_sources": state["delivered_sources"],
        "expected_sources": state["expected_sources"],
        "reason_codes": state["reason_codes"],
        "entries": state["entries"],
        "segments": len(state["segments"]),
        "digest": candidate["digest"] if candidate else None,
        "counters": candidate["counters"] if candidate else None,
        "accounts": candidate["accounts"] if candidate else None,
        "diagnostics": _payload_diags(state["diagnostics"]),
        "live": {"round_id": live["round_id"], "digest": live["digest"],
                 "promoted_at": live["promoted_at"]} if live else None,
    }


def _payload_diags(diags: list[dict]) -> list[dict]:
    return [{"code": item["code"], "level": item["level"], "stage": item["stage"],
             "identity": item.get("identity"), "name": item.get("name"),
             "report_id": item.get("report_id"), "detail": item["detail"]}
            for item in diags]


def deliver(store, settings, payload) -> dict:
    """收一次投递；同批同来源原样重试直接返回，内容不同则拒绝。"""
    try:
        parsed = model.parse_payload(payload)
    except model.PayloadError as exc:
        raise errors.BadRequest(str(exc), code=exc.code) from exc
    except ValueError as exc:
        raise errors.BadRequest(str(exc), code="I_ENTRY_INVALID") from exc

    digest = model.payload_digest(parsed)
    existing = store.get_delivery(parsed["round_id"], parsed["source"])
    if existing is not None:
        if existing["digest"] != digest:
            raise errors.Conflict(
                f"批次 {parsed['round_id']} 的来源 {parsed['source']} 已投递过不同内容",
                code="I_DELIVERY_CONFLICT",
                detail={"round_id": parsed["round_id"],
                        "source": parsed["source"],
                        "received_digest": existing["digest"][:12],
                        "incoming_digest": digest[:12]})
        row = {
            "round_id": parsed["round_id"],
            "source": parsed["source"],
            "digest": digest,
            "entries": model.stored_entries(parsed["entries"]),
            "expected_sources": parsed["expected_sources"],
            "received_at": existing["received_at"],
            "seq": existing["seq"],
        }
        reused = True
    else:
        row = {
            "round_id": parsed["round_id"],
            "source": parsed["source"],
            "digest": digest,
            "entries": model.stored_entries(parsed["entries"]),
            "expected_sources": parsed["expected_sources"],
            "reports": parsed["reports"],
            "received_at": _now(),
            "seq": store.next_delivery_seq(),
        }
        store.save_delivery(row)
        reused = False

    deliveries = _round_rows(store, parsed["round_id"])
    state = evaluate_round(store, settings, parsed["round_id"], deliveries)
    live = store.load_live()

    if state["status"] == "promoted" and state["snapshot"] is not None:
        candidate = state["snapshot"]
        if live is None or live["digest"] != candidate["digest"]:
            # 只允许新批次覆盖旧批次：重放或补交更早的批次不能把现网拨回去
            if live is not None and live["seq"] > state["seq"]:
                state["diagnostics"] = model.sort_diags(
                    state["diagnostics"] + [model.diag(
                        "I_ROUND_SUPERSEDED",
                        detail=f"现网已是更晚批次 {live['round_id']}，"
                               f"本次批次不覆盖")])
            else:
                store.promote_live(round_id=state["round_id"],
                                   digest=candidate["digest"],
                                   promoted_at=_now(), snapshot=candidate,
                                   seq=state["seq"])
                live = store.load_live()
                state["diagnostics"] = model.sort_diags(
                    state["diagnostics"] + [model.diag(
                        "I_ROUND_PROMOTED",
                        detail=f"批次 {state['round_id']} 整批一致，已切换现网")])

    if reused:
        state["diagnostics"] = model.sort_diags(
            state["diagnostics"] + [model.diag(
                "I_ROUND_REPLAY",
                detail=f"批次 {state['round_id']} 来源 {parsed['source']} "
                       f"原样重试，不新增投递与结果")])
    return _round_payload(state, live, reused=reused)


def refresh_live(store, settings) -> dict | None:
    """定义批次变了以后重算现网：映射不变，依赖与影响面按新定义整体替换。

    只在已有现网映射时动作；重新评估原批次若不再满足「整批一致」，则**不动**
    现网，留着上一版并返回 None，避免把现网悄悄改回半成品。
    """
    live = store.load_live()
    if live is None:
        return None
    deliveries = _round_rows(store, live["round_id"])
    if not deliveries:
        return None
    state = evaluate_round(store, settings, live["round_id"], deliveries)
    if state["status"] != "promoted" or state["snapshot"] is None:
        return None
    candidate = state["snapshot"]
    if live["digest"] == candidate["digest"]:
        return live
    store.promote_live(round_id=state["round_id"], digest=candidate["digest"],
                       promoted_at=_now(), snapshot=candidate, seq=state["seq"])
    return store.load_live()


def round_view(store, settings, round_id: str) -> dict:
    deliveries = _round_rows(store, round_id)
    if not deliveries:
        raise errors.NotFound(f"批次不存在：{round_id}", code="I_NO_LIVE")
    state = evaluate_round(store, settings, round_id, deliveries)
    return _round_payload(state, store.load_live(), reused=False)


def list_rounds(store, settings) -> dict:
    rounds = []
    for round_id in store.round_ids():
        deliveries = _round_rows(store, round_id)
        state = evaluate_round(store, settings, round_id, deliveries)
        rounds.append({
            "round_id": round_id,
            "status": state["status"],
            "expected_sources": state["expected_sources"],
            "delivered_sources": state["delivered_sources"],
            "missing_sources": state["missing_sources"],
            "entries": state["entries"],
            "seq": state["seq"],
        })
    live = store.load_live()
    return {"rounds": rounds, "count": len(rounds),
            "live": {"round_id": live["round_id"], "digest": live["digest"],
                     "promoted_at": live["promoted_at"]} if live else None}


def live_snapshot(store) -> dict:
    live = store.load_live()
    if live is None:
        raise errors.NotFound("还没有整批一致的身份映射，先投递齐一个批次",
                              code="I_NO_LIVE")
    return live


def live_view(store) -> dict:
    live = live_snapshot(store)
    snap = live["snapshot"]
    return {
        "live": True,
        "round_id": live["round_id"],
        "digest": live["digest"],
        "promoted_at": live["promoted_at"],
        "eval_date": snap["eval_date"],
        "counters": snap["counters"],
        "accounts": snap["accounts"],
        "identities": snap["identities"],
        "definitions": snap["definitions"],
        "edges": snap["edges"],
        "cycles": snap["cycles"],
        "reports": snap["reports"],
        "impact": sorted(snap["impact"],
                         key=lambda row: (-Decimal(row["score"]), row["identity"])),
        "diagnostics": snap["diagnostics"],
    }


def snapshot_view(store, settings) -> dict:
    rounds = list_rounds(store, settings)["rounds"]
    try:
        view = live_view(store)
    except errors.MetricregError:
        return {"live": False, "rounds": rounds,
                "diagnostics": [{"code": "I_NO_LIVE", "level": "warn",
                                 "stage": "identity",
                                 "detail": reasons.get("I_NO_LIVE").title}]}
    view["rounds"] = rounds
    return view


def impact_view(store, identity: str | None) -> dict:
    live = live_snapshot(store)
    snap = live["snapshot"]
    rows = {row["identity"]: row for row in snap["impact"]}
    if identity is None:
        if not rows:
            raise errors.NotFound("现网快照没有身份", code="X_EMPTY_SELECTION")
        identity = min(rows)
    if identity not in rows:
        raise errors.NotFound(f"身份不在现网快照：{identity}", code="I_NO_LIVE")
    payload = dict(rows[identity])
    definition = next((row for row in snap["definitions"]
                       if row["identity"] == identity), None)
    payload.update({"known": True, "round_id": live["round_id"],
                    "digest": live["digest"],
                    "definition": definition,
                    "identities": [row["identity"] for row in snap["identities"]],
                    "ranking": sorted(snap["impact"],
                                      key=lambda row: (-Decimal(row["score"]),
                                                       row["identity"]))})
    return payload


def reports_view(store, settings, *, report_id: str | None = None) -> dict:
    live = store.load_live()
    if live is None:
        raise errors.NotFound("还没有整批一致的身份映射，先投递齐一个批次",
                              code="I_NO_LIVE")
    snap = live["snapshot"]
    rows = snap["reports"]
    if report_id:
        rows = [row for row in rows if row["report_id"] == report_id]
        if not rows:
            raise errors.NotFound(f"报表不在现网快照：{report_id}",
                                  code="I_REPORT_UNMAPPED")
    return {"round_id": live["round_id"], "eval_date": snap["eval_date"],
            "rows": rows, "total": len(rows),
            "bound": sum(1 for row in rows if row["status"] == "bound"),
            "unmapped": sum(1 for row in rows if row["status"] == "unmapped")}


def lookup(store, settings, *, name: str, published_on: str | None) -> dict:
    """按「名称 + 发布时间」查当时的身份：旧报表口径回溯的单一入口。"""
    live = store.load_live()
    if live is None:
        raise errors.NotFound("还没有整批一致的身份映射，先投递齐一个批次",
                              code="I_NO_LIVE")
    day = settings.eval_date
    if published_on:
        parsed = dt.date.fromisoformat(published_on) if _is_date(published_on) else None
        if parsed is None:
            raise errors.BadRequest("published_on 不是日期", code="I_REPORT_TIME_INVALID")
        day = parsed
    segments = model.rows_to_entries(live["snapshot"]["segments"])
    nindex = model.name_index(segments)
    iindex = model.identity_index(segments)
    resolved = model.resolve_name(nindex, name, day)
    if resolved is None:
        return {"name": name, "published_on": published_on or "",
                "resolved_on": day.isoformat(), "identity": None,
                "name_today": None, "matched": False,
                "detail": "该名称在这个时点没有归属身份"}
    identity = resolved["identity"]
    return {"name": name, "published_on": published_on or "",
            "resolved_on": day.isoformat(), "identity": identity,
            "identity_name": resolved["name"],
            "name_today": model.name_at(iindex, identity, settings.eval_date),
            "matched": True}


def _is_date(text: str) -> bool:
    try:
        dt.date.fromisoformat(text)
    except ValueError:
        return False
    return True


def sample_payloads(settings, filename: str = "metric_identity.csv") -> list[dict]:
    """样例映射表按 source 拆成多次投递；同一批的 expected_sources 取全集。

    样例文件只是「照着填」的参考：真实投递仍走 /api/identity/deliver，
    分批到达与整批核对的行为完全一致。
    """
    import csv
    import io
    from pathlib import Path

    path = Path(settings.sample_dir) / filename
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8-sig")
    grouped: dict[str, list[dict]] = {}
    for raw in csv.DictReader(io.StringIO(text)):
        identity = (raw.get("identity") or "").strip()
        name = (raw.get("name") or "").strip()
        source = (raw.get("source") or "样例").strip()
        if not identity or not name:
            continue
        grouped.setdefault(source, []).append({
            "identity": identity, "name": name,
            "valid_from": (raw.get("valid_from") or "").strip(),
            "valid_to": (raw.get("valid_to") or "").strip() or None,
            "source": source,
        })
    sources = sorted(grouped)
    return [{"round": "sample", "source": source, "expected_sources": sources,
             "entries": grouped[source]} for source in sources]
