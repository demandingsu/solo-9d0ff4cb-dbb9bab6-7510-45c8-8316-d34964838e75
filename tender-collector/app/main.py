from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from . import database
from .models import (
    CollectRequest,
    CollectResponse,
    ProjectOut,
    RunOut,
    TraceabilityOut,
)
from .service import (
    ServiceError,
    TraceabilityNotFound,
    TraceabilityNotOriginal,
    query_projects,
    query_traceability,
    run_collection,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    yield


app = FastAPI(
    title="市政工程投标截止时间采集 API",
    description=(
        "提交公告列表页地址，服务抓取全部详情页，将更正公告按引用的原公告编号归并，"
        "按发布时间确定生效的投标截止时间。重复采集不产生重复记录；"
        "任一详情页读取失败时整体报错并保留上次成功采集结果。"
        "追溯接口按原公告编号列出原公告及各次更正的截止时间原文、详情链接、"
        "字段所在表格行与详情 HTML 的 SHA-256，标明生效依据。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {"service": "tender-collector", "docs": "/docs"}


@app.post("/api/collect", response_model=CollectResponse, status_code=200)
def collect_endpoint(req: CollectRequest) -> CollectResponse:
    try:
        result = run_collection(req.list_url)
    except ServiceError as exc:
        # 422 语义：采集到的源站内容/可用性有问题，本次未入库
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return CollectResponse(status="success", **result)


@app.get("/api/projects", response_model=list[ProjectOut])
def list_projects() -> list[ProjectOut]:
    return [ProjectOut(**p) for p in query_projects()]


@app.get("/api/traceability/{code}", response_model=TraceabilityOut)
def traceability(code: str) -> TraceabilityOut:
    """按原公告编号追溯生效截止时间的原文依据。"""
    try:
        result = query_traceability(code)
    except TraceabilityNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TraceabilityNotOriginal as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ServiceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return TraceabilityOut(**result)


@app.get("/api/collections", response_model=list[RunOut])
def list_collections(limit: int = Query(20, ge=1, le=200)) -> list[RunOut]:
    with database.get_conn() as conn:
        return [RunOut(**dict(r)) for r in database.fetch_runs(conn, limit)]
