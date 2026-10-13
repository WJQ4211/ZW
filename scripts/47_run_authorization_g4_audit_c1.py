from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_RUN_ID = "FORMAL-G3-T1-G4-T1-FINAL-PAPER-CASE-20260827T112444Z-v1"
ORIGINAL_RUN = ROOT / "outputs" / ORIGINAL_RUN_ID
OUT = ROOT / "outputs" / "AUTHORIZATION-G4-AUDIT-C1"
AUTH_TEXT = (
    "我已审阅P16R-C1三项分歧、G4QR-C1报告草案v3、论文案例章节v4及修订图表，同意将`DISAG-T1R-C1-01`作为高影响机制分歧条件保留，将`DISAG-T1R-C1-02`作为高影响约束权重分歧持续监测，将`DISAG-T1R-C1-03`通过“局部事实成立—整体外推受限”的范围拆分予以解决；同意3项判别性需求、4项监测指标和8条触发规则。同意上述T1条件化综合判断，授权按“AI预审与复评＋作者整体签核、关键分歧逐项裁决”的方式正式执行G3-T1并签发G4-T1。签发时建立T0—T1分歧谱系，但不得重跑或修改Evidence、快照、假设和观点版本；同时生成最终论文案例包。"
)
CORRECTION = "G3-T1作者审核字段保持空白。"
CORRECTED = "G3-T1已按‘AI预审与复评＋作者整体签核、关键分歧逐项裁决’完成正式审核，相关审核记录已写入；本次正式签发未重跑或修改Evidence、快照、假设、观点版本及既有依赖关系。"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run() -> Path:
    if OUT.exists():
        raise RuntimeError(f"append-only correction directory already exists: {OUT}")
    if not ORIGINAL_RUN.exists():
        raise RuntimeError(f"original formal run is missing: {ORIGINAL_RUN}")
    source_manifest_path = ORIGINAL_RUN / "final_case_manifest.json"
    source_auth_path = ORIGINAL_RUN / "authorization.txt"
    source_report_path = ORIGINAL_RUN / "FINAL_PAPER_CASE" / "g4_t1_final_report.md"
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    original_auth_file_hash = sha256_file(source_auth_path)
    original_recorded_hash = source_manifest["authorization_sha256"]
    recovered_hash = sha256_bytes(AUTH_TEXT.encode("utf-8"))
    expected_hash = "e43aa1e1221275a902e19f390f4dd64d0c3b5f362911579e7da658f83801d7cf"
    if recovered_hash != expected_hash:
        raise RuntimeError(f"recovered authorization hash differs from expected value: {recovered_hash}")
    if original_recorded_hash != "0168641acc42d8df525b4e1e3082b460464425603520bc0ebc70c7c3f618859a":
        raise RuntimeError("original manifest authorization hash changed unexpectedly")
    report = source_report_path.read_text(encoding="utf-8")
    if report.count(CORRECTION) != 1:
        raise RuntimeError(f"expected one stale audit sentence in report, found {report.count(CORRECTION)}")
    corrected_report = report.replace(CORRECTION, CORRECTED)

    OUT.mkdir(parents=True)
    # authorization_original.txt is deliberately written as exact UTF-8
    # bytes: no BOM and no terminal newline.
    (OUT / "authorization_original.txt").write_bytes(AUTH_TEXT.encode("utf-8"))
    (OUT / "g4_t1_final_report_c1.md").write_text(corrected_report, encoding="utf-8")
    provenance = {
        "original_formal_run_id": ORIGINAL_RUN_ID,
        "original_manifest_authorization_hash": original_recorded_hash,
        "original_authorization_file_hash": original_auth_file_hash,
        "recovered_authorization_text_hash": recovered_hash,
        "encoding_rule": "UTF-8",
        "bom": False,
        "trailing_newline": False,
        "hash_algorithm": "SHA-256",
        "hash_input": "exact UTF-8 bytes of authorization_original.txt",
        "old_hash_status": "legacy_recorded_authorization_hash_unverifiable",
        "new_hash_status": "canonical_authorization_text_hash",
        "correction_scope": "provenance_documentation_only",
        "substantive_decisions_changed": False,
        "formal_g3_g4_status_changed": False,
        "database_writes": 0,
        "historical_artifacts_modified": False,
        "report_correction_scope": "single mandated audit sentence only",
    }
    write_json(OUT / "authorization_provenance_correction.json", provenance)
    write_json(OUT / "authorization_hash_crosswalk.json", {
        "old": {
            "recorded_hash": original_recorded_hash,
            "actual_authorization_file_hash": original_auth_file_hash,
            "status": "legacy_recorded_authorization_hash_unverifiable",
        },
        "new": {
            "canonical_text_hash": recovered_hash,
            "file": "authorization_original.txt",
            "status": "canonical_authorization_text_hash",
        },
        "relationship": "new canonical source documentation; no database or historical artifact write-back",
        "correction_scope": "provenance_documentation_only",
    })
    (OUT / "correction_report.md").write_text(
        "# AUTHORIZATION-G4-AUDIT-C1\n\n"
        "本次仅完成授权来源和G4报告审计文字的追加式文档纠正。原正式运行目录保持不变；数据库、Evidence、快照、假设、观点、依赖、分歧和审核记录均未写入或修改。\n\n"
        f"原记录授权哈希：`{original_recorded_hash}`；原授权文件实际哈希：`{original_auth_file_hash}`。\n"
        f"恢复后的规范授权原文哈希：`{recovered_hash}`。\n\n"
        "G4报告只将“G3-T1作者审核字段保持空白”替换为已完成正式审核并已写入审核记录的说明，其他判断、数据和结论保持不变。\n",
        encoding="utf-8",
    )

    file_hashes = {
        str(path.relative_to(OUT)).replace("\\", "/"): sha256_file(path)
        for path in sorted(OUT.rglob("*"))
        if path.is_file()
    }
    manifest = {
        "run_id": "AUTHORIZATION-G4-AUDIT-C1",
        "stage": "pure_document_audit_correction",
        "original_formal_run_id": ORIGINAL_RUN_ID,
        "original_manifest": "final_case_manifest.json",
        "model_api_calls": 0,
        "database_writes": 0,
        "historical_files_modified": 0,
        "formal_g3_status_unchanged": True,
        "formal_g4_status_unchanged": True,
        "t1_snapshot_item_count": 125,
        "evidence_inserted": 0,
        "hypothesis_inserted": 0,
        "viewpoint_inserted": 0,
        "dependency_inserted": 0,
        "disagreement_inserted": 0,
        "human_review_inserted": 0,
        "snapshot_refrozen": False,
        "api_key_written": False,
        "historical_artifacts_modified": False,
        "source_report_hash": sha256_file(source_report_path),
        "corrected_report_hash": sha256_file(OUT / "g4_t1_final_report_c1.md"),
        "authorization_original_sha256": recovered_hash,
        "file_hashes_excluding_manifest": file_hashes,
        "manifest_hash_scope": "all correction files except this manifest to avoid self-hash recursion",
    }
    write_json(OUT / "correction_manifest.json", manifest)
    return OUT


if __name__ == "__main__":
    print(run())
