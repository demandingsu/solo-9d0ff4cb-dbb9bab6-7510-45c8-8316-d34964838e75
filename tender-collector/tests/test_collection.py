"""端到端测试：用线程内静态 HTTP 服务器托管生成的示例公告站，
通过 FastAPI TestClient 走完整的采集/查询/失败保留流程。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from .conftest import collect, gen, project_map


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------
def test_full_collection_and_deadline_resolution(client, demo_server):
    c, main, _ = client
    base, _, _ = demo_server
    payload = collect(c, base)
    assert payload["collected"] == 14

    projects = project_map(payload)
    assert set(projects) == {a.project_name for a in gen.ANNOUNCEMENTS}

    # A：更正延期生效
    a = projects["滨河路市政改造工程"]
    assert a["current_deadline"] == "2026-11-25 14:30"
    assert a["deadline_source_code"] == "SZ-2026-001-G1"
    assert a["deadline_source_url"].endswith("SZ-2026-001-G1.html")
    assert a["is_expired"] is False

    # B：两次更正，以发布时间最新的第二次为准（不能受页面顺序影响）
    b = projects["城北污水处理厂扩建工程"]
    assert b["current_deadline"] == "2026-11-30 10:00"
    assert b["deadline_source_code"] == "SZ-2026-002-G2"

    # C：无更正、已过期
    cproj = projects["东风桥加固维修工程"]
    assert cproj["current_deadline"] == "2026-10-30 17:00"
    assert cproj["deadline_source_code"] == "SZ-2026-003"
    assert cproj["is_expired"] is True

    # D：最新更正未给截止时间，沿用上一份带时间的公告（原公告）
    d = projects["高新区雨污分流管网工程"]
    assert d["current_deadline"] == "2026-11-28 09:30"
    assert d["deadline_source_code"] == "SZ-2026-004"

    # E：原公告在最后一页、更正公告在第一页（跨页乱序）
    e = projects["湖滨公园配套市政道路工程"]
    assert e["current_deadline"] == "2026-11-20 15:00"
    assert e["deadline_source_code"] == "SZ-2026-005-G1"

    # F：连续三次更正，第二次未写截止时间（沿用 G1），第三次再次调整
    f = projects["滨江大道地下综合管廊工程"]
    assert f["current_deadline"] == "2026-12-05 14:00"
    assert f["deadline_source_code"] == "SZ-2026-006-G3"


def test_order_independence():
    """打乱输入顺序，归并结果必须一致。"""
    from app.merge import merge_projects

    rows = [
        {
            "code": a.code,
            "title": a.title,
            "kind": a.kind,
            "project_name": a.project_name,
            "published_at": a.published_at,
            "deadline": a.deadline,
            "corrects_code": a.corrects_code,
            "detail_url": f"http://x/{a.code}.html",
        }
        for a in gen.ANNOUNCEMENTS
    ]
    baseline = merge_projects(rows)
    shuffled = list(reversed(rows))
    import random

    random.Random(42).shuffle(shuffled)
    assert merge_projects(shuffled) == baseline


def test_duplicate_collection_creates_no_duplicates(client, demo_server):
    c, main, database = client
    base, _, _ = demo_server
    collect(c, base)
    collect(c, base)  # 重复采集

    with database.get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0]
        distinct = conn.execute(
            "SELECT COUNT(DISTINCT code) FROM announcements"
        ).fetchone()[0]
        evidence = conn.execute(
            "SELECT COUNT(*) FROM announcement_evidence"
        ).fetchone()[0]
        runs = conn.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0]
    assert total == 14
    assert distinct == 14
    assert evidence == 14  # 证据与公告一一对应，重复采集不产生重复证据
    assert runs == 2

    # 查询接口仍然是 6 个项目
    resp = c.get("/api/projects")
    assert resp.status_code == 200
    assert len(resp.json()) == 6


def test_detail_failure_keeps_last_successful_result(client, demo_server):
    c, main, database = client
    base, httpd, handler = demo_server

    # 首次成功采集
    first = collect(c, base)
    assert first["collected"] == 14

    # 注入故障：某个详情页 404
    handler.fail_paths = {"/SZ-2026-002-G2.html"}
    try:
        resp = c.post(
            "/api/collect", json={"list_url": f"{base}/list_1.html"}
        )
        assert resp.status_code == 422
        assert "SZ-2026-002-G2" in resp.json()["detail"] or "HTTP 404" in resp.json()["detail"]
    finally:
        handler.fail_paths = set()

    # 上次成功采集的完整结果必须保留
    resp = c.get("/api/projects")
    projects = {p["project_name"]: p for p in resp.json()}
    assert len(projects) == 6
    assert (
        projects["城北污水处理厂扩建工程"]["deadline_source_code"]
        == "SZ-2026-002-G2"
    )

    # 失败被记录，且不影响后续恢复采集
    runs = c.get("/api/collections").json()
    statuses = [r["status"] for r in runs]
    assert "failed" in statuses and "success" in statuses

    again = collect(c, base)
    assert again["collected"] == 14


def test_list_page_failure_returns_error(client, demo_server):
    c, main, database = client
    base, httpd, handler = demo_server
    collect(c, base)

    handler.fail_paths = {"/list_2.html"}
    try:
        resp = c.post(
            "/api/collect", json={"list_url": f"{base}/list_1.html"}
        )
        assert resp.status_code == 422
        assert "list_2" in resp.json()["detail"]
    finally:
        handler.fail_paths = set()

    # 数据仍是上次成功的 14 条
    with database.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 14
        assert (
            conn.execute("SELECT COUNT(*) FROM announcement_evidence").fetchone()[0]
            == 14
        )


def test_correction_referencing_missing_original_rejected():
    from app.merge import MergeError, merge_projects

    rows = [
        {
            "code": "X-1", "title": "t", "kind": "correction",
            "project_name": "P", "published_at": "2026-11-01 10:00",
            "deadline": "2026-12-01 10:00", "corrects_code": "X-0",
            "detail_url": "http://x/X-1.html",
        }
    ]
    with pytest.raises(MergeError, match="X-0"):
        merge_projects(rows)


def test_expired_flag_uses_fixed_now(client, demo_server):
    """注入固定当前时间验证过期判定边界。"""
    from app import service

    c, main, _ = client
    base, _, _ = demo_server
    collect(c, base)

    fixed_now = datetime(2026, 11, 26, 0, 0)
    projects = {p["project_name"]: p for p in service.query_projects(fixed_now)}
    # A 的截止 11-25 已过期；B 的 11-30 未过期
    assert projects["滨河路市政改造工程"]["is_expired"] is True
    assert projects["城北污水处理厂扩建工程"]["is_expired"] is False
