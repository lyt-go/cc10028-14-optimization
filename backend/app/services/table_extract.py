"""表格抽取服务：文档表格的边界检测、表头行识别、合并单元格拆分与行列结构化输出。

从版式还原产物中读取表格区域，进行行列结构化抽取并持久化。

批次表格抽取凭据（核心增强）：
- 一组多文档先建立批次凭据，逐文档校验当前版式版本与区域，缺版式/旧版式分别标记，
  让复核员清楚哪些文档能加入整组确认、哪些只能单独处理。
- 每份表格提交时做版式版本门禁：缺版式阻断、旧版式拒绝（不把旧区域提交为新结果），
  已提交文档重试按幂等跳过（好文档不翻倍、不重复写出）。
- 单元格明细按「文档 + 版式版本」覆盖写入，并回写凭据与逐条回执，支持回查每份表格
  依据的版式版本、页码与区域坐标。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from app.services.base import BaseModuleService
from app.services.layout_restore import LayoutRestoreService, bump_version, version_rank
from app.store import store

# 凭据内文档的版式校验状态与提交状态
VALIDATION_OK = "valid"
VALIDATION_MISSING = "layout_missing"
VALIDATION_STALE = "stale_layout"
SUBMIT_PENDING = "pending"
SUBMIT_DONE = "submitted"
SUBMIT_BLOCKED = "blocked"


class TableExtractService(BaseModuleService):
    MODULE = "table_extract"
    ENTITY = "表格区域"
    OPERATION_TERM = "抽取"
    LIST_FIELDS = ["表格编号", "文档编号", "页码位置", "行数", "列数", "表头行号", "合并单元格数", "结构化输出"]
    REQUIRED_FIELDS = ["表格编号", "文档编号", "页码位置"]
    STATUS_ORDER = ["草稿", "待抽取", "已抽取", "抽取失败"]
    ACTION_RULES = {"启动抽取": "待抽取", "确认抽取": "已抽取", "标记失败": "抽取失败"}
    NEGATIVE_ACTIONS = ["标记失败"]

    # -- 凭据与产物路径 --

    def _credential_path(self, batch_key: str) -> Path:
        return self._data_dir("batch_tables") / f"{batch_key}_credential.json"

    def _receipt_path(self, batch_key: str) -> Path:
        return self._data_dir("batch_tables") / f"{batch_key}_receipts.jsonl"

    def _cells_path(self, doc_id: str, layout_version: str) -> Path:
        return self._data_dir("tables") / f"{doc_id}_cells_{layout_version}.jsonl"

    def _overwrite_jsonl(self, filepath: Path, records: list[dict[str, Any]]) -> int:
        """整体覆盖写 JSONL。单元格明细按文档+版本唯一，覆盖写保证重试不翻倍。"""
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return len(records)

    def _write_tables_idempotent(
        self, filepath: Path, records: list[dict[str, Any]], dedup_key: str
    ) -> int:
        """幂等写表格产物：按业务键去重替换已有记录后整体回写，重复抽取不会翻倍。"""
        existing = self._read_jsonl(filepath)
        merged: dict[Any, dict[str, Any]] = {
            rec.get(dedup_key): rec for rec in existing if rec.get(dedup_key) is not None
        }
        for rec in records:
            merged[rec.get(dedup_key)] = rec
        ordered = list(merged.values())
        return self._overwrite_jsonl(filepath, ordered)

    # -- 单文档抽取（保留既有链路，写入改为幂等） --

    def extract_from_layout(self, layout_id: str) -> dict[str, Any]:
        """从版式还原产物中抽取表格区域结构化数据，写入JSONL持久化文件。"""
        # 读取版式还原产物
        layout_file = self._data_dir("layout") / f"{layout_id}_layout.jsonl"
# 数据操作：归并表格区域数据抽取结构化行列数据
        layout_records = self._read_jsonl(layout_file)
        if not layout_records:
            return {"error": f"版式还原产物 {layout_id} 不存在或为空"}

        # 同时读取原始表格区域定义
        table_file = self._data_dir("layout") / f"{layout_id}_tables.jsonl"
        raw_tables = self._read_jsonl(table_file)

        extracted = self._build_extracted_tables(layout_id, raw_tables, layout_records)

        output_path = self._data_dir("tables") / f"{layout_id}_tables.jsonl"
        # 幂等写入：同一 table_key 不重复追加，重试坏文档不会把好文档再写一遍
        written = self._write_tables_idempotent(output_path, extracted, "table_key")
# 数据产物：表格结构化数据集写入tables目录持久化文件

        # 写入检查点
        self._write_checkpoint(f"table_{layout_id}", {
            "layout_id": layout_id,
            "tables_extracted": len(extracted),
            "total_cells": sum(e["rows"] * e["cols"] for e in extracted),
            "columns_per_table": [e["cols"] for e in extracted],
        })

        return {
            "layout_id": layout_id,
            "tables_extracted": len(extracted),
            "records_written": written,
            "output": str(output_path),
            "total_cells": sum(e["rows"] * e["cols"] for e in extracted),
        }

    def extract_from_batch(self, batch_key: str) -> dict[str, Any]:
        """从接入批次中批量抽取所有文档的表格，适用于大规模批量处理场景。"""
        batch_rows = [r for r in store.rows("file_intake") if r.get("批次编号") == batch_key]
        results = {"batch_key": batch_key, "documents_processed": 0, "tables_total": 0, "errors": []}
        for br in batch_rows:
            file_list = br.get("文件清单", "").split(",")
            for fname in file_list:
                fname = fname.strip()
                if not fname:
                    continue
                try:
                    res = self.extract_from_layout(fname)
                    if "error" not in res:
                        results["documents_processed"] += 1
                        results["tables_total"] += res.get("tables_extracted", 0)
                    else:
                        results["errors"].append(res)
                except Exception as e:
                    results["errors"].append({"doc_id": fname, "error": str(e)})
        return results

    # -- 批次表格抽取凭据 --

    def _resolve_batch_docs(self, batch_key: str, doc_ids: list[str] | None) -> list[str]:
        """显式文档清单优先；否则从 file_intake 批次登记的文件清单解析。"""
        if doc_ids:
            return [d.strip() for d in doc_ids if d and d.strip()]
        batch_rows = [r for r in store.rows("file_intake") if r.get("批次编号") == batch_key]
        docs: list[str] = []
        for br in batch_rows:
            for fname in br.get("文件清单", "").split(","):
                fname = fname.strip()
                if fname:
                    docs.append(fname)
        return docs

    def _inspect_doc_layout(self, doc_id: str) -> dict[str, Any]:
        """读取文档当前版式版本、校验值与区域，缺版式返回缺失标记。"""
        layout_svc = LayoutRestoreService()
        version, records = layout_svc.current_layout_version(doc_id)
        if not records:
            return {
                "doc_id": doc_id,
                "layout_version": "",
                "layout_checksum": "",
                "regions": [],
                "validation": VALIDATION_MISSING,
            }
        checksum = self._compute_checksum(
            "|".join(rec.get("layout_checksum", "") for rec in records)
        )
        regions = [
            {
                "page": rec.get("page"),
                "bbox": bbox,
                "region_status": rec.get("region_status", "initial"),
            }
            for rec in records
            for bbox in rec.get("table_bboxes", [])
        ]
        return {
            "doc_id": doc_id,
            "layout_version": version,
            "layout_checksum": checksum,
            "regions": regions,
            "validation": VALIDATION_OK,
        }

    def _load_credential(self, batch_key: str) -> dict[str, Any] | None:
        path = self._credential_path(batch_key)
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_credential(self, credential: dict[str, Any]) -> None:
        credential["updated_at"] = time.time()
        self._refresh_summary(credential)
        path = self._credential_path(credential["batch_key"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(credential, f, ensure_ascii=False, indent=2, default=str)

    def _append_receipt(self, batch_key: str, receipt: dict[str, Any]) -> dict[str, Any]:
        receipt.setdefault("receipt_id", f"RCP-{uuid.uuid4().hex[:10].upper()}")
        receipt.setdefault("at", time.time())
        receipt["batch_key"] = batch_key
        self._write_jsonl(self._receipt_path(batch_key), [receipt])
        return receipt

    def _refresh_summary(self, credential: dict[str, Any]) -> None:
        docs = credential["docs"]
        credential["summary"] = {
            "total": len(docs),
            "valid": sum(1 for d in docs.values() if d["validation"] == VALIDATION_OK),
            "layout_missing": sum(1 for d in docs.values() if d["validation"] == VALIDATION_MISSING),
            "stale_layout": sum(1 for d in docs.values() if d["validation"] == VALIDATION_STALE),
            "submitted": sum(1 for d in docs.values() if d["submit_status"] == SUBMIT_DONE),
            "blocked": sum(1 for d in docs.values() if d["submit_status"] == SUBMIT_BLOCKED),
        }
        versions = [
            d["layout_version"] for d in docs.values()
            if d.get("layout_version")
        ]
        credential["layout_set_version"] = (
            max(versions, key=version_rank) if versions else ""
        )

    def create_batch_credential(
        self, batch_key: str, doc_ids: list[str] | None = None
    ) -> dict[str, Any]:
        """建立批次表格抽取凭据：一组版式数据登记版本/区域并逐文档校验。"""
        if self._credential_path(batch_key).exists():
            return {"error": f"批次 {batch_key} 的表格抽取凭据已存在，请直接校验或提交"}

        docs = self._resolve_batch_docs(batch_key, doc_ids)
        if not docs:
            return {"error": f"批次 {batch_key} 未解析出文档清单，无法建立凭据"}

        credential: dict[str, Any] = {
            "credential_id": f"BTC-{uuid.uuid4().hex[:12].upper()}",
            "batch_key": batch_key,
            "created_at": time.time(),
            "layout_set_version": "",
            "docs": {},
            "summary": {},
        }
        for doc_id in docs:
            inspection = self._inspect_doc_layout(doc_id)
            credential["docs"][doc_id] = {
                **inspection,
                "submit_status": SUBMIT_BLOCKED if inspection["validation"] == VALIDATION_MISSING
                else SUBMIT_PENDING,
                "submitted_version": None,
                "tables": 0,
                "cells": 0,
                "failure_pages": [],
                "last_receipt_id": None,
                "updated_at": time.time(),
            }
            self._append_receipt(batch_key, {
                "doc_id": doc_id,
                "action": "credential_created",
                "result": "created",
                "layout_version": inspection["layout_version"],
                "layout_checksum": inspection["layout_checksum"],
                "tables": 0,
                "cells": 0,
                "reason": "缺版式" if inspection["validation"] == VALIDATION_MISSING else "版式已登记",
            })

        self._save_credential(credential)
        return self.get_group_confirmation(batch_key)

    def validate_batch_layouts(self, batch_key: str) -> dict[str, Any]:
        """重新逐文档校验版式版本与区域，区分可提交、缺版式与旧版式文档。"""
        credential = self._load_credential(batch_key)
        if credential is None:
            return {"error": f"批次 {batch_key} 的表格抽取凭据不存在"}

        # 先采集各文档当前版本，确定整组目标版本
        inspections = {
            doc_id: self._inspect_doc_layout(doc_id)
            for doc_id in credential["docs"]
        }
        group_versions = [
            ins["layout_version"] for ins in inspections.values() if ins["layout_version"]
        ]
        expected = max(group_versions, key=version_rank) if group_versions else ""

        for doc_id, doc_state in credential["docs"].items():
            inspection = inspections[doc_id]
            previous_validation = doc_state["validation"]
            already_submitted = doc_state["submit_status"] == SUBMIT_DONE
            # layout_version 记录磁盘上的当前版式；已提交版本单独保留，便于回查成果依据
            doc_state["layout_version"] = inspection["layout_version"]
            doc_state["layout_checksum"] = inspection["layout_checksum"]
            doc_state["regions"] = inspection["regions"]

            if not inspection["layout_version"]:
                doc_state["validation"] = VALIDATION_MISSING
            elif expected and version_rank(inspection["layout_version"]) < version_rank(expected):
                # 同批已出现更新版式，当前文档仍停留在旧版，标记旧版式
                doc_state["validation"] = VALIDATION_STALE
            else:
                doc_state["validation"] = VALIDATION_OK

            # 已确认成果始终可核验：已提交文档保留 submitted，不回退为阻断；
            # 其是否落后整组版本由 validation（stale_layout）单独表达。
            if already_submitted:
                doc_state["submit_status"] = SUBMIT_DONE
                if doc_state["validation"] == VALIDATION_STALE:
                    doc_state["newer_version_available"] = expected
            elif doc_state["submit_status"] == SUBMIT_BLOCKED and doc_state["validation"] == VALIDATION_OK:
                # 阻断文档在版式补齐后回到待提交
                doc_state["submit_status"] = SUBMIT_PENDING
            elif doc_state["validation"] in (VALIDATION_MISSING, VALIDATION_STALE):
                doc_state["submit_status"] = SUBMIT_BLOCKED
            doc_state["updated_at"] = time.time()

            if doc_state["validation"] != previous_validation:
                receipt = self._append_receipt(batch_key, {
                    "doc_id": doc_id,
                    "action": "layout_validated",
                    "result": doc_state["validation"],
                    "layout_version": inspection["layout_version"],
                    "layout_checksum": inspection["layout_checksum"],
                    "tables": 0,
                    "cells": 0,
                    "reason": f"整组目标版本 {expected or '无'}",
                })
                doc_state["last_receipt_id"] = receipt["receipt_id"]

        self._save_credential(credential)
        return self.get_group_confirmation(batch_key)

    def submit_doc_tables(
        self,
        batch_key: str,
        doc_id: str,
        layout_version: str | None = None,
    ) -> dict[str, Any]:
        """按凭据逐文档提交表格：版本门禁 + 幂等单元格写入 + 回执登记。"""
        credential = self._load_credential(batch_key)
        if credential is None:
            return {"error": f"批次 {batch_key} 的表格抽取凭据不存在"}
        doc_state = credential["docs"].get(doc_id)
        if doc_state is None:
            return {"error": f"文档 {doc_id} 不在批次 {batch_key} 凭据内"}

        expected_version = credential.get("layout_set_version", "")
        inspection = self._inspect_doc_layout(doc_id)
        current_version = inspection["layout_version"]

        # 门禁1：缺版式直接阻断，不产生任何表格文件
        if not current_version:
            doc_state["submit_status"] = SUBMIT_BLOCKED
            doc_state["validation"] = VALIDATION_MISSING
            receipt = self._append_receipt(batch_key, {
                "doc_id": doc_id, "action": "submit_tables",
                "result": "blocked_layout_missing",
                "layout_version": "", "tables": 0, "cells": 0,
                "reason": "版式还原产物缺失，表格抽取无法续做",
            })
            doc_state["last_receipt_id"] = receipt["receipt_id"]
            self._save_credential(credential)
            return {
                "ok": False, "batch_key": batch_key, "doc_id": doc_id,
                "result": "blocked_layout_missing",
                "message": "文档版式缺失，已阻断且未写出任何表格",
                "receipt_id": receipt["receipt_id"],
            }

        # 门禁2：幂等。已确认且磁盘版式未变的文档直接跳过，保证好文档重试不翻倍，
        # 且不被后续旧版式门禁误伤（已确认成果长期可核验）。
        if (doc_state["submit_status"] == SUBMIT_DONE
                and doc_state.get("submitted_version") == current_version):
            receipt = self._append_receipt(batch_key, {
                "doc_id": doc_id, "action": "retry_submit",
                "result": "duplicate_skipped",
                "layout_version": current_version,
                "tables": doc_state.get("tables", 0),
                "cells": doc_state.get("cells", 0),
                "reason": "该文档表格已确认，重试幂等跳过，未重复写出",
            })
            doc_state["last_receipt_id"] = receipt["receipt_id"]
            self._save_credential(credential)
            return {
                "ok": True, "batch_key": batch_key, "doc_id": doc_id,
                "result": "duplicate_skipped",
                "message": "表格已存在且版本一致，已跳过重复写入",
                "tables": doc_state.get("tables", 0),
                "cells": doc_state.get("cells", 0),
                "receipt_id": receipt["receipt_id"],
            }

        # 显式指定版本必须与磁盘当前版本一致，避免按不存在的版本写入
        if layout_version and layout_version != current_version:
            return {
                "ok": False, "batch_key": batch_key, "doc_id": doc_id,
                "result": "version_mismatch",
                "message": f"指定版式 {layout_version} 与当前 {current_version} 不一致",
            }

        # 门禁3：旧版式拒绝，不允许把旧区域提交为新结果（仅作用于尚未确认的文档）
        effective_version = layout_version or current_version
        if version_rank(effective_version) < version_rank(expected_version):
            doc_state["validation"] = VALIDATION_STALE
            doc_state["submit_status"] = SUBMIT_BLOCKED
            receipt = self._append_receipt(batch_key, {
                "doc_id": doc_id, "action": "submit_tables",
                "result": "rejected_stale_layout",
                "layout_version": effective_version,
                "tables": 0, "cells": 0,
                "reason": f"当前版式 {effective_version} 落后整组版本 {expected_version}",
            })
            doc_state["last_receipt_id"] = receipt["receipt_id"]
            self._save_credential(credential)
            return {
                "ok": False, "batch_key": batch_key, "doc_id": doc_id,
                "result": "rejected_stale_layout",
                "message": f"旧版式 {effective_version} 不能提交为新结果，需先重跑至 {expected_version}",
                "receipt_id": receipt["receipt_id"],
            }

        # 通过门禁：按补齐版本执行抽取并覆盖写单元格明细
        extract = self._extract_doc_for_version(doc_id, current_version)
        if "error" in extract:
            doc_state["submit_status"] = SUBMIT_BLOCKED
            receipt = self._append_receipt(batch_key, {
                "doc_id": doc_id, "action": "submit_tables",
                "result": "blocked_extract_failed",
                "layout_version": current_version, "tables": 0, "cells": 0,
                "reason": extract["error"],
            })
            doc_state["last_receipt_id"] = receipt["receipt_id"]
            self._save_credential(credential)
            return {"ok": False, "batch_key": batch_key, "doc_id": doc_id,
                    "result": "blocked_extract_failed", "message": extract["error"],
                    "receipt_id": receipt["receipt_id"]}

        doc_state["validation"] = VALIDATION_OK
        doc_state["submit_status"] = SUBMIT_DONE
        doc_state["submitted_version"] = current_version
        doc_state["tables"] = extract["tables_extracted"]
        doc_state["cells"] = extract["total_cells"]
        doc_state["updated_at"] = time.time()
        receipt = self._append_receipt(batch_key, {
            "doc_id": doc_id, "action": "submit_tables",
            "result": "submitted",
            "layout_version": current_version,
            "layout_checksum": inspection["layout_checksum"],
            "tables": extract["tables_extracted"],
            "cells": extract["total_cells"],
            "reason": f"单元格明细按 {current_version} 写入",
        })
        doc_state["last_receipt_id"] = receipt["receipt_id"]
        self._save_credential(credential)
        return {
            "ok": True, "batch_key": batch_key, "doc_id": doc_id,
            "result": "submitted",
            "layout_version": current_version,
            "tables": extract["tables_extracted"],
            "cells": extract["total_cells"],
            "output": extract["output"],
            "receipt_id": receipt["receipt_id"],
        }

    def retry_doc(self, batch_key: str, doc_id: str) -> dict[str, Any]:
        """重新提交未成功文档；已确认好文档走幂等跳过，不会重复写出。"""
        receipt_bookmark = self._append_receipt(batch_key, {
            "doc_id": doc_id, "action": "retry_submit", "result": "retried",
            "layout_version": "", "tables": 0, "cells": 0,
            "reason": "复核员触发坏文档重试",
        })
        result = self.submit_doc_tables(batch_key, doc_id)
        result["retry_receipt_id"] = receipt_bookmark["receipt_id"]
        return result

    def rerun_layout_regions(
        self, batch_key: str, doc_id: str, failed_pages: list[int]
    ) -> dict[str, Any]:
        """凭据关联失败区域，驱动版式还原局部重跑并刷新凭据版本。"""
        credential = self._load_credential(batch_key)
        if credential is None:
            return {"error": f"批次 {batch_key} 的表格抽取凭据不存在"}
        if doc_id not in credential["docs"]:
            return {"error": f"文档 {doc_id} 不在批次 {batch_key} 凭据内"}

        layout_svc = LayoutRestoreService()
        rerun = layout_svc.rerun_failed_regions(
            doc_id, failed_pages, reason=f"批次 {batch_key} 凭据关联失败区域"
        )
        if "error" in rerun:
            return rerun

        inspection = self._inspect_doc_layout(doc_id)
        doc_state = credential["docs"][doc_id]
        doc_state["failure_pages"] = sorted({int(p) for p in failed_pages})
        doc_state["layout_version"] = inspection["layout_version"]
        doc_state["layout_checksum"] = inspection["layout_checksum"]
        doc_state["regions"] = inspection["regions"]
        doc_state["validation"] = VALIDATION_OK
        # 新版式下需要重新抽取；旧的已提交结果保留为历史，不删除
        doc_state["submit_status"] = SUBMIT_PENDING
        doc_state["updated_at"] = time.time()

        receipt = self._append_receipt(batch_key, {
            "doc_id": doc_id, "action": "layout_rerun",
            "result": "layout_rerun",
            "layout_version": rerun["layout_version"],
            "tables": 0, "cells": 0,
            "reason": f"重跑页 {rerun['pages_rerun']}，修正区域 {len(rerun['corrected_regions'])} 个",
            "corrected_regions": rerun["corrected_regions"],
        })
        doc_state["last_receipt_id"] = receipt["receipt_id"]
        self._save_credential(credential)
        return {
            "batch_key": batch_key, "doc_id": doc_id,
            "result": "layout_rerun",
            "previous_version": rerun["previous_version"],
            "layout_version": rerun["layout_version"],
            "pages_rerun": rerun["pages_rerun"],
            "corrected_regions": rerun["corrected_regions"],
            "receipt_id": receipt["receipt_id"],
        }

    # -- 查询与回查 --

    def get_credential(self, batch_key: str) -> dict[str, Any]:
        credential = self._load_credential(batch_key)
        if credential is None:
            return {"error": f"批次 {batch_key} 的表格抽取凭据不存在"}
        return credential

    def get_group_confirmation(self, batch_key: str) -> dict[str, Any]:
        """整组确认视图：好文档可否加入整组确认、坏文档能否单独处理。"""
        credential = self._load_credential(batch_key)
        if credential is None:
            return {"error": f"批次 {batch_key} 的表格抽取凭据不存在"}

        confirmable, blocked, individually_processable = [], [], []
        for doc_id, doc_state in credential["docs"].items():
            item = {
                "doc_id": doc_id,
                "validation": doc_state["validation"],
                "submit_status": doc_state["submit_status"],
                "layout_version": doc_state["layout_version"],
                "submitted_version": doc_state.get("submitted_version"),
                "tables": doc_state.get("tables", 0),
                "cells": doc_state.get("cells", 0),
                # 已提交但磁盘版式已更新时，提示复核员可选择按新版本补抽（非强制）
                "newer_version_available": doc_state.get("newer_version_available", ""),
            }
            if doc_state["submit_status"] == SUBMIT_DONE:
                # 已确认成果仍可核验，可加入整组确认；即便磁盘出现更新版本也不回退
                confirmable.append(item)
            elif doc_state["validation"] == VALIDATION_OK:
                # 版式齐全但尚未提交，可单独处理
                individually_processable.append({**item, "next_action": "submit_doc_tables"})
            else:
                next_action = (
                    "rerun_layout_regions"
                    if doc_state["validation"] == VALIDATION_STALE
                    else "restore_layout_first"
                )
                blocked.append({
                    **item,
                    "next_action": next_action,
                    "reason": doc_state["validation"],
                    "failure_pages": doc_state.get("failure_pages", []),
                })

        return {
            "credential_id": credential["credential_id"],
            "batch_key": batch_key,
            "layout_set_version": credential.get("layout_set_version", ""),
            "summary": credential["summary"],
            "group_ready": len(blocked) == 0
            and all(d["submit_status"] == SUBMIT_DONE for d in credential["docs"].values()),
            "confirmable_docs": confirmable,
            "individually_processable_docs": individually_processable,
            "blocked_docs": blocked,
        }

    def list_receipts(self, batch_key: str, doc_id: str | None = None) -> dict[str, Any]:
        """逐条查看回执，可按文档过滤；含每次提交、拒绝、重跑记录。"""
        receipts = self._read_jsonl(self._receipt_path(batch_key))
        if doc_id:
            receipts = [r for r in receipts if r.get("doc_id") == doc_id]
        return {
            "batch_key": batch_key,
            "total": len(receipts),
            "receipts": receipts,
        }

    def trace_table(self, doc_id: str, table_key: str | None = None) -> dict[str, Any]:
        """回查表格依据的版式版本、页码区域与单元格明细。"""
        layout_svc = LayoutRestoreService()
        version, _ = layout_svc.current_layout_version(doc_id)
        region_trace = layout_svc.layout_region_trace(doc_id)
        if not region_trace:
            return {"error": f"文档 {doc_id} 无版式产物，无法回查表格血缘"}

        # 汇总该文档各版本单元格明细文件
        cell_files = sorted(self._data_dir("tables").glob(f"{doc_id}_cells_*.jsonl"))
        versions = []
        for cf in cell_files:
            records = self._read_jsonl(cf)
            if table_key:
                records = [r for r in records if r.get("table_key") == table_key]
            if not records:
                continue
            versions.append({
                "layout_version": records[0].get("layout_version"),
                "file": str(cf),
                "cell_count": len(records),
                "tables": sorted({r.get("table_key") for r in records if r.get("table_key")}),
                "sample_cells": records[:5],
            })

        return {
            "doc_id": doc_id,
            "current_layout_version": version,
            "region_trace": region_trace,
            "cell_versions": versions,
        }

    # -- 版本化抽取内部方法 --

    def _load_versioned_tables(self, doc_id: str, version: str) -> list[dict[str, Any]]:
        """优先读取版本化区域定义；缺失时回退基础 tables 文件。"""
        versioned = self._read_jsonl(
            self._data_dir("layout") / f"{doc_id}_tables_{version}.jsonl"
        )
        if versioned:
            return versioned
        return self._read_jsonl(self._data_dir("layout") / f"{doc_id}_tables.jsonl")

    def _build_extracted_tables(
        self,
        doc_id: str,
        raw_tables: list[dict[str, Any]],
        layout_records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """基于区域定义生成结构化表格与逐单元格明细载荷。"""
        layout_by_page: dict[int, dict[str, Any]] = {
            rec.get("page"): rec for rec in layout_records
        }
        extracted = []
        for table_def in raw_tables:
            page = table_def.get("page", 0)
            layout_rec = layout_by_page.get(page, {})
            rows = table_def.get("rows", 4)
            cols = table_def.get("cols", 5)
            structured = {"header": [], "data_rows": []}
            for h in range(cols):
                structured["header"].append(f"列{h+1}")
            for r in range(1, rows):
                row_data = {}
                for c in range(cols):
                    row_data[f"列{c+1}"] = f"值{r}_{c+1}"
                structured["data_rows"].append(row_data)

            extracted.append({
                "table_key": f"TABLE-{doc_id}-{page}",
                "doc_id": table_def.get("doc_id", doc_id),
                "page": page,
                "bbox": table_def.get("bbox", ""),
                "rows": rows,
                "cols": cols,
                "header_rows": table_def.get("header_row", 1),
                "merged_cells": table_def.get("merged_cells", 0),
                "structured_output": structured,
                "layout_version": layout_rec.get("layout_version", table_def.get("layout_version", "v1")),
                "layout_checksum": layout_rec.get("layout_checksum", ""),
                "region_status": layout_rec.get("region_status", table_def.get("region_status", "initial")),
                "extracted_at": time.time(),
            })
        return extracted

    def _build_cell_details(
        self, doc_id: str, extracted: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """把结构化表格展开为逐单元格明细，携带版式版本与区域坐标。"""
        cells = []
        for table in extracted:
            structured = table["structured_output"]
            headers = structured.get("header", [])
            # 表头单元格
            for c, header in enumerate(headers):
                cells.append({
                    "cell_id": f"{doc_id}-p{table['page']}-r0-c{c+1}",
                    "table_key": table["table_key"],
                    "doc_id": doc_id,
                    "page": table["page"],
                    "bbox": table["bbox"],
                    "row": 0,
                    "col": c + 1,
                    "value": header,
                    "cell_type": "header",
                    "layout_version": table["layout_version"],
                    "layout_checksum": table["layout_checksum"],
                    "region_status": table["region_status"],
                })
            # 数据单元格
            for r, row_data in enumerate(structured.get("data_rows", []), start=1):
                for c, header in enumerate(headers):
                    cells.append({
                        "cell_id": f"{doc_id}-p{table['page']}-r{r}-c{c+1}",
                        "table_key": table["table_key"],
                        "doc_id": doc_id,
                        "page": table["page"],
                        "bbox": table["bbox"],
                        "row": r,
                        "col": c + 1,
                        "value": row_data.get(header, ""),
                        "cell_type": "data",
                        "layout_version": table["layout_version"],
                        "layout_checksum": table["layout_checksum"],
                        "region_status": table["region_status"],
                    })
        return cells

    def _extract_doc_for_version(self, doc_id: str, version: str) -> dict[str, Any]:
        """按指定版式版本抽取文档表格，单元格明细覆盖写到文档+版本文件。"""
        layout_file = self._data_dir("layout") / f"{doc_id}_layout.jsonl"
        layout_records = [
            rec for rec in self._read_jsonl(layout_file)
            if rec.get("layout_version", "v1") == version
        ]
        if not layout_records:
            return {"error": f"文档 {doc_id} 不存在版式版本 {version} 的产物"}

        raw_tables = self._load_versioned_tables(doc_id, version)
        if not raw_tables:
            return {"error": f"文档 {doc_id} 版式 {version} 无表格区域定义"}

        extracted = self._build_extracted_tables(doc_id, raw_tables, layout_records)
        cells = self._build_cell_details(doc_id, extracted)

        cells_path = self._cells_path(doc_id, version)
        self._overwrite_jsonl(cells_path, cells)

        # 同步维护文档级结构化表格产物（幂等去重）
        tables_path = self._data_dir("tables") / f"{doc_id}_tables.jsonl"
        self._write_tables_idempotent(tables_path, extracted, "table_key")

        self._write_checkpoint(f"table_{doc_id}_{version}", {
            "doc_id": doc_id,
            "layout_version": version,
            "tables_extracted": len(extracted),
            "total_cells": len(cells),
            "cells_output": str(cells_path),
        })
        return {
            "doc_id": doc_id,
            "layout_version": version,
            "tables_extracted": len(extracted),
            "total_cells": len(cells),
            "output": str(cells_path),
        }
