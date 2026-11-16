"""M8 身份与名称复用：更名、复用、整批核对、原子切换、按发布时间回溯。

核心反例（TRAP-29）：同一名称先属于旧身份、后被新身份复用。把同名分段并进
同一身份，会让 `旧名 = ref(新名)` 与 `新名 = ref(旧名)` 拼成一个假环；
正确做法是身份只由 identity 决定，名称只在指定日期解析。
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.errors import BadRequest, Conflict, NotFound
from app.modules.m8_identity import model, service, snapshot

EVAL = dt.date(2026, 11, 15)


def entry(identity, name, start, end=None, source="BI-A"):
    return {"identity": identity, "name": name, "valid_from": start,
            "valid_to": end, "source": source}


def payload(round_id, source, entries, expected=None, reports=None):
    return {"round": round_id, "source": source,
            "expected_sources": expected or [source],
            "entries": entries, "reports": reports or []}


def segments(*rows):
    out = []
    for row in rows:
        identity, name, start, end = row
        start_d = dt.date.fromisoformat(start)
        end_d = dt.date.fromisoformat(end) if end else dt.date.max
        out.append({"identity": identity, "name": name, "start": start_d,
                    "end": end_d, "valid_from": start, "valid_to": end or "",
                    "source": "test", "seq": len(out) + 1})
    return out


# ---------------- 纯规则 ----------------
def test_name_resolution_is_half_open_across_rename():
    """更名相邻两段：前段 valid_to 当天已归后段，不会两头都命中。"""
    segs = segments(("I-1", "旧名", "2026-01-01", "2026-06-01"),
                    ("I-1", "新名", "2026-06-01", None))
    index = model.name_index(segs)
    assert model.resolve_name(index, "旧名", dt.date(2026, 5, 31))["identity"] == "I-1"
    assert model.resolve_name(index, "旧名", dt.date(2026, 6, 1)) is None
    assert model.resolve_name(index, "新名", dt.date(2026, 6, 1))["identity"] == "I-1"
    assert model.name_at(model.identity_index(segs), "I-1", dt.date(2026, 5, 31)) == "旧名"
    assert model.name_at(model.identity_index(segs), "I-1", dt.date(2026, 6, 1)) == "新名"


def test_name_reuse_maps_to_distinct_identities():
    """复用：同名在两段时间分属两个身份，按日期各归各的。"""
    segs = segments(("I-OLD", "销售额", "2026-01-01", "2026-09-01"),
                    ("I-NEW", "销售额", "2026-09-01", None))
    index = model.name_index(segs)
    assert model.resolve_name(index, "销售额", dt.date(2026, 5, 1))["identity"] == "I-OLD"
    assert model.resolve_name(index, "销售额", dt.date(2026, 10, 1))["identity"] == "I-NEW"
    checked = model.validate(segs)
    assert not checked["has_error"]
    codes = [d["code"] for d in checked["diagnostics"]]
    assert "I_NAME_REUSED" in codes


def test_parallel_reuse_is_conflict():
    """复用必须前后衔接：两个身份并行占用同一名称是冲突。"""
    segs = segments(("I-OLD", "销售额", "2026-01-01", "2026-09-01"),
                    ("I-NEW", "销售额", "2026-06-01", None))
    checked = model.validate(segs)
    assert checked["has_error"]
    assert "I_NAME_REUSE_OVERLAP" in [d["code"] for d in checked["diagnostics"]]


def test_identity_cannot_have_two_names_at_once():
    """同一身份的两个名称区间重叠：无法判断当期叫什么。"""
    segs = segments(("I-1", "甲", "2026-01-01", "2026-07-01"),
                    ("I-1", "乙", "2026-06-01", None))
    checked = model.validate(segs)
    assert checked["has_error"]
    assert "I_IDENTITY_OVERLAP" in [d["code"] for d in checked["diagnostics"]]


def test_identical_entries_are_deduped_not_conflicts():
    segs = segments(("I-1", "甲", "2026-01-01", None),
                    ("I-1", "甲", "2026-01-01", None))
    checked = model.validate(segs)
    assert not checked["has_error"]
    assert len(checked["segments"]) == 1
    assert "I_ENTRY_DUPLICATE" in [d["code"] for d in checked["diagnostics"]]


# ---------------- 快照：不伪造依赖环 ----------------
def test_rename_alias_does_not_fabricate_cycle():
    """反例：迁移别名 `新名 = ref(旧名)` 不能被解析成 A→B→A 的假环。"""
    segs = segments(("I-SALES", "旧销售额", "2026-01-01", "2026-09-01"),
                    ("I-SALES", "销售额", "2026-09-01", None))
    metrics = [
        {"name": "销售额", "line_no": 1, "metrics": ["旧销售额"],
         "depends_declared": ["旧销售额"], "expr_text": "ref(旧销售额)"},
        {"name": "旧销售额", "line_no": 2, "metrics": ["销售额"],
         "depends_declared": [], "expr_text": "ref(销售额)"},
    ]
    payload = snapshot.build(segments=segs, metrics=metrics, report_rows=[],
                             eval_day=EVAL)
    assert payload["cycles"] == []
    assert payload["edges"] == []
    assert "I_ALIAS_SELF_REF" in [d["code"] for d in payload["diagnostics"]]
    assert len(payload["definitions"]) == 1
    assert payload["definitions"][0]["name"] == "销售额"


def test_true_self_reference_still_counts_as_cycle():
    """反倒：真正的同名自引用不能被当成迁移别名隐掉。"""
    segs = segments(("I-1", "自比值", "2026-01-01", None))
    metrics = [{"name": "自比值", "line_no": 1, "metrics": ["自比值"],
                "depends_declared": [], "expr_text": "ref(自比值)"}]
    payload = snapshot.build(segments=segs, metrics=metrics, report_rows=[],
                             eval_day=EVAL)
    assert len(payload["cycles"]) == 1
    assert payload["cycles"][0]["members"] == ["I-1"]


def test_old_report_binds_to_old_identity_by_publish_time():
    """旧报表只记名称与发布时间：必须查回发布时那个身份，不并进复用者。"""
    segs = segments(("I-OLD", "销售额", "2026-01-01", "2026-09-01"),
                    ("I-NEW", "销售额", "2026-09-01", None))
    metrics = [
        {"name": "销售额", "line_no": 1, "metrics": [], "depends_declared": [],
         "expr_text": "SUM(order_amt)"},
    ]
    reports = [
        {"report_id": "R-OLD", "report_name": "旧日报", "metric": "销售额",
         "weight": 3, "published_on": "2026-05-01"},
        {"report_id": "R-NEW", "report_name": "新日报", "metric": "销售额",
         "weight": 2, "published_on": "2026-10-01"},
    ]
    payload = snapshot.build(segments=segs, metrics=metrics, report_rows=reports,
                             eval_day=EVAL)
    bindings = {row["report_id"]: row for row in payload["reports"]}
    assert bindings["R-OLD"]["identity"] == "I-OLD"
    assert bindings["R-NEW"]["identity"] == "I-NEW"
    # 旧身份没有当期定义 → 报表仍绑定，但不计入影响面
    assert bindings["R-OLD"]["status"] == "bound"
    assert bindings["R-OLD"]["has_definition"] is False


def test_report_without_publish_time_binds_to_current():
    segs = segments(("I-OLD", "销售额", "2026-01-01", "2026-09-01"),
                    ("I-NEW", "销售额", "2026-09-01", None))
    metrics = [{"name": "销售额", "line_no": 1, "metrics": [],
                "depends_declared": [], "expr_text": "SUM(order_amt)"}]
    reports = [{"report_id": "R-LIVE", "report_name": "新报表", "metric": "销售额",
                "weight": 1, "published_on": ""}]
    payload = snapshot.build(segments=segs, metrics=metrics, report_rows=reports,
                             eval_day=EVAL)
    assert payload["reports"][0]["identity"] == "I-NEW"
    assert payload["reports"][0]["matched_on"] == "current"


def test_report_published_before_any_segment_is_unmapped():
    """发布时间早于所有分段：不猜身份，也不退到后来的复用者。"""
    segs = segments(("I-NEW", "销售额", "2026-09-01", None))
    metrics = [{"name": "销售额", "line_no": 1, "metrics": [],
                "depends_declared": [], "expr_text": "SUM(order_amt)"}]
    reports = [{"report_id": "R-EARLY", "report_name": "早期", "metric": "销售额",
                "weight": 1, "published_on": "2025-01-01"}]
    payload = snapshot.build(segments=segs, metrics=metrics, report_rows=reports,
                             eval_day=EVAL)
    assert payload["reports"][0]["identity"] is None
    assert payload["reports"][0]["status"] == "unmapped"
    assert "I_REPORT_UNMAPPED" in [d["code"] for d in payload["diagnostics"]]


def test_invalid_publish_time_is_not_guessed():
    segs = segments(("I-NEW", "销售额", "2026-09-01", None))
    metrics = [{"name": "销售额", "line_no": 1, "metrics": [],
                "depends_declared": [], "expr_text": "SUM(order_amt)"}]
    reports = [{"report_id": "R-BAD", "report_name": "坏时间", "metric": "销售额",
                "weight": 1, "published_on": "2026/13/40"}]
    payload = snapshot.build(segments=segs, metrics=metrics, report_rows=reports,
                             eval_day=EVAL)
    assert payload["reports"][0]["status"] == "unmapped"
    assert payload["reports"][0]["time_valid"] is False
    assert "I_REPORT_TIME_INVALID" in [d["code"] for d in payload["diagnostics"]]


def test_impact_uses_identity_edges_not_names():
    segs = segments(("I-A", "甲", "2026-01-01", None),
                    ("I-B", "乙", "2026-01-01", None))
    metrics = [
        {"name": "甲", "line_no": 1, "metrics": ["乙"], "depends_declared": ["乙"],
         "expr_text": "ref(乙)"},
        {"name": "乙", "line_no": 2, "metrics": [], "depends_declared": [],
         "expr_text": "SUM(order_amt)"},
    ]
    reports = [{"report_id": "R-A", "report_name": "甲报", "metric": "甲",
                "weight": 2, "published_on": ""},
               {"report_id": "R-B", "report_name": "乙报", "metric": "乙",
                "weight": 3, "published_on": ""}]
    payload = snapshot.build(segments=segs, metrics=metrics, report_rows=reports,
                             eval_day=EVAL)
    impact = {row["identity"]: row for row in payload["impact"]}
    assert impact["I-B"]["downstream"] == ["I-A"]
    assert impact["I-B"]["weight_sum"] == "5.00"
    assert impact["I-B"]["score"] == "5.00"


def test_retired_name_still_resolves_to_last_owner():
    """名称已彻底停用（无复用者）：定义按最后所属身份解析，引用也按那一时点。"""
    segs = segments(("I-OLD", "销售额", "2026-01-01", "2026-09-01"))
    metrics = [
        {"name": "销售额", "line_no": 1, "metrics": [], "depends_declared": [],
         "expr_text": "SUM(order_amt)"},
        {"name": "毛利率", "line_no": 2, "metrics": ["销售额"],
         "depends_declared": ["销售额"], "expr_text": "ref(销售额)"},
    ]
    payload = snapshot.build(segments=segs, metrics=metrics, report_rows=[],
                             eval_day=EVAL)
    definitions = {row["name"]: row for row in payload["definitions"]}
    assert definitions["销售额"]["identity"] == "I-OLD"
    assert definitions["销售额"]["name_state"] == "stale"
    assert definitions["毛利率"]["identity"] == "name:毛利率"
    # 引用按评估日解析；名称已停用时退到最后一个所属身份，而不是造一个新身份
    assert payload["edges"] == [
        {"src": "name:毛利率", "dst": "I-OLD", "kind": "identity_identity",
         "via": "销售额", "resolved_on": "2026-11-15"}]
    assert payload["cycles"] == []


def test_reused_name_never_merges_two_identities():
    """复用名称的两个身份各自成节点：不会因为同名被合成一个。"""
    segs = segments(("I-OLD", "销售额", "2026-01-01", "2026-09-01"),
                    ("I-NEW", "销售额", "2026-09-01", None),
                    ("I-OLD", "旧口径", "2026-01-01", None),
                    ("I-NEW", "新口径", "2026-01-01", None))
    metrics = [
        {"name": "旧口径", "line_no": 1, "metrics": [], "depends_declared": [],
         "expr_text": "SUM(order_amt)"},
        {"name": "新口径", "line_no": 2, "metrics": ["旧口径"],
         "depends_declared": ["旧口径"], "expr_text": "ref(旧口径)"},
    ]
    payload = snapshot.build(segments=segs, metrics=metrics, report_rows=[],
                             eval_day=EVAL)
    identities = {row["identity"] for row in payload["definitions"]}
    assert identities == {"I-OLD", "I-NEW"}
    assert payload["edges"] == [
        {"src": "I-NEW", "dst": "I-OLD", "kind": "identity_identity",
         "via": "旧口径", "resolved_on": "2026-11-15"}]
    assert payload["cycles"] == []


# ---------------- 批次状态机 ----------------
def _deliver(store, settings, round_id, source, entries, expected, reports=None):
    return service.deliver(store, settings,
                           payload(round_id, source, entries, expected, reports))


def test_round_incomplete_keeps_previous_version(store, settings):
    entries = [entry("I-1", "甲", "2026-01-01")]
    first = _deliver(store, settings, "R1", "BI-A", entries, ["BI-A"])
    assert first["status"] == "promoted"
    assert first["promoted"] is True
    live_before = store.load_live()["digest"]

    second = _deliver(store, settings, "R2", "BI-A",
                      [entry("I-1", "乙", "2026-01-01")], ["BI-A", "BI-B"])
    assert second["status"] == "pending"
    assert second["promoted"] is False
    assert second["missing_sources"] == ["BI-B"]
    assert "I_ROUND_INCOMPLETE" in [d["code"] for d in second["diagnostics"]]
    assert store.load_live()["digest"] == live_before


def test_round_conflict_keeps_previous_version(store, settings):
    _deliver(store, settings, "R1", "BI-A", [entry("I-1", "甲", "2026-01-01")],
             ["BI-A"])
    live_before = store.load_live()["digest"]
    conflicting = [entry("I-1", "甲", "2026-01-01", "2026-07-01"),
                   entry("I-1", "乙", "2026-06-01")]
    result = _deliver(store, settings, "R2", "BI-A", conflicting, ["BI-A"])
    assert result["status"] == "conflict"
    assert result["promoted"] is False
    assert "I_ROUND_CONFLICT" in [d["code"] for d in result["diagnostics"]]
    assert store.load_live()["digest"] == live_before


def test_round_promotes_atomically_when_all_sources_arrive(store, settings):
    first = _deliver(store, settings, "R1", "BI-A",
                     [entry("I-1", "甲", "2026-01-01")], ["BI-A", "BI-B"])
    assert first["status"] == "pending"
    assert store.load_live() is None
    second = _deliver(store, settings, "R1", "BI-B",
                      [entry("I-2", "乙", "2026-01-01")], ["BI-A", "BI-B"])
    assert second["status"] == "promoted"
    assert second["promoted"] is True
    live = store.load_live()
    assert live["round_id"] == "R1"
    assert {seg["identity"] for seg in live["snapshot"]["segments"]} == {"I-1", "I-2"}


def test_replay_same_delivery_adds_no_result(store, settings):
    entries = [entry("I-1", "甲", "2026-01-01")]
    payload_a = payload("R1", "BI-A", entries, ["BI-A"])
    first = service.deliver(store, settings, payload_a)
    digest = store.load_live()["digest"]
    replay = service.deliver(store, settings, payload_a)
    assert replay["reused"] is True
    assert "I_ROUND_REPLAY" in [d["code"] for d in replay["diagnostics"]]
    assert len(store.list_deliveries("R1")) == 1
    assert store.load_live()["digest"] == digest


def test_same_round_different_content_is_rejected(store, settings):
    service.deliver(store, settings,
                    payload("R1", "BI-A", [entry("I-1", "甲", "2026-01-01")], ["BI-A"]))
    with pytest.raises(Conflict) as exc:
        service.deliver(store, settings,
                        payload("R1", "BI-A", [entry("I-1", "乙", "2026-01-01")],
                                ["BI-A"]))
    assert exc.value.payload()["error"] == "I_DELIVERY_CONFLICT"
    segments_now = store.load_live()["snapshot"]["segments"]
    assert [seg["name"] for seg in segments_now] == ["甲"]


def test_delivery_validation_errors(store, settings):
    with pytest.raises(BadRequest) as no_round:
        service.deliver(store, settings, {"source": "BI-A", "entries": []})
    assert no_round.value.payload()["error"] == "I_ENTRY_INVALID"
    with pytest.raises(BadRequest) as no_source:
        service.deliver(store, settings, {"round": "R1", "entries": []})
    assert no_source.value.payload()["error"] == "I_ENTRY_INVALID"
    with pytest.raises(BadRequest):
        service.deliver(store, settings, {"round": "R1", "source": "BI-A",
                                          "entries": "不是数组"})
    with pytest.raises(BadRequest) as inverted:
        service.deliver(store, settings, payload(
            "R1", "BI-A", [entry("I-1", "甲", "2026-05-01", "2026-01-01")], ["BI-A"]))
    assert inverted.value.payload()["error"] == "I_INTERVAL_INVERTED"


def test_lookup_returns_identity_at_publish_time(store, settings):
    entries = [entry("I-OLD", "销售额", "2026-01-01", "2026-09-01"),
               entry("I-NEW", "销售额", "2026-09-01")]
    service.deliver(store, settings, payload("R1", "BI-A", entries, ["BI-A"]))
    old = service.lookup(store, settings, name="销售额", published_on="2026-05-01")
    assert old["identity"] == "I-OLD"
    new = service.lookup(store, settings, name="销售额", published_on="2026-10-01")
    assert new["identity"] == "I-NEW"
    missing = service.lookup(store, settings, name="销售额", published_on="2025-01-01")
    assert missing["matched"] is False
    with pytest.raises(BadRequest):
        service.lookup(store, settings, name="销售额", published_on="不是日期")


def test_live_view_requires_promotion(store, settings):
    with pytest.raises(NotFound) as exc:
        service.live_view(store)
    assert exc.value.payload()["error"] == "I_NO_LIVE"
    view = service.snapshot_view(store, settings)
    assert view["live"] is False


def test_renames_and_reuse_counters(store, settings):
    entries = [entry("I-SALES", "销售总额", "2025-01-01", "2026-03-01"),
               entry("I-SALES", "销售额", "2026-03-01", "2026-09-01"),
               entry("I-NEW", "销售额", "2026-09-01")]
    result = _deliver(store, settings, "R1", "BI-A", entries, ["BI-A"])
    counters = result["counters"]
    assert counters["segments"] == 3
    assert counters["renamed_identities"] == 1
    assert counters["reused_names"] == 1


def test_late_source_on_old_round_does_not_override_newer_round(store, settings):
    """旧批次补交来源不能让批次次序变大，更不能盖掉已上线的新批次。"""
    _deliver(store, settings, "R1", "BI-A",
             [entry("I-1", "甲", "2026-01-01")], ["BI-A", "BI-B"])
    _deliver(store, settings, "R2", "BI-A",
             [entry("I-1", "乙", "2026-01-01")], ["BI-A"])
    assert store.load_live()["round_id"] == "R2"
    digest_new = store.load_live()["digest"]

    late = _deliver(store, settings, "R1", "BI-B",
                    [entry("I-9", "丙", "2026-01-01")], ["BI-A", "BI-B"])
    assert late["status"] == "promoted"
    assert late["promoted"] is False
    assert "I_ROUND_SUPERSEDED" in [d["code"] for d in late["diagnostics"]]
    live = store.load_live()
    assert live["round_id"] == "R2"
    assert live["digest"] == digest_new
    # 投递本身照常留档，便于事后核对「这一批到底收了什么」
    assert len(store.list_deliveries("R1")) == 2


# ---------------- 接口 ----------------
def test_older_round_replay_does_not_roll_back_live(store, settings):
    """重放更早批次不能点回现网：批次数与现网指纹都停在最新版。"""
    _deliver(store, settings, "R1", "BI-A", [entry("I-1", "甲", "2026-01-01")],
             ["BI-A"])
    _deliver(store, settings, "R2", "BI-A", [entry("I-1", "乙", "2026-01-01")],
             ["BI-A"])
    live_after_r2 = store.load_live()
    assert live_after_r2["round_id"] == "R2"

    replay = _deliver(store, settings, "R1", "BI-A",
                      [entry("I-1", "甲", "2026-01-01")], ["BI-A"])
    assert replay["reused"] is True
    assert replay["promoted"] is False
    live_now = store.load_live()
    assert live_now["round_id"] == "R2"
    assert live_now["digest"] == live_after_r2["digest"]


def test_definition_batch_refresh_keeps_live_mapping(store, settings, run):
    """定义批次更新后现网映射不变，依赖与影响面按新定义整体替换。"""
    _deliver(store, settings, "R1", "BI-A",
             [entry("I-SALES", "销售额", "2025-01-01")], ["BI-A"])
    live_before = store.load_live()
    from app.modules.m1_ingest import service as ingest
    ingest.run(settings, store, sample="metrics_clean.dsl")
    live_after = store.load_live()
    assert live_after["round_id"] == live_before["round_id"]
    assert live_after["digest"] != live_before["digest"]
    definitions = {row["name"] for row in live_after["snapshot"]["definitions"]}
    assert definitions == {"销售额", "毛利额", "毛利率", "客单价", "订单数",
                           "渠道销售额", "退款率", "净增量"}


def test_fallback_identity_prefix_is_reserved(store, settings):
    """`name:` 是兜底身份的保留前缀，外部映射占用了就和兜底撞号。"""
    with pytest.raises(BadRequest):
        service.deliver(store, settings, payload(
            "R1", "BI-A", [entry("name:销售额", "销售额", "2026-01-01")], ["BI-A"]))


def test_sample_payloads_split_by_source(settings):
    payloads = service.sample_payloads(settings)
    assert len(payloads) == 2
    assert {item["source"] for item in payloads} == {"BI-报表", "BI-看板"}
    assert all(set(item["expected_sources"]) ==
               {payload["source"] for payload in payloads} for item in payloads)


def test_api_flow(client):
    assert client.post("/api/import", json={}).status_code == 200
    first = client.post("/api/identity/deliver", json=payload(
        "R1", "BI-A", [entry("I-1", "甲", "2026-01-01")], ["BI-A"]))
    assert first.status_code == 200
    assert first.json()["status"] == "promoted"
    live = client.get("/api/identity/live").json()
    assert live["round_id"] == "R1"
    lookup = client.get("/api/identity/lookup",
                        params={"name": "甲", "published_on": "2026-06-01"}).json()
    assert lookup["identity"] == "I-1"
    rounds = client.get("/api/identity/rounds").json()
    assert rounds["count"] == 1
    assert rounds["live"]["round_id"] == "R1"
    impact = client.get("/api/identity/impact", params={"identity": "I-1"}).json()
    assert impact["identity"] == "I-1"
    reports = client.get("/api/identity/reports").json()
    assert reports["total"] == 10
    page = client.get("/identity")
    assert page.status_code == 200
    assert 'data-nav="identity"' in page.text


def test_api_delivery_conflict_status(client):
    body = payload("R1", "BI-A", [entry("I-1", "甲", "2026-01-01")], ["BI-A"])
    assert client.post("/api/identity/deliver", json=body).status_code == 200
    body2 = payload("R1", "BI-A", [entry("I-1", "乙", "2026-01-01")], ["BI-A"])
    response = client.post("/api/identity/deliver", json=body2)
    assert response.status_code == 409
    assert response.json()["error"] == "I_DELIVERY_CONFLICT"


def test_api_live_missing_returns_404(client):
    assert client.get("/api/identity/live").status_code == 404
    assert client.get("/api/identity/lookup", params={"name": "甲"}).status_code == 404
