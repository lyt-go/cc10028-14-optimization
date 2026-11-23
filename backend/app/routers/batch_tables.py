"""批次表格抽取凭据路由：一组版式数据逐文档校验、提交、重跑与回执回查。

与通用资源路由分离，承载凭据驱动的状态化批次操作；路由层不做业务判断，
全部委派给 TableExtractService。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.table_extract import TableExtractService

router = APIRouter(prefix="/api/batch-tables", tags=["批次表格抽取凭据"])

_service = TableExtractService()


class CreateCredentialPayload(BaseModel):
    batch_key: str
    doc_ids: list[str] | None = None


class SubmitDocPayload(BaseModel):
    layout_version: str | None = None


class RerunRegionPayload(BaseModel):
    failed_pages: list[int] = Field(default_factory=list)


def _unwrap(result: dict[str, Any]) -> dict[str, Any]:
    """服务以 ok/error 表达失败，统一转成可读的 HTTP 错误。"""
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    if result.get("ok") is False:
        raise HTTPException(status_code=409, detail=result.get("message", "操作被业务规则阻断"))
    return result


@router.post("/credentials")
def create_credential(payload: CreateCredentialPayload) -> dict[str, Any]:
    """建立批次表格抽取凭据，登记整组文档的版式版本与区域。"""
    result = _service.create_batch_credential(payload.batch_key, payload.doc_ids)
    return _unwrap(result)


@router.get("/credentials/{batch_key}")
def get_credential(batch_key: str) -> dict[str, Any]:
    return _unwrap(_service.get_credential(batch_key))


@router.post("/credentials/{batch_key}/validate")
def validate_layouts(batch_key: str) -> dict[str, Any]:
    """逐文档重新校验版式版本，区分可提交、缺版式与旧版式。"""
    return _unwrap(_service.validate_batch_layouts(batch_key))


@router.get("/credentials/{batch_key}/confirmation")
def group_confirmation(batch_key: str) -> dict[str, Any]:
    """整组确认视图：好文档能否加入整组确认、坏文档能否单独处理。"""
    return _unwrap(_service.get_group_confirmation(batch_key))


@router.post("/credentials/{batch_key}/docs/{doc_id}/submit")
def submit_doc(batch_key: str, doc_id: str, payload: SubmitDocPayload) -> dict[str, Any]:
    """逐文档提交表格，带版式版本门禁与幂等保护。"""
    return _unwrap(
        _service.submit_doc_tables(batch_key, doc_id, payload.layout_version)
    )


@router.post("/credentials/{batch_key}/docs/{doc_id}/retry")
def retry_doc(batch_key: str, doc_id: str) -> dict[str, Any]:
    """重新提交未成功文档；已确认文档幂等跳过不翻倍。"""
    return _unwrap(_service.retry_doc(batch_key, doc_id))


@router.post("/credentials/{batch_key}/docs/{doc_id}/rerun-regions")
def rerun_regions(
    batch_key: str, doc_id: str, payload: RerunRegionPayload
) -> dict[str, Any]:
    """凭据关联失败区域，驱动版式还原局部重跑并刷新版本。"""
    return _unwrap(
        _service.rerun_layout_regions(batch_key, doc_id, payload.failed_pages)
    )


@router.get("/credentials/{batch_key}/receipts")
def list_receipts(batch_key: str, doc_id: str | None = None) -> dict[str, Any]:
    """逐条查看回执，可按文档过滤。"""
    return _unwrap(_service.list_receipts(batch_key, doc_id))


@router.get("/docs/{doc_id}/trace")
def trace_table(doc_id: str, table_key: str | None = None) -> dict[str, Any]:
    """回查每份表格的版式版本、页码区域与单元格明细。"""
    return _unwrap(_service.trace_table(doc_id, table_key))
