"""追溯 API 测试：截止时间原文依据、证据哈希、表格行号、生效/沿用标注，
以及复采字段变化后追溯与项目查询对应同轮内容。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .conftest import DEMO_DIR, collect, gen


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def trace(client, code: str):
    resp = client.get(f"/api/traceability/{code}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_traceability_lists_group_ordered_with_evidence(client, demo_server):
    c, _, _ = client
    base, _, _ = demo_server
    collect(c, base)

    data = trace(c, "SZ-2026-002")
    assert data["original_code"] == "SZ-2026-002"
    assert data["project_name"] == "城北污水处理厂扩建工程"
    assert data["current_deadline"] == "2026-11-30 10:00"
    assert data["effective_code"] == "SZ-2026-002-G2"
    assert data["effective_url"].endswith("/SZ-2026-002-G2.html")

    entries = data["entries"]
    # 按发布时间升序：原公告 -> 第一次更正 -> 第二次更正
    assert [e["code"] for e in entries] == [
        "SZ-2026-002",
        "SZ-2026-002-G1",
        "SZ-2026-002-G2",
    ]
    published = [e["published_at"] for e in entries]
    assert published == sorted(published)

    # 每条都带截止时间原文、详情链接、表格行号与详情 HTML 的 SHA-256
    for e in entries:
        assert e["detail_url"].endswith(f"/{e['code']}.html")
        assert e["detail_sha256"] == sha256_of(DEMO_DIR / f"{e['code']}.html")
        assert e["deadline_row"] == 5  # 生成页面中截止时间固定在第 5 行
        assert e["collected_run_id"] is not None

    assert [e["deadline_text"] for e in entries] == [
        "2026-11-10 09:00",
        "2026-11-18 09:00",
        "2026-11-30 10:00",
    ]

    # 生效依据只有一条，且是最新一次更正
    effective = [e for e in entries if e["is_effective"]]
    assert len(effective) == 1
    assert effective[0]["code"] == "SZ-2026-002-G2"
    assert "生效依据" in effective[0]["note"]
    # 被取代的公告有说明
    assert "SZ-2026-002-G2" in entries[0]["note"]
    assert "取代" in entries[1]["note"]


def test_traceability_marks_inheritance_for_correction_without_deadline(
    client, demo_server
):
    c, _, _ = client
    base, _, _ = demo_server
    collect(c, base)

    data = trace(c, "SZ-2026-004")
    assert data["effective_code"] == "SZ-2026-004"  # 生效值仍来自原公告

    g1 = next(e for e in data["entries"] if e["code"] == "SZ-2026-004-G1")
    # 未写截止时间的更正：原文/行号为空，说明沿用原公告
    assert g1["deadline_text"] is None
    assert g1["deadline_row"] is None
    assert g1["is_effective"] is False
    assert "沿用" in g1["note"]
    assert "SZ-2026-004" in g1["note"]
    # 证据哈希仍然存在（页面本身被采集了）
    assert g1["detail_sha256"] == sha256_of(DEMO_DIR / "SZ-2026-004-G1.html")


def test_traceability_consecutive_corrections(client, demo_server):
    c, _, _ = client
    base, _, _ = demo_server
    collect(c, base)

    data = trace(c, "SZ-2026-006")
    entries = data["entries"]
    assert [e["code"] for e in entries] == [
        "SZ-2026-006",
        "SZ-2026-006-G1",
        "SZ-2026-006-G2",
        "SZ-2026-006-G3",
    ]
    # 连续更正链：G2 未写截止时间，沿用 G1；G3 为最终生效依据
    assert data["current_deadline"] == "2026-12-05 14:00"
    assert data["effective_code"] == "SZ-2026-006-G3"

    g2 = entries[2]
    assert g2["deadline_text"] is None
    assert "沿用" in g2["note"] and "SZ-2026-006-G1" in g2["note"]

    g1 = entries[1]
    assert g1["deadline_text"] == "2026-11-22 10:00"
    assert g1["is_effective"] is False
    assert "SZ-2026-006-G3" in g1["note"]


def test_traceability_unknown_code_returns_404(client, demo_server):
    c, _, _ = client
    base, _, _ = demo_server
    collect(c, base)
    resp = c.get("/api/traceability/SZ-2099-999")
    assert resp.status_code == 404


def test_traceability_correction_code_returns_400(client, demo_server):
    c, _, _ = client
    base, _, _ = demo_server
    collect(c, base)
    resp = c.get("/api/traceability/SZ-2026-002-G1")
    assert resp.status_code == 400
    assert "SZ-2026-002" in resp.json()["detail"]


def test_recollection_with_detail_changes_switches_to_same_round(
    client, mutable_site
):
    """同一详情页复采后字段变化：项目查询与追溯证据必须同步切换到新一轮。"""
    c, _, database = client
    base, site_dir, _ = mutable_site

    collect(c, base)
    before = trace(c, "SZ-2026-001")
    g1_before = next(e for e in before["entries"] if e["code"] == "SZ-2026-001-G1")
    assert g1_before["deadline_text"] == "2026-11-25 14:30"
    old_sha = g1_before["detail_sha256"]
    old_run = g1_before["collected_run_id"]

    # 源站改稿：同一 URL 的详情页截止时间变化（用生成器产出同格式新页面）
    changed = gen.apply_changes(gen.ANNOUNCEMENTS, gen.ROUND2_CHANGES)
    for a in changed:
        (site_dir / a.filename).write_text(gen._detail_page(a), encoding="utf-8")
    collect(c, base)

    # 项目查询看到新一轮的值
    projects = {p["project_name"]: p for p in c.get("/api/projects").json()}
    assert projects["滨河路市政改造工程"]["current_deadline"] == "2026-12-02 14:30"
    assert projects["滨江大道地下综合管廊工程"]["current_deadline"] == "2026-12-08 09:00"

    # 追溯看到同一轮的内容：原文、SHA-256、轮次 id 全部更新
    after = trace(c, "SZ-2026-001")
    g1_after = next(e for e in after["entries"] if e["code"] == "SZ-2026-001-G1")
    assert after["current_deadline"] == "2026-12-02 14:30"
    assert g1_after["deadline_text"] == "2026-12-02 14:30"
    assert g1_after["detail_sha256"] == sha256_of(site_dir / "SZ-2026-001-G1.html")
    assert g1_after["detail_sha256"] != old_sha
    assert g1_after["collected_run_id"] > old_run
    # 未变化的公告也重采了一轮，证据与公告记录同属最新轮次
    runs = {e["collected_run_id"] for e in after["entries"]}
    assert runs == {g1_after["collected_run_id"]}

    # 证据仍与公告一一对应，无重复
    with database.get_conn() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM announcement_evidence").fetchone()[0]
            == 14
        )


def test_failed_recollection_keeps_previous_evidence(client, mutable_site):
    """任一页面失败时，公告与证据都保留上次完整结果。"""
    c, _, _ = client
    base, site_dir, handler = mutable_site

    collect(c, base)
    before = trace(c, "SZ-2026-001")

    # 源站改稿后采集失败（某详情页 404）
    changed = gen.apply_changes(gen.ANNOUNCEMENTS, gen.ROUND2_CHANGES)
    for a in changed:
        (site_dir / a.filename).write_text(gen._detail_page(a), encoding="utf-8")
    handler.fail_paths = {"/SZ-2026-006-G3.html"}
    try:
        resp = c.post("/api/collect", json={"list_url": f"{base}/list_1.html"})
        assert resp.status_code == 422
    finally:
        handler.fail_paths = set()

    # 追溯与项目查询都仍是上一轮完整内容
    after = trace(c, "SZ-2026-001")
    assert after == before
    projects = {p["project_name"]: p for p in c.get("/api/projects").json()}
    assert projects["滨河路市政改造工程"]["current_deadline"] == "2026-11-25 14:30"
