"""采集编排服务：抓取 -> 归并校验 -> 单事务落库；失败时保留上次成功结果。

追溯查询：按原公告编号列出原公告及各次更正的截止时间原文、详情链接、
字段所在表格行与详情 HTML 的 SHA-256，并标明当前生效依据；未声明截止
时间的更正标注“沿用”来源。
"""
from __future__ import annotations

from datetime import datetime

from . import database
from .merge import (
    EffectiveDeadline,
    MergeError,
    effective_source,
    merge_projects,
    order_group,
)
from .scraper import CollectionError, collect

TIME_FORMAT = "%Y-%m-%d %H:%M"


class ServiceError(Exception):
    """对外统一的采集错误（带可读原因）。"""


class TraceabilityNotFound(Exception):
    """追溯的原公告编号不存在。"""


class TraceabilityNotOriginal(Exception):
    """输入的编号不是原公告（而是更正公告）。"""


def _is_expired(deadline_text: str, now: datetime | None = None) -> bool:
    try:
        deadline = datetime.strptime(deadline_text, TIME_FORMAT)
    except ValueError:
        # 容错：支持带秒或 ISO 形式
        deadline = datetime.fromisoformat(deadline_text)
    return deadline <= (now or datetime.now())


def _to_project(d: EffectiveDeadline, now: datetime | None = None) -> dict:
    return {
        "project_name": d.project_name,
        "current_deadline": d.deadline,
        "deadline_source_code": d.source_code,
        "deadline_source_url": d.source_url,
        "is_expired": _is_expired(d.deadline, now),
    }


def build_projects(rows: list[dict], now: datetime | None = None) -> list[dict]:
    return [_to_project(d, now) for d in merge_projects(rows)]


def run_collection(list_url: str) -> dict:
    started_at = database.utcnow_iso()
    try:
        raw = collect(list_url)
        # 先在内存完成归并校验：数据有问题时不写库
        projects = build_projects(raw)
    except (CollectionError, MergeError) as exc:
        # 记录失败采集；announcements/evidence 完全不动，保留上次成功结果
        with database.get_conn() as conn:
            database.record_run(
                conn,
                source_url=list_url,
                status="failed",
                items_collected=0,
                error=str(exc),
                started_at=started_at,
            )
            conn.commit()
        raise ServiceError(str(exc)) from exc

    # 成功：单事务写入 运行记录 + 公告 UPSERT + 证据 UPSERT，保证原子性。
    # 复采后字段变化时，公告记录与证据在同一事务切换到本轮内容，
    # 项目查询与追溯查询永远对应同一轮采集结果。
    with database.get_conn() as conn:
        try:
            run_id = database.record_run(
                conn,
                source_url=list_url,
                status="success",
                items_collected=len(raw),
                error=None,
                started_at=started_at,
            )
            database.upsert_announcements(conn, raw)
            database.upsert_evidence(conn, raw, run_id)
            conn.commit()
        except Exception as exc:  # pragma: no cover - 兜底回滚
            conn.rollback()
            raise ServiceError(f"写入数据库失败: {exc}") from exc

    return {"source_url": list_url, "collected": len(raw), "projects": projects}


def query_projects(now: datetime | None = None) -> list[dict]:
    with database.get_conn() as conn:
        rows = [dict(r) for r in database.fetch_all_announcements(conn)]
    if not rows:
        return []
    return build_projects(rows, now)


def query_traceability(code: str, now: datetime | None = None) -> dict:
    """按原公告编号追溯生效截止时间的原文依据。

    返回按发布时间排序的原公告 + 各次更正，每条附截止时间原文、详情链接、
    字段所在表格行、详情 HTML 的 SHA-256 及生效/沿用说明。
    """
    code = code.strip()
    with database.get_conn() as conn:
        target = database.fetch_announcement(conn, code)
        if target is None:
            raise TraceabilityNotFound(f"公告 {code} 不存在，请先采集")
        if target["kind"] != "original":
            raise TraceabilityNotOriginal(
                f"{code} 是更正公告（更正 {target['corrects_code']}），"
                f"请改用原公告编号 {target['corrects_code']} 查询"
            )
        rows = [dict(r) for r in database.fetch_group_with_evidence(conn, code)]

    ordered = order_group(rows)
    source = effective_source(ordered)
    if source is None:  # 成功采集的项目不可能出现；防御性处理
        raise ServiceError(f"项目 {target['project_name']} 的所有公告均未给出投标截止时间")

    effective_code = source["code"]
    entries: list[dict] = []
    last_deadline_code: str | None = None  # 当前位置之前最近一份声明截止时间的公告
    for r in ordered:
        has_deadline = bool(r["deadline"])
        is_effective = r["code"] == effective_code
        if is_effective:
            note = "生效依据：发布时间最新且声明投标截止时间的公告，当前查询值以此为准"
        elif has_deadline:
            note = f"其截止时间已被后续公告 {effective_code} 取代，未生效"
        elif last_deadline_code is not None:
            note = f"未声明投标截止时间，沿用 {last_deadline_code} 的截止时间"
        else:
            note = "未声明投标截止时间，且此前公告均未给出截止时间"
        entries.append(
            {
                "code": r["code"],
                "kind": r["kind"],
                "title": r["title"],
                "published_at": r["published_at"],
                "deadline_text": r["deadline_text"],
                "deadline_row": r["deadline_row"],
                "detail_url": r["detail_url"],
                "detail_sha256": r["detail_sha256"],
                "collected_run_id": r["evidence_run_id"],
                "is_effective": is_effective,
                "note": note,
            }
        )
        if has_deadline:
            last_deadline_code = r["code"]

    return {
        "original_code": code,
        "project_name": target["project_name"],
        "current_deadline": source["deadline"],
        "effective_code": effective_code,
        "effective_url": source["detail_url"],
        "is_expired": _is_expired(source["deadline"], now),
        "entries": entries,
    }
