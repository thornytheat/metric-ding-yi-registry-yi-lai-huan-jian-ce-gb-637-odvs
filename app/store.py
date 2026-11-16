"""SQLite 存储：批次、行、指标、诊断、图、版本、冲突、报表、账目、运行清单。

约定：
- 权重与版本号入 TEXT，Decimal 字符串，不落 float；
- 一次运行的所有行同一事务「先删后插」，失败整体回滚，不留半批数据；
- 所有读取都按 batch_id 过滤，页面永远只展示一个批次。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY, batch_key TEXT NOT NULL, run_id TEXT NOT NULL,
    source TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
    finished_at TEXT, input_digest TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
    params_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_batches_key ON batches(batch_key, status);
CREATE TABLE IF NOT EXISTS lines (
    batch_id TEXT NOT NULL, line_no INTEGER NOT NULL, kind TEXT NOT NULL,
    state TEXT NOT NULL, raw TEXT NOT NULL, code TEXT
);
CREATE TABLE IF NOT EXISTS metrics (
    batch_id TEXT NOT NULL, name TEXT NOT NULL, line_no INTEGER NOT NULL,
    signature TEXT NOT NULL, expr_json TEXT NOT NULL, where_json TEXT NOT NULL,
    group_by_json TEXT NOT NULL, fields_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL, deps_json TEXT NOT NULL,
    aggs_json TEXT NOT NULL, expr_text TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS diagnostics (
    batch_id TEXT NOT NULL, code TEXT NOT NULL, level TEXT NOT NULL,
    stage TEXT NOT NULL, line_no INTEGER, metric TEXT, field TEXT,
    raw_value TEXT, detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS nodes (
    batch_id TEXT NOT NULL, node_id TEXT NOT NULL, kind TEXT NOT NULL, label TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edges (
    batch_id TEXT NOT NULL, src TEXT NOT NULL, dst TEXT NOT NULL, kind TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS components (
    batch_id TEXT NOT NULL, comp_id INTEGER NOT NULL, size INTEGER NOT NULL,
    members_json TEXT NOT NULL, path_json TEXT NOT NULL DEFAULT '[]',
    is_cycle INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS conflicts (
    batch_id TEXT NOT NULL, metric TEXT NOT NULL, level TEXT NOT NULL,
    kept_line INTEGER NOT NULL, other_line INTEGER NOT NULL,
    diff_json TEXT NOT NULL, detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS versions (
    batch_id TEXT NOT NULL, metric TEXT NOT NULL, version INTEGER NOT NULL,
    valid_from TEXT NOT NULL, valid_to TEXT NOT NULL, author TEXT NOT NULL,
    submitted_at TEXT NOT NULL, status TEXT NOT NULL, note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS reports (
    batch_id TEXT NOT NULL, report_id TEXT NOT NULL, report_name TEXT NOT NULL,
    metric TEXT NOT NULL, weight TEXT NOT NULL, resolved INTEGER NOT NULL DEFAULT 1,
    published_on TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS totals (
    batch_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manifests (
    batch_id TEXT PRIMARY KEY, json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS identity_deliveries (
    round_id TEXT NOT NULL, source TEXT NOT NULL, digest TEXT NOT NULL,
    entries_json TEXT NOT NULL, expected_json TEXT NOT NULL,
    reports_json TEXT NOT NULL DEFAULT '[]',
    received_at TEXT NOT NULL, seq INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (round_id, source)
);
CREATE INDEX IF NOT EXISTS idx_identity_round ON identity_deliveries(round_id);
CREATE TABLE IF NOT EXISTS identity_live (
    slot INTEGER PRIMARY KEY CHECK (slot = 1),
    round_id TEXT NOT NULL, digest TEXT NOT NULL, promoted_at TEXT NOT NULL,
    opened_seq INTEGER NOT NULL DEFAULT 0,
    snapshot_json TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).resolve().parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn) -> None:
        """老库补列：只加不删，重复执行安全（SQLite 无 IF NOT EXISTS 列语法）。"""
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(reports)")}
        if "published_on" not in columns:
            conn.execute(
                "ALTER TABLE reports ADD COLUMN published_on TEXT NOT NULL DEFAULT ''")
        columns = {row["name"]
                   for row in conn.execute("PRAGMA table_info(identity_live)")}
        if "opened_seq" not in columns:
            conn.execute(
                "ALTER TABLE identity_live ADD COLUMN opened_seq INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------- 批次 ----------
    def get_batch(self, batch_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        return dict(row) if row else None

    def find_done_by_key(self, key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM batches WHERE batch_key=? AND status='done' "
                "ORDER BY created_at LIMIT 1", (key,)).fetchone()
        return dict(row) if row else None

    def latest_batch(self, status: str = "done") -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM batches WHERE status=? ORDER BY created_at DESC, rowid DESC "
                "LIMIT 1", (status,)).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM batches ORDER BY created_at DESC, rowid DESC LIMIT 1"
                ).fetchone()
        return dict(row) if row else None

    def list_batches(self, limit: int = 30) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM batches ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def count_batches(self) -> int:
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM batches").fetchone()["n"]

    # ---------- 一次运行整体落库 ----------
    def save_run(self, *, batch: dict, payload: dict) -> None:
        batch_id = batch["batch_id"]
        with self.connect() as conn:
            for table in ("lines", "metrics", "diagnostics", "nodes", "edges",
                          "components", "conflicts", "versions", "reports",
                          "totals", "manifests"):
                conn.execute(f"DELETE FROM {table} WHERE batch_id=?", (batch_id,))
            conn.executemany(
                "INSERT INTO lines (batch_id,line_no,kind,state,raw,code) VALUES (?,?,?,?,?,?)",
                [(batch_id, r["line_no"], r["kind"], r["state"], r["raw"], r.get("code"))
                 for r in payload["lines"]])
            conn.executemany(
                """INSERT INTO metrics (batch_id,name,line_no,signature,expr_json,where_json,
                     group_by_json,fields_json,metrics_json,deps_json,aggs_json,
                     expr_text,note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(batch_id, m["name"], m["line_no"], m["signature"],
                  _j(m["expr"]), _j(m["where"]), _j(m["group_by"]), _j(m["fields"]),
                  _j(m["metrics"]), _j(m["depends_declared"]), _j(m["aggregators"]),
                  m.get("expr_text", ""), m.get("note", "")) for m in payload["metrics"]])
            conn.executemany(
                """INSERT INTO diagnostics (batch_id,code,level,stage,line_no,metric,field,
                     raw_value,detail) VALUES (?,?,?,?,?,?,?,?,?)""",
                [(batch_id, d["code"], d["level"], d["stage"], d.get("line_no"),
                  d.get("metric"), d.get("field"), d.get("raw_value"), d.get("detail", ""))
                 for d in payload["diagnostics"]])
            conn.executemany(
                "INSERT INTO nodes (batch_id,node_id,kind,label) VALUES (?,?,?,?)",
                [(batch_id, n["node_id"], n["kind"], n["label"]) for n in payload["nodes"]])
            conn.executemany(
                "INSERT INTO edges (batch_id,src,dst,kind) VALUES (?,?,?,?)",
                [(batch_id, e["src"], e["dst"], e["kind"]) for e in payload["edges"]])
            conn.executemany(
                """INSERT INTO components (batch_id,comp_id,size,members_json,path_json,
                     is_cycle) VALUES (?,?,?,?,?,?)""",
                [(batch_id, c["comp_id"], c["size"], _j(c["members"]),
                  _j(c.get("path", [])), 1 if c["is_cycle"] else 0)
                 for c in payload["components"]])
            conn.executemany(
                """INSERT INTO conflicts (batch_id,metric,level,kept_line,other_line,
                     diff_json,detail) VALUES (?,?,?,?,?,?,?)""",
                [(batch_id, c["metric"], c["level"], c["kept_line"], c["other_line"],
                  _j(c["diff"]), c.get("detail", "")) for c in payload["conflicts"]])
            conn.executemany(
                """INSERT INTO versions (batch_id,metric,version,valid_from,valid_to,author,
                     submitted_at,status,note) VALUES (?,?,?,?,?,?,?,?,?)""",
                [(batch_id, v["metric"], v["version"], v["valid_from"], v["valid_to"],
                  v["author"], v["submitted_at"], v["status"], v.get("note", ""))
                 for v in payload["versions"]])
            conn.executemany(
                """INSERT INTO reports (batch_id,report_id,report_name,metric,weight,
                     resolved,published_on) VALUES (?,?,?,?,?,?,?)""",
                [(batch_id, r["report_id"], r["report_name"], r["metric"],
                  str(r["weight"]), 1 if r["resolved"] else 0,
                  r.get("published_on", "")) for r in payload["reports"]])
            for key, value in payload["totals"].items():
                conn.execute("INSERT INTO totals (batch_id,key,value) VALUES (?,?,?)",
                             (batch_id, key, str(value)))
            conn.execute(
                "INSERT INTO manifests (batch_id,json) VALUES (?,?) "
                "ON CONFLICT(batch_id) DO UPDATE SET json=excluded.json",
                (batch_id, json.dumps(payload["manifest"], ensure_ascii=False)))
            conn.execute(
                """INSERT INTO batches (batch_id,batch_key,run_id,source,status,created_at,
                     finished_at,input_digest,note,params_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(batch_id) DO UPDATE SET status=excluded.status,
                     finished_at=excluded.finished_at, note=excluded.note,
                     input_digest=excluded.input_digest""",
                (batch_id, batch["batch_key"], batch["run_id"], batch["source"],
                 batch["status"], batch["created_at"], batch.get("finished_at"),
                 batch["input_digest"], batch.get("note", ""),
                 json.dumps(batch.get("params", {}), ensure_ascii=False)))

    # ---------- 读取 ----------
    def load_lines(self, batch_id: str) -> list[dict]:
        return self._all(
            "SELECT * FROM lines WHERE batch_id=? ORDER BY line_no", (batch_id,))

    def load_metrics(self, batch_id: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM metrics WHERE batch_id=? ORDER BY line_no", (batch_id,))
        mapping = {"expr": "expr_json", "where": "where_json",
                   "group_by": "group_by_json", "fields": "fields_json",
                   "metrics": "metrics_json", "depends_declared": "deps_json",
                   "aggregators": "aggs_json"}
        for r in rows:
            for key, column in mapping.items():
                r[key] = json.loads(r.pop(column) or "null")
        return rows

    def find_metric(self, batch_id: str, name: str) -> dict | None:
        for m in self.load_metrics(batch_id):
            if m["name"] == name:
                return m
        return None

    def load_diagnostics(self, batch_id: str) -> list[dict]:
        return self._all(
            "SELECT * FROM diagnostics WHERE batch_id=? "
            "ORDER BY CASE level WHEN 'error' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END, "
            "line_no, code", (batch_id,))

    def load_nodes(self, batch_id: str) -> list[dict]:
        return self._all(
            "SELECT * FROM nodes WHERE batch_id=? ORDER BY kind, node_id", (batch_id,))

    def load_edges(self, batch_id: str) -> list[dict]:
        return self._all(
            "SELECT * FROM edges WHERE batch_id=? ORDER BY kind, src, dst", (batch_id,))

    def load_components(self, batch_id: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM components WHERE batch_id=? ORDER BY is_cycle DESC, comp_id",
            (batch_id,))
        for r in rows:
            r["members"] = json.loads(r.pop("members_json"))
            r["path"] = json.loads(r.pop("path_json") or "[]")
            r["is_cycle"] = bool(r["is_cycle"])
        return rows

    def load_conflicts(self, batch_id: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM conflicts WHERE batch_id=? "
            "ORDER BY CASE level WHEN 'BLOCK' THEN 0 WHEN 'REVIEW' THEN 1 ELSE 2 END, metric",
            (batch_id,))
        from . import config
        for r in rows:
            r["diff"] = json.loads(r.pop("diff_json"))
            r["level_label"] = config.CONFLICT_LEVEL_LABELS.get(r["level"], r["level"])
        return rows

    def load_versions(self, batch_id: str) -> list[dict]:
        return self._all(
            "SELECT * FROM versions WHERE batch_id=? ORDER BY metric, version", (batch_id,))

    def load_reports(self, batch_id: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM reports WHERE batch_id=? ORDER BY report_id", (batch_id,))
        for r in rows:
            r["weight"] = Decimal(r["weight"])
            r["resolved"] = bool(r["resolved"])
        return rows

    def load_totals(self, batch_id: str) -> dict:
        rows = self._all("SELECT key, value FROM totals WHERE batch_id=?", (batch_id,))
        return {r["key"]: r["value"] for r in rows}

    def load_manifest(self, batch_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT json FROM manifests WHERE batch_id=?", (batch_id,)).fetchone()
        return json.loads(row["json"]) if row else None

    def count_rows(self, batch_id: str) -> dict:
        tables = ("lines", "metrics", "diagnostics", "nodes", "edges",
                  "components", "conflicts", "versions", "reports")
        with self.connect() as conn:
            return {t: conn.execute(
                f"SELECT COUNT(*) AS n FROM {t} WHERE batch_id=?", (batch_id,)
            ).fetchone()["n"] for t in tables}

    def insert_version(self, batch_id: str, row: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO versions (batch_id,metric,version,valid_from,valid_to,
                     author,submitted_at,status,note) VALUES (?,?,?,?,?,?,?,?,?)""",
                (batch_id, row["metric"], row["version"], row["valid_from"],
                 row["valid_to"], row["author"], row["submitted_at"],
                 row["status"], row.get("note", "")))

    def set_version_status(self, batch_id: str, metric: str, version: int,
                           status: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE versions SET status=? WHERE batch_id=? AND metric=? AND version=?",
                (status, batch_id, metric, version))

    def _all(self, sql: str, args: tuple) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    # ---------- M8 身份映射：分批投递与现网槽位 ----------
    def get_delivery(self, round_id: str, source: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM identity_deliveries WHERE round_id=? AND source=?",
                (round_id, source)).fetchone()
        return self._delivery_row(row) if row else None

    def list_deliveries(self, round_id: str | None = None) -> list[dict]:
        with self.connect() as conn:
            if round_id is None:
                rows = conn.execute(
                    "SELECT * FROM identity_deliveries ORDER BY round_id, seq, source"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM identity_deliveries WHERE round_id=? "
                    "ORDER BY seq, source", (round_id,)).fetchall()
        return [self._delivery_row(row) for row in rows]

    def round_ids(self) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT round_id, MAX(seq) AS last_seq FROM identity_deliveries "
                "GROUP BY round_id ORDER BY last_seq, round_id").fetchall()
        return [row["round_id"] for row in rows]

    def next_delivery_seq(self) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS n FROM identity_deliveries").fetchone()
        return int(row["n"]) + 1

    def save_delivery(self, row: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO identity_deliveries
                     (round_id, source, digest, entries_json, expected_json,
                      reports_json, received_at, seq)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (row["round_id"], row["source"], row["digest"],
                 _j(row["entries"]), _j(row.get("expected_sources", [])),
                 _j(row.get("reports", [])), row["received_at"], row["seq"]))

    def load_live(self) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM identity_live WHERE slot=1").fetchone()
        if row is None:
            return None
        return {"round_id": row["round_id"], "digest": row["digest"],
                "promoted_at": row["promoted_at"], "seq": row["opened_seq"],
                "snapshot": json.loads(row["snapshot_json"])}

    def promote_live(self, *, round_id: str, digest: str, promoted_at: str,
                     snapshot: dict, seq: int = 0) -> None:
        """单条 UPSERT：现网槽位要么完整是旧版，要么完整是新版，没有中间态。"""
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO identity_live (slot, round_id, digest, promoted_at,
                     opened_seq, snapshot_json) VALUES (1,?,?,?,?,?)
                   ON CONFLICT(slot) DO UPDATE SET round_id=excluded.round_id,
                     digest=excluded.digest, promoted_at=excluded.promoted_at,
                     opened_seq=excluded.opened_seq,
                     snapshot_json=excluded.snapshot_json""",
                (round_id, digest, promoted_at, seq,
                 json.dumps(snapshot, ensure_ascii=False)))

    @staticmethod
    def _delivery_row(row) -> dict:
        return {"round_id": row["round_id"], "source": row["source"],
                "digest": row["digest"],
                "entries": json.loads(row["entries_json"]),
                "expected_sources": json.loads(row["expected_json"]),
                "reports": json.loads(row["reports_json"] or "[]"),
                "received_at": row["received_at"], "seq": row["seq"]}


def _j(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
