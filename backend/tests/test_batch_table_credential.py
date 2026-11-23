"""批次表格抽取凭据集成测试。

模拟同批三份文档的真实并发场景：
- DOC-GOOD：版式齐全且首次提交成功（好文档，后续重试必须幂等、不翻倍）；
- DOC-REGION：先缺版式被阻断，再经失败区域重跑补齐版本后提交成功；
- DOC-STALE：在整组出现 v2 后仍停留在旧版式，提交必须被拒绝。

同时验证逐单元格明细按「文档+版本」幂等写入，以及表格对版式版本/区域的回查。
"""
import json
import os
import sys
import uuid as _uuid

import pytest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from app.services.base import DATA_ROOT
from app.services.format_parse import FormatParseService
from app.services.layout_restore import LayoutRestoreService, bump_version, version_rank
from app.services.table_extract import (
    SUBMIT_BLOCKED,
    SUBMIT_DONE,
    SUBMIT_PENDING,
    VALIDATION_MISSING,
    VALIDATION_OK,
    VALIDATION_STALE,
    TableExtractService,
)


def _uid(prefix="BT"):
    return f"{prefix}-{_uuid.uuid4().hex[:8].upper()}"


@pytest.fixture()
def three_doc_batch():
    """准备同批三份文档：均已解析；其中一份稍后移除版式模拟缺版式。"""
    batch_key = _uid("BATCH")
    good_doc = f"{_uid('GOOD')}.pdf"
    region_doc = f"{_uid('REGION')}.pdf"
    stale_doc = f"{_uid('STALE')}.pdf"

    parse_svc = FormatParseService()
    for doc_id in (good_doc, region_doc, stale_doc):
        task, _ = parse_svc.create_entry({
            "任务编号": _uid("PR"), "文档编号": doc_id, "解析器类型": "PDF",
        })
        parse_svc.execute_parse(task["id"])
        # 解析自动触发表式还原，确认 v1 版式与表格区域就绪
        LayoutRestoreService().restore_from_document(doc_id)

    return {
        "batch_key": batch_key,
        "good": good_doc,
        "region": region_doc,
        "stale": stale_doc,
    }


def _layout_file(doc_id):
    return DATA_ROOT / "layout" / f"{doc_id}_layout.jsonl"


def _remove_layout(doc_id):
    """删除版式产物（保留解析产物与原始区域），模拟个别文档缺版式。"""
    layout_file = _layout_file(doc_id)
    if layout_file.exists():
        layout_file.unlink()


class TestCredentialCreationAndValidation:
    def test_credential_marks_missing_and_valid_docs(self, three_doc_batch):
        ctx = three_doc_batch
        svc = TableExtractService()
        # 让 region 文档缺版式
        _remove_layout(ctx["region"])

        result = svc.create_batch_credential(
            ctx["batch_key"], [ctx["good"], ctx["region"], ctx["stale"]]
        )
        assert "error" not in result
        assert result["summary"]["total"] == 3
        assert result["summary"]["layout_missing"] == 1
        blocked = {d["doc_id"]: d for d in result["blocked_docs"]}
        assert ctx["region"] in blocked
        assert blocked[ctx["region"]]["validation"] == VALIDATION_MISSING
        # 两份有版式的文档可单独处理
        individually = {d["doc_id"] for d in result["individually_processable_docs"]}
        assert {ctx["good"], ctx["stale"]} <= individually
        # 整组尚未就绪
        assert result["group_ready"] is False

    def test_duplicate_credential_rejected(self, three_doc_batch):
        ctx = three_doc_batch
        svc = TableExtractService()
        docs = [ctx["good"], ctx["region"], ctx["stale"]]
        svc.create_batch_credential(ctx["batch_key"], docs)
        again = svc.create_batch_credential(ctx["batch_key"], docs)
        assert "error" in again


class TestGoodDocSubmissionAndIdempotency:
    def test_good_doc_submits_and_is_verifiable(self, three_doc_batch):
        ctx = three_doc_batch
        svc = TableExtractService()
        _remove_layout(ctx["region"])
        svc.create_batch_credential(
            ctx["batch_key"], [ctx["good"], ctx["region"], ctx["stale"]]
        )

        result = svc.submit_doc_tables(ctx["batch_key"], ctx["good"])
        assert result["ok"] is True
        assert result["result"] == "submitted"
        assert result["layout_version"] == "v1"
        assert result["tables"] > 0
        assert result["cells"] > 0

        # 单元格明细按文档+版本落盘，且每条带版式版本与区域
        cells_file = DATA_ROOT / "tables" / f"{ctx['good']}_cells_v1.jsonl"
        assert cells_file.exists()
        cells = [json.loads(line) for line in cells_file.read_text(encoding="utf-8").splitlines()]
        assert len(cells) == result["cells"]
        assert all(c["layout_version"] == "v1" for c in cells)
        assert all(c["bbox"] for c in cells)

        # 好文档进入可加入整组确认清单
        confirmation = svc.get_group_confirmation(ctx["batch_key"])
        confirmable = {d["doc_id"] for d in confirmation["confirmable_docs"]}
        assert ctx["good"] in confirmable

    def test_retry_good_doc_does_not_duplicate(self, three_doc_batch):
        ctx = three_doc_batch
        svc = TableExtractService()
        _remove_layout(ctx["region"])
        svc.create_batch_credential(
            ctx["batch_key"], [ctx["good"], ctx["region"], ctx["stale"]]
        )
        first = svc.submit_doc_tables(ctx["batch_key"], ctx["good"])
        cells_file = DATA_ROOT / "tables" / f"{ctx['good']}_cells_v1.jsonl"
        count_after_first = len(cells_file.read_text(encoding="utf-8").splitlines())

        # 坏文档并发重试场景下，好文档被再次提交：必须幂等跳过
        retry = svc.retry_doc(ctx["batch_key"], ctx["good"])
        assert retry["result"] == "duplicate_skipped"
        count_after_retry = len(cells_file.read_text(encoding="utf-8").splitlines())
        assert count_after_first == count_after_retry
        assert retry["tables"] == first["tables"]

        # 文档级表格产物也不能翻倍
        tables_file = DATA_ROOT / "tables" / f"{ctx['good']}_tables.jsonl"
        table_records = [
            json.loads(line) for line in tables_file.read_text(encoding="utf-8").splitlines()
        ]
        table_keys = [r["table_key"] for r in table_records]
        assert len(table_keys) == len(set(table_keys))


class TestMissingLayoutBlockingAndRegionRerun:
    def test_missing_layout_blocks_without_writing(self, three_doc_batch):
        ctx = three_doc_batch
        svc = TableExtractService()
        _remove_layout(ctx["region"])
        svc.create_batch_credential(
            ctx["batch_key"], [ctx["good"], ctx["region"], ctx["stale"]]
        )

        result = svc.submit_doc_tables(ctx["batch_key"], ctx["region"])
        assert result["ok"] is False
        assert result["result"] == "blocked_layout_missing"
        # 阻断时不能写出任何单元格/表格产物
        assert not list((DATA_ROOT / "tables").glob(f"{ctx['region']}_cells_*.jsonl"))
        assert not (DATA_ROOT / "tables" / f"{ctx['region']}_tables.jsonl").exists()

    def test_region_rerun_corrects_coordinates_and_submits_v2(self, three_doc_batch):
        ctx = three_doc_batch
        svc = TableExtractService()
        layout_svc = LayoutRestoreService()
        _remove_layout(ctx["region"])
        svc.create_batch_credential(
            ctx["batch_key"], [ctx["good"], ctx["region"], ctx["stale"]]
        )

        # 先恢复 v1 版式，再对失败页做区域重跑，得到 v2 修正版式
        layout_svc.restore_from_document(ctx["region"])
        before = layout_svc.layout_region_trace(ctx["region"])
        v1_bboxes = {item["page"]: item["table_bboxes"] for item in before
                     if item["layout_version"] == "v1"}

        failed_page = 1
        rerun = svc.rerun_layout_regions(ctx["batch_key"], ctx["region"], [failed_page])
        assert rerun["result"] == "layout_rerun"
        assert rerun["layout_version"] == "v2"
        assert rerun["previous_version"] == "v1"
        assert failed_page in rerun["pages_rerun"]
        # 修正页坐标确实变化，可追回
        corrected = rerun["corrected_regions"]
        assert corrected and corrected[0]["old_bbox"] != corrected[0]["corrected_bbox"]

        trace = layout_svc.layout_region_trace(ctx["region"])
        v2_pages = [t for t in trace if t["layout_version"] == "v2"]
        corrected_page = next(t for t in v2_pages if t["page"] == failed_page)
        assert corrected_page["region_status"] == "corrected"
        assert corrected_page["table_bboxes"] != v1_bboxes[failed_page]
        # 未失败页坐标保留
        unchanged = next(t for t in v2_pages if t["page"] != failed_page)
        assert unchanged["region_status"] == "unchanged"

        # 按补齐的 v2 版本写入对应文档单元格明细
        result = svc.submit_doc_tables(ctx["batch_key"], ctx["region"])
        assert result["ok"] is True
        assert result["layout_version"] == "v2"
        cells_file = DATA_ROOT / "tables" / f"{ctx['region']}_cells_v2.jsonl"
        assert cells_file.exists()
        cells = [json.loads(line) for line in cells_file.read_text(encoding="utf-8").splitlines()]
        failed_cells = [c for c in cells if c["page"] == failed_page]
        assert failed_cells
        corrected_bbox = corrected[0]["corrected_bbox"]
        assert all(c["bbox"] == corrected_bbox for c in failed_cells)
        assert all(c["region_status"] == "corrected" for c in failed_cells)


class TestStaleLayoutRejection:
    def test_confirmed_good_doc_stays_verifiable_when_group_moves_on(self, three_doc_batch):
        """整组出现更新版式后，已确认的好文档仍可核验、可整组确认，重试幂等不阻断。"""
        ctx = three_doc_batch
        svc = TableExtractService()
        _remove_layout(ctx["region"])
        svc.create_batch_credential(
            ctx["batch_key"], [ctx["good"], ctx["region"], ctx["stale"]]
        )
        # 好文档先在 v1 确认
        good = svc.submit_doc_tables(ctx["batch_key"], ctx["good"])
        assert good["result"] == "submitted"
        cells_file = DATA_ROOT / "tables" / f"{ctx['good']}_cells_v1.jsonl"
        first_count = len(cells_file.read_text(encoding="utf-8").splitlines())

        # 另一文档区域重跑，整组目标版本升到 v2
        LayoutRestoreService().restore_from_document(ctx["region"])
        svc.rerun_layout_regions(ctx["batch_key"], ctx["region"], [1])
        validation = svc.validate_batch_layouts(ctx["batch_key"])

        # 好文档磁盘版式仍是 v1，被标注旧版式，但保留已提交、留在可确认清单
        good_view = next(
            d for d in validation["confirmable_docs"] if d["doc_id"] == ctx["good"]
        )
        assert good_view["submit_status"] == SUBMIT_DONE
        assert good_view["submitted_version"] == "v1"
        assert good_view["validation"] == VALIDATION_STALE
        assert good_view["newer_version_available"] == "v2"

        # 重试好文档命中幂等，不被旧版式门禁拒绝，单元格不翻倍
        retry = svc.retry_doc(ctx["batch_key"], ctx["good"])
        assert retry["result"] == "duplicate_skipped"
        assert len(cells_file.read_text(encoding="utf-8").splitlines()) == first_count

    def test_stale_doc_cannot_submit_old_regions(self, three_doc_batch):
        ctx = three_doc_batch
        svc = TableExtractService()
        _remove_layout(ctx["region"])
        svc.create_batch_credential(
            ctx["batch_key"], [ctx["good"], ctx["region"], ctx["stale"]]
        )

        # region 文档区域重跑后整组目标版本升到 v2
        LayoutRestoreService().restore_from_document(ctx["region"])
        svc.rerun_layout_regions(ctx["batch_key"], ctx["region"], [1])
        validation = svc.validate_batch_layouts(ctx["batch_key"])
        assert validation["layout_set_version"] == "v2"

        # stale 文档仍停留在 v1：校验标记旧版式
        stale_state = next(
            d for d in validation["blocked_docs"] if d["doc_id"] == ctx["stale"]
        )
        assert stale_state["validation"] == VALIDATION_STALE

        # 直接提交旧版式必须被拒绝，且不写出单元格
        result = svc.submit_doc_tables(ctx["batch_key"], ctx["stale"])
        assert result["ok"] is False
        assert result["result"] == "rejected_stale_layout"
        assert not list((DATA_ROOT / "tables").glob(f"{ctx['stale']}_cells_*.jsonl"))

        # 显式指定旧版本同样被拒绝
        explicit = svc.submit_doc_tables(
            ctx["batch_key"], ctx["stale"], layout_version="v1"
        )
        assert explicit["result"] == "rejected_stale_layout"

    def test_stale_doc_recovers_after_updating_layout(self, three_doc_batch):
        ctx = three_doc_batch
        svc = TableExtractService()
        _remove_layout(ctx["region"])
        svc.create_batch_credential(
            ctx["batch_key"], [ctx["good"], ctx["region"], ctx["stale"]]
        )
        LayoutRestoreService().restore_from_document(ctx["region"])
        svc.rerun_layout_regions(ctx["batch_key"], ctx["region"], [1])

        # stale 文档自己也重跑补齐到 v2 后即可提交
        svc.rerun_layout_regions(ctx["batch_key"], ctx["stale"], [1])
        result = svc.submit_doc_tables(ctx["batch_key"], ctx["stale"])
        assert result["ok"] is True
        assert result["layout_version"] == "v2"
        assert result["result"] == "submitted"


class TestReceiptsAndTrace:
    def test_receipt_chain_records_every_outcome(self, three_doc_batch):
        ctx = three_doc_batch
        svc = TableExtractService()
        _remove_layout(ctx["region"])
        svc.create_batch_credential(
            ctx["batch_key"], [ctx["good"], ctx["region"], ctx["stale"]]
        )
        svc.submit_doc_tables(ctx["batch_key"], ctx["good"])
        svc.submit_doc_tables(ctx["batch_key"], ctx["region"])  # blocked
        svc.retry_doc(ctx["batch_key"], ctx["good"])             # duplicate

        receipts = svc.list_receipts(ctx["batch_key"])["receipts"]
        actions = [r["action"] for r in receipts]
        assert "credential_created" in actions
        blocked = [r for r in receipts if r["result"] == "blocked_layout_missing"]
        assert blocked and blocked[0]["doc_id"] == ctx["region"]
        duplicates = [r for r in receipts if r["result"] == "duplicate_skipped"]
        assert duplicates and duplicates[0]["doc_id"] == ctx["good"]

        # 可按文档过滤逐条查看
        region_receipts = svc.list_receipts(ctx["batch_key"], ctx["region"])["receipts"]
        assert all(r["doc_id"] == ctx["region"] for r in region_receipts)
        assert all(r.get("receipt_id") for r in receipts)

    def test_trace_returns_version_and_region_per_table(self, three_doc_batch):
        ctx = three_doc_batch
        svc = TableExtractService()
        svc.create_batch_credential(
            ctx["batch_key"], [ctx["good"], ctx["region"], ctx["stale"]]
        )
        svc.submit_doc_tables(ctx["batch_key"], ctx["good"])

        trace = svc.trace_table(ctx["good"])
        assert "error" not in trace
        assert trace["current_layout_version"] == "v1"
        assert trace["region_trace"], "应能回查每个页/版本的区域坐标"
        assert trace["cell_versions"], "应能回查已写单元格的版本"
        cell_version = trace["cell_versions"][0]
        assert cell_version["layout_version"] == "v1"
        assert cell_version["cell_count"] > 0

        # 按 table_key 精确回查
        table_key = cell_version["tables"][0]
        precise = svc.trace_table(ctx["good"], table_key)
        assert all(table_key in v["tables"] for v in precise["cell_versions"])


class TestVersionHelpers:
    def test_bump_and_rank(self):
        assert bump_version("v1") == "v2"
        assert bump_version("v9") == "v10"
        assert bump_version("") == "v1"
        assert bump_version("weird") == "v1"
        assert version_rank("v3") > version_rank("v1")
        assert version_rank("") == 0
