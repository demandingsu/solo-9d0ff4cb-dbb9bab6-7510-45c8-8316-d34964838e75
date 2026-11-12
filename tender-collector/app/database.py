"""SQLite 持久化层。

announcements 以公告编号 (code) 为唯一键：重复采集走 UPSERT，不会产生重复记录。
announcement_evidence 与公告记录一一对应，保存采集时的追溯证据
（详情 HTML 的 SHA-256、截止时间原文、字段所在表格行、来源采集轮次），
并与 announcements 在同一事务内写入——复采后字段变化时，公告记录与证据
原子地切换到同一轮内容，查询永远不会看到新旧混杂的状态。
collection_runs 记录每次采集的成败，失败时不触碰 announcements/evidence，
因此上次成功采集的完整结果始终保留。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS announcements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('original', 'correction')),
    project_name    TEXT NOT NULL,
    published_at    TEXT NOT NULL,
    deadline        TEXT,
    corrects_code   TEXT,
    detail_url      TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_announcements_project
    ON announcements(project_name);
CREATE INDEX IF NOT EXISTS idx_announcements_corrects
    ON announcements(corrects_code);

CREATE TABLE IF NOT EXISTS collection_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url      TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('success', 'failed')),
    items_collected INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS announcement_evidence (
    code            TEXT PRIMARY KEY REFERENCES announcements(code),
    detail_url      TEXT NOT NULL,
    detail_sha256   TEXT NOT NULL,
    deadline_text   TEXT,
    deadline_row    INTEGER,
    run_id          INTEGER NOT NULL REFERENCES collection_runs(id),
    captured_at     TEXT NOT NULL
);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path or settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: str | None = None) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert_announcements(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    """在调用方给定的事务内 UPSERT；重复 code 不产生重复记录。"""
    now = utcnow_iso()
    # 原公告先写，保证更正公告引用的数据已存在（顺序不影响业务判定，仅约束友好）
    rows = sorted(rows, key=lambda r: 0 if r["kind"] == "original" else 1)
    conn.executemany(
        """
        INSERT INTO announcements
            (code, title, kind, project_name, published_at, deadline,
             corrects_code, detail_url, last_seen_at)
        VALUES
            (:code, :title, :kind, :project_name, :published_at, :deadline,
             :corrects_code, :detail_url, :last_seen_at)
        ON CONFLICT(code) DO UPDATE SET
            title=excluded.title,
            kind=excluded.kind,
            project_name=excluded.project_name,
            published_at=excluded.published_at,
            deadline=excluded.deadline,
            corrects_code=excluded.corrects_code,
            detail_url=excluded.detail_url,
            last_seen_at=excluded.last_seen_at
        """,
        [{**r, "last_seen_at": now} for r in rows],
    )


def upsert_evidence(
    conn: sqlite3.Connection, rows: list[dict[str, Any]], run_id: int
) -> None:
    """与 upsert_announcements 同一事务内调用：证据随公告记录原子切换，
    run_id 标记本轮采集，保证追溯查询与项目查询对应同轮内容。"""
    captured_at = utcnow_iso()
    conn.executemany(
        """
        INSERT INTO announcement_evidence
            (code, detail_url, detail_sha256, deadline_text, deadline_row,
             run_id, captured_at)
        VALUES
            (:code, :detail_url, :detail_sha256, :deadline, :deadline_row,
             :run_id, :captured_at)
        ON CONFLICT(code) DO UPDATE SET
            detail_url=excluded.detail_url,
            detail_sha256=excluded.detail_sha256,
            deadline_text=excluded.deadline_text,
            deadline_row=excluded.deadline_row,
            run_id=excluded.run_id,
            captured_at=excluded.captured_at
        """,
        [{**r, "run_id": run_id, "captured_at": captured_at} for r in rows],
    )


def record_run(
    conn: sqlite3.Connection,
    *,
    source_url: str,
    status: str,
    items_collected: int,
    error: str | None,
    started_at: str,
) -> int:
    """记录一次采集运行，返回运行 id（供证据行标记来源轮次）。"""
    cur = conn.execute(
        """
        INSERT INTO collection_runs
            (source_url, status, items_collected, error, started_at, finished_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (source_url, status, items_collected, error, started_at, utcnow_iso()),
    )
    return cur.lastrowid


def fetch_all_announcements(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM announcements ORDER BY project_name, published_at, code"
        )
    )


def fetch_announcement(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM announcements WHERE code = ?", (code,)
    ).fetchone()


def fetch_group_with_evidence(
    conn: sqlite3.Connection, original_code: str
) -> list[sqlite3.Row]:
    """取原公告及其全部更正公告，并联出各自当前的追溯证据。"""
    return list(
        conn.execute(
            """
            SELECT a.*, e.detail_sha256, e.deadline_text, e.deadline_row,
                   e.run_id AS evidence_run_id, e.captured_at AS evidence_captured_at
            FROM announcements a
            LEFT JOIN announcement_evidence e ON e.code = a.code
            WHERE a.code = ? OR a.corrects_code = ?
            ORDER BY a.published_at, a.code
            """,
            (original_code, original_code),
        )
    )


def fetch_runs(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM collection_runs ORDER BY id DESC LIMIT ?", (limit,)
        )
    )
