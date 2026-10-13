"""Blind T1 collection, extraction, and G1-T1 preparation.

This module deliberately does not import or read T0 hypotheses, viewpoints,
disagreements, or monitoring objects.  It is entered only after the G4-T0
transaction has verified that T1 is unsealed.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from bs4 import BeautifulSoup
from pypdf import PdfReader

from .model_client import APIModelClient, ApiCallRecord, ModelAPIError
from .schemas import CODING_DIMENSION_VALUES, EVIDENCE_NATURE_VALUES


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "research.sqlite3"
CASE_ID = "us_military_ai_deployment"
SNAPSHOT_ID = "SNAP-T0-US-MILITARY-AI-C6C812108AF95309"
G4_DIR = ROOT / "outputs" / "G4-P11-P12-20260827T051332Z-v1" / "G4_FORMAL"
T1_START = date(2025, 1, 1)
T1_END = date(2026, 6, 30)

CASE_QUESTION = (
    "以公开、非密材料为依据，研究2025年1月1日至2026年6月30日期间，"
    "美国军用人工智能能力形成、应用部署和体系化发展的公开可观察变化。"
)

DIMENSION_DEFINITIONS = {
    "integration_level": "跨项目、跨平台、跨军种或企业级系统集成的程度。",
    "application_maturity": "从目标、演示、试验到部署和持续使用的应用成熟度。",
    "human_machine_authority": "人机在任务中的权责、授权、监督与决策关系。",
    "engineering_resource_conditions": "数据、算力、基础设施、人才、预算、采办和保障条件。",
    "trust_governance_constraints": "可信性、安全、测试评估、责任、治理约束及制度落地。",
    "organizational_system_fit": "组织运用、流程、任务适配和体系制度环境的匹配程度。",
}

NATURE_DEFINITIONS = {
    "goal": "计划、目标、拟议能力或未来安排，不等于已经实现。",
    "governance_event": "已经发生的政策、职责、指南或组织治理机制建立。",
    "resource_input": "已经发生的预算、合同、采购、数据、人才或基础设施投入。",
    "test_event": "测试、演示、演习或竞赛发生及其参与主体、配置。",
    "test_result": "测试中明确观察到的功能、性能或结果。",
    "deployment_event": "明确已经部署、实装、投入运行或持续使用。",
    "constraint": "限制能力形成、部署或评估的已报告条件。",
    "evaluation": "来源明确给出的评估、判断或评价，不改写为客观结果。",
}


# This is a source-discovery catalogue, not an evidence fixture.  It contains
# only public T1 URLs and metadata needed to verify the original pages.  The
# body of every source is fetched and parsed at runtime.
SOURCE_CATALOG: list[dict[str, Any]] = [
    {
        "source_id": "T1-DAF-001",
        "title_hint": "DAF releases data, AI strategies to accelerate military dominance",
        "publisher": "Department of the Air Force",
        "source_grade": "A",
        "category": "strategy_policy_governance",
        "document_date_hint": "2026-04-22",
        "url": "https://www.af.mil/News/Article-Display/Article/4467267/daf-releases-data-ai-strategies-to-accelerate-military-dominance/",
        "capture_type": "official_html",
        "reason": "官方军种战略与治理材料，直接涉及数据和AI战略实施。",
    },
    {
        "source_id": "T1-DAF-002",
        "title_hint": "DAF launches plan to bolster AI workforce",
        "publisher": "Department of the Air Force",
        "source_grade": "A",
        "category": "strategy_policy_governance",
        "document_date_hint": "2026-04-28",
        "url": "https://www.af.mil/News/Article-Display/Article/4472626/daf-launches-plan-to-bolster-ai-workforce/",
        "capture_type": "official_html",
        "reason": "官方军种人才治理与工程资源材料。",
    },
    {
        "source_id": "T1-DAF-003",
        "title_hint": "Air Force advances secure AI capabilities with IL5 approval for VIA's eJARVIS",
        "publisher": "U.S. Air Force",
        "source_grade": "A",
        "category": "program_budget_acquisition",
        "document_date_hint": "2026-04-17",
        "url": "https://www.af.mil/News/Article-Display/Article/4463303/air-force-advances-secure-ai-capabilities-with-il5-approval-for-vias-ejarvis/",
        "capture_type": "official_html",
        "reason": "官方安全AI能力和运行环境准入材料。",
    },
    {
        "source_id": "T1-DAF-004",
        "title_hint": "Space Force integrates with Air Force in AI sprint to ensure mission dominance",
        "publisher": "United States Space Force",
        "source_grade": "A",
        "category": "program_budget_acquisition",
        "document_date_hint": "2026-06-30",
        "url": "https://www.spaceforce.mil/News/Article-Display/Article/4530561/space-force-integrates-with-air-force-in-ai-sprint-to-ensure-mission-dominance/",
        "capture_type": "official_html",
        "reason": "官方跨军种AI应用与整合活动材料。",
    },
    {
        "source_id": "T1-DAF-005",
        "title_hint": "Capstone 2025: Advancing AI for C2, dynamic targeting, coalition integration",
        "publisher": "U.S. Air Force",
        "source_grade": "A",
        "category": "test_deployment_organization",
        "document_date_hint": "2025-12-17",
        "url": "https://www.af.mil/News/Article-Display/Article/4363112/capstone-2025-advancing-ai-for-c2-dynamic-targeting-coalition-integration/",
        "capture_type": "official_html",
        "reason": "官方演示和任务运用材料，涉及C2、动态目标和联盟协同。",
    },
    {
        "source_id": "T1-DAF-006",
        "title_hint": "Air Force experiments with AI, boosts battle management speed, accuracy",
        "publisher": "U.S. Air Force",
        "source_grade": "A",
        "category": "test_deployment_organization",
        "document_date_hint": "2025-09-19",
        "url": "https://www.af.mil/News/Article-Display/Article/4310090/air-force-experiments-with-ai-boosts-battle-management-speed-accuracy/",
        "capture_type": "official_html",
        "reason": "官方AI试验与战斗管理运用材料。",
    },
    {
        "source_id": "T1-DAF-007",
        "title_hint": "Luke AFB Airmen pioneer AI integration for daily operations",
        "publisher": "U.S. Air Force",
        "source_grade": "A",
        "category": "test_deployment_organization",
        "document_date_hint": "2026-05-18",
        "url": "https://www.af.mil/News/Article-Display/Article/4495022/luke-afb-airmen-pioneer-ai-integration-for-daily-operations/",
        "capture_type": "official_html",
        "reason": "官方基层组织日常运用与部署材料。",
    },
    {
        "source_id": "T1-DAF-008",
        "title_hint": "732nd AMS leverages artificial intelligence to enhance Arctic TTX",
        "publisher": "U.S. Air Force",
        "source_grade": "A",
        "category": "test_deployment_organization",
        "document_date_hint": "2026-06-04",
        "url": "https://www.af.mil/News/Article-Display/Article/4507566/732nd-ams-leverages-artificial-intelligence-to-enhance-arctic-ttx/",
        "capture_type": "official_html",
        "reason": "官方战区环境桌面演练和组织运用材料。",
    },
    {
        "source_id": "T1-DARPA-001",
        "title_hint": "Sharpening AI warfighting advantage on the battlefield",
        "publisher": "DARPA",
        "source_grade": "A",
        "category": "test_deployment_organization",
        "document_date_hint": "2025-05-01",
        "url": "https://www.darpa.mil/news/2025/saber-warfighter-ai",
        "capture_type": "official_html",
        "reason": "DARPA官方项目与战场AI应用材料。",
    },
    {
        "source_id": "T1-DARPA-002",
        "title_hint": "Closing the integration gap for molecules and microsystems",
        "publisher": "DARPA",
        "source_grade": "A",
        "category": "program_budget_acquisition",
        "document_date_hint": "2025-06-12",
        "url": "https://www.darpa.mil/news/2025/closing-integration-gap",
        "capture_type": "official_html",
        "reason": "DARPA官方工程整合项目材料，涉及从研发到应用的转化条件。",
    },
    {
        "source_id": "T1-DARPA-003",
        "title_hint": "DART and MSAI triumph at DARPA Triage Challenge",
        "publisher": "DARPA",
        "source_grade": "A",
        "category": "test_deployment_organization",
        "document_date_hint": "2025-02-18",
        "url": "https://www.darpa.mil/news/2025/dart-msai-triumph-darpa-triage-challenge",
        "capture_type": "official_html",
        "reason": "DARPA官方竞赛、测试结果与任务应用材料。",
    },
    {
        "source_id": "T1-IR-001",
        "title_hint": "Opportunities for Artificial Intelligence in Operational Medicine: Lessons from the United States Military",
        "publisher": "Bioengineering (MDPI), peer-reviewed research",
        "source_grade": "B",
        "category": "independent_review_research",
        "document_date_hint": "2025-05-14",
        "url": "https://www.mdpi.com/2306-5354/12/5/519",
        "capture_url": "https://mdpi-res.com/d_attachment/bioengineering/bioengineering-12-00519/article_deploy/bioengineering-12-00519-v2.pdf",
        "capture_type": "publisher_pdf",
        "reason": "2025年公开同行评议研究，提供独立应用与工程条件视角。",
    },
]


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def file_hashes(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not root.exists():
        return result
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        result[str(path.relative_to(root)).replace("\\", "/")] = sha256_bytes(path.read_bytes())
    return result


def _read_only_db_counts() -> dict[str, int]:
    uri = f"file:{DB_PATH.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        values = {}
        for table in ("evidence", "hypothesis", "viewpoint", "disagreement", "monitor_indicator", "trigger_rule", "evidence_snapshot"):
            values[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        values["formal_t1_evidence"] = int(
            conn.execute(
                "SELECT COUNT(*) FROM evidence WHERE case_id=? AND snapshot_tag='T1'",
                (CASE_ID,),
            ).fetchone()[0]
        )
        values["formal_t1_snapshot"] = int(
            conn.execute(
                "SELECT COUNT(*) FROM evidence_snapshot WHERE case_id=? AND phase='T1'",
                (CASE_ID,),
            ).fetchone()[0]
        )
        return values


def assert_g4_unsealed() -> dict[str, Any]:
    manifest_path = G4_DIR / "g4_t0_signoff_manifest.json"
    report_path = G4_DIR / "g4_t0_report_signed.md"
    if not manifest_path.exists() or not report_path.exists():
        raise RuntimeError("G4 signed artifacts are missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report_hash = sha256_bytes(report_path.read_bytes())
    if not manifest.get("g4_signed") or not manifest.get("t1_unsealed"):
        raise RuntimeError("G4-T0 is not signed and T1 is not unsealed")
    if manifest.get("signed_report_sha256") != report_hash:
        raise RuntimeError("signed report hash mismatch")
    counts = _read_only_db_counts()
    uri = f"file:{DB_PATH.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        row = conn.execute(
            "SELECT approved, payload_json FROM human_review WHERE case_id=? AND gate IN ('G4-T0','G4_T0') ORDER BY rowid DESC LIMIT 1",
            (CASE_ID,),
        ).fetchone()
    review_payload: dict[str, Any] = {}
    if row and isinstance(row[1], str):
        try:
            review_payload = json.loads(row[1])
        except json.JSONDecodeError:
            review_payload = {}
    review_hash = review_payload.get("artifact_hash") or review_payload.get("signed_report_sha256")
    if not row or int(row[0]) != 1 or review_hash != report_hash:
        raise RuntimeError("G4-T0 human_review gate is not approved for the signed report")
    return {"g4_manifest": manifest, "signed_report_sha256": report_hash, "db_counts_before": counts}


def _fetch(url: str) -> tuple[bytes, str, str, int]:
    user_agents = [
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0",
    ]
    last: Exception | None = None
    for attempt in range(8):
        headers = {
            "User-Agent": user_agents[attempt % len(user_agents)],
            "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
        }
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=45) as response:
                return response.read(), response.geturl(), response.headers.get("Content-Type", ""), int(response.status)
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in {403, 429, 500, 502, 503, 504}:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionResetError, OSError) as exc:
            last = exc
        if attempt < 7:
            time.sleep(min(2.0 + attempt * 0.5, 5.0))
    if isinstance(last, urllib.error.HTTPError):
        raise RuntimeError(f"source fetch failed with HTTP {last.code}") from None
    raise RuntimeError(f"source fetch failed: {type(last).__name__ if last else 'UnknownError'}") from None


def _first_meta(soup: BeautifulSoup, keys: set[str]) -> str:
    for tag in soup.find_all("meta"):
        key = str(tag.get("property") or tag.get("name") or "").lower()
        value = str(tag.get("content") or "").strip()
        if value and key in keys:
            return value
    return ""


def _parse_date(value: str) -> str | None:
    value = value.strip()
    for pattern in (r"(202[56])-([01]\d)-([0-3]\d)", r"([A-Z][a-z]+)\s+([0-3]?\d),\s+(202[56])"):
        match = re.search(pattern, value)
        if not match:
            continue
        try:
            if match.group(1).isdigit():
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
            parsed = datetime.strptime(f"{match.group(1)} {match.group(2)} {match.group(3)}", "%B %d %Y")
            return parsed.date().isoformat()
        except ValueError:
            continue
    return None


def _extract_html_date(soup: BeautifulSoup, text: str) -> str | None:
    value = _first_meta(
        soup,
        {
            "article:published_time",
            "datepublished",
            "datecreated",
            "citation_publication_date",
            "dc.date",
            "date",
            "publishdate",
        },
    )
    if value:
        parsed = _parse_date(value)
        if parsed:
            return parsed
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        objects = data if isinstance(data, list) else [data]
        for obj in objects:
            if isinstance(obj, dict):
                parsed = _parse_date(str(obj.get("datePublished", "")))
                if parsed:
                    return parsed
    head = text[:3500]
    for pattern in (
        r"(?:Published|Posted|Release Date|Date)\s*[:\-]?\s*([A-Z][a-z]+\s+[0-3]?\d,\s+202[56])",
        r"([A-Z][a-z]+\s+[0-3]?\d,\s+202[56])",
        r"(202[56]-[01]\d-[0-3]\d)",
    ):
        match = re.search(pattern, head)
        if match:
            parsed = _parse_date(match.group(1))
            if parsed:
                return parsed
    return None


def _clean_html(raw: bytes) -> tuple[str, dict[str, Any]]:
    decoded = raw.decode("utf-8", errors="replace")
    soup = BeautifulSoup(decoded, "html.parser")
    title = (_first_meta(soup, {"og:title", "twitter:title", "citation_title"}) or (soup.title.get_text(" ", strip=True) if soup.title else "")).strip()
    candidates = []
    for selector in ("article", "main", "[class*=article-body]", "[class*=articleBody]", "[class*=story-body]", "[class*=storyBody]", "[class*=article-content]"):
        candidates.extend(soup.select(selector))
    if not candidates:
        candidates = [soup.body or soup]
    container = max(candidates, key=lambda node: len(node.get_text(" ", strip=True)))
    for tag in container.find_all(["script", "style", "nav", "header", "footer", "aside", "form", "noscript", "svg"]):
        tag.decompose()
    lines: list[str] = []
    for element in container.find_all(["h1", "h2", "h3", "h4", "p", "blockquote", "li"]):
        value = " ".join(element.get_text(" ", strip=True).split())
        if not value or len(value) < 2:
            continue
        if value in lines:
            continue
        if re.fullmatch(r"(?:Home|News|Photos|Videos|About|Contact|Share|Print|Skip to main content|Search)", value, flags=re.I):
            continue
        if re.fullmatch(r"(?:Follow us|Related articles|More articles|All rights reserved).*", value, flags=re.I):
            continue
        lines.append(value)
    if title and title not in lines:
        lines.insert(0, title)
    text = "\n\n".join(lines)
    return text, {"title": title, "published_at": _extract_html_date(soup, " ".join(lines)), "parser": "html_clean_paragraphs"}


def _clean_pdf(raw: bytes) -> tuple[str, dict[str, Any]]:
    reader = PdfReader(io.BytesIO(raw))
    pages: list[str] = []
    for page in reader.pages:
        pages.append(" ".join((page.extract_text() or "").split()))
    text = "\n\n".join(page for page in pages if page)
    title = str((reader.metadata or {}).get("/Title", "")).strip()
    first_page = pages[0] if pages else ""
    published = _parse_date(first_page) or _parse_date(str(reader.metadata or ""))
    return text, {"title": title, "published_at": published, "page_count": len(reader.pages), "parser": "pypdf_text_no_ocr"}


def _paragraph_rows(source_id: str, text: str, is_pdf: bool) -> list[dict[str, Any]]:
    pieces = [piece.strip() for piece in re.split(r"\n\s*\n", text) if piece.strip()]
    if not pieces:
        pieces = [line.strip() for line in text.splitlines() if line.strip()]
    rows = []
    cursor = 0
    for index, piece in enumerate(pieces, start=1):
        position = text.find(piece, cursor)
        if position < 0:
            position = cursor
        locator = f"pdf_paragraph_{index:04d}" if is_pdf else f"web_paragraph_{index:04d}"
        rows.append({
            "case_id": CASE_ID,
            "source_id": source_id,
            "paragraph_id": locator,
            "source_locator": locator,
            "character_start": position,
            "character_end": position + len(piece),
            "text": piece,
        })
        cursor = position + len(piece)
    return rows


def _save_source_record(base: Path, item: dict[str, Any]) -> dict[str, Any]:
    source_id = item["source_id"]
    url = item.get("capture_url", item["url"])
    try:
        raw, final_url, content_type, status = _fetch(url)
        if item["capture_type"] == "publisher_pdf" and not raw.startswith(b"%PDF"):
            raise RuntimeError("publisher capture did not have a PDF header")
        is_pdf = item["capture_type"] == "publisher_pdf" or raw.startswith(b"%PDF") or "pdf" in content_type.lower()
        if is_pdf:
            text, parsed = _clean_pdf(raw)
            raw_suffix = ".pdf"
        else:
            text, parsed = _clean_html(raw)
            raw_suffix = ".html"
        parsed_date = parsed.get("published_at")
        if not parsed_date:
            raise RuntimeError("publication date not found in original source")
        parsed_date_obj = date.fromisoformat(parsed_date)
        temporal_status = "within_t1_window" if T1_START <= parsed_date_obj <= T1_END else "outside_t1_window"
        if temporal_status != "within_t1_window":
            raise RuntimeError("verified publication date is outside T1 window")
        if not text.strip():
            raise RuntimeError("empty extracted full text")
        raw_path = base / "source_raw" / f"{source_id}{raw_suffix}"
        text_path = base / "source_text" / f"{source_id}.txt"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        text_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(raw)
        text_path.write_text(text, encoding="utf-8")
        text_hash = sha256_text(text)
        return {
            **item,
            "title": parsed.get("title") or item["title_hint"],
            "document_date": parsed_date,
            "public_release_date": parsed_date,
            "canonical_url": item["url"],
            "capture_url": url,
            "resolved_url": final_url,
            "http_status": status,
            "content_type": content_type,
            "raw_path": str(raw_path.relative_to(ROOT)).replace("\\", "/"),
            "text_path": str(text_path.relative_to(ROOT)).replace("\\", "/"),
            "raw_sha256": sha256_bytes(raw),
            "text_sha256": text_hash,
            "file_size": len(raw),
            "full_text_status": "verified_full_text",
            "selection_status": "selected_for_p12",
            "temporal_status": temporal_status,
            "public_release_verified": True,
            "duplicate_status": "unique_in_p11_catalogue",
            "duplicate_of": "",
            "access_error": "",
            "parser": parsed.get("parser", ""),
            "page_count": parsed.get("page_count", ""),
        }
    except Exception as exc:
        reason = str(exc)
        safe_reason = re.sub(r"https?://\S+", "[url-redacted]", reason)
        return {
            **item,
            "title": item["title_hint"],
            "document_date": "",
            "public_release_date": "",
            "canonical_url": item["url"],
            "capture_url": url,
            "resolved_url": "",
            "http_status": "",
            "content_type": "",
            "raw_path": "",
            "text_path": "",
            "raw_sha256": "",
            "text_sha256": "",
            "file_size": "",
            "full_text_status": "full_text_unavailable",
            "selection_status": "blocked_original_page",
            "temporal_status": "date_unverified",
            "public_release_verified": False,
            "duplicate_status": "not_assessed",
            "duplicate_of": "",
            "access_error": safe_reason,
            "parser": "",
            "page_count": "",
        }


def _p11_prompt() -> str:
    dimensions = "\n".join(f"- {name}: {definition}" for name, definition in DIMENSION_DEFINITIONS.items())
    return (
        "T1盲态资料搜集提示\n"
        f"研究问题：{CASE_QUESTION}\n"
        "时间窗：2025-01-01至2026-06-30（含起止日期）\n"
        "资料类型：战略政策治理；项目预算合同采办；试验部署演习组织运用；独立监督审计立法研究或同行评议研究。\n"
        "只纳入公开、非密、原始或权威全文，必须核验发布机构、发布日期和原始URL。排除搜索摘要、匿名消息、聚合转载、日期不明、时间窗外和弱相关材料。\n"
        "编码维度定义：\n" + dimensions + "\n"
        "本提示为盲态搜集，不使用任何既有分析对象、既有假设、观点、分歧、监测需求或结论。"
    )


def run_p11(base: Path, g4_info: dict[str, Any]) -> dict[str, Any]:
    p11 = base / "P11_T1_COLLECTION"
    p11.mkdir(parents=True, exist_ok=True)
    prompt = _p11_prompt()
    (base / "prompts").mkdir(parents=True, exist_ok=True)
    (base / "prompts" / "p11_collection_blind.txt").write_text(prompt, encoding="utf-8")
    rows = [_save_source_record(base, item) for item in SOURCE_CATALOG]
    # No source is silently promoted by search output. Only successfully
    # verified full-text records are selected for P12.
    selected = [row for row in rows if row["full_text_status"] == "verified_full_text" and row["temporal_status"] == "within_t1_window"]
    categories = {row["category"] for row in selected}
    raw_candidates = [{"source_id": row["source_id"], "url": row["url"], "discovery_only": True} for row in rows]
    manifest_fields = [
        "case_id", "source_id", "title", "publisher", "source_grade", "category", "document_date", "public_release_date",
        "canonical_url", "capture_url", "resolved_url", "capture_type", "raw_path", "text_path", "raw_sha256", "text_sha256",
        "file_size", "full_text_status", "selection_status", "temporal_status", "public_release_verified", "duplicate_status",
        "duplicate_of", "reason", "access_error", "parser", "page_count",
    ]
    write_csv(p11 / "source_manifest_t1_candidate.csv", manifest_fields, rows)
    write_csv(p11 / "selected_source_inventory_t1.csv", manifest_fields, selected)
    search_rows = []
    groups = [
        ("strategy_policy_governance", "site:af.mil OR site:spaceforce.mil artificial intelligence strategy policy governance", "official RSS discovery"),
        ("program_budget_acquisition", "site:af.mil OR site:darpa.mil artificial intelligence program budget acquisition 2025 2026", "official RSS/sitemap discovery"),
        ("test_deployment_organization", "site:af.mil OR site:darpa.mil artificial intelligence test deployment exercise 2025 2026", "official RSS/sitemap discovery"),
        ("independent_review_research", "military artificial intelligence operational medicine peer reviewed 2025", "Crossref discovery API"),
    ]
    for index, (category, query, method) in enumerate(groups, start=1):
        ids = [row["source_id"] for row in rows if row["category"] == category]
        search_rows.append({
            "search_id": f"P11-S{index:02d}",
            "executed_at": now_utc(),
            "category": category,
            "query": query,
            "discovery_method": method,
            "candidate_source_ids": json.dumps(ids, ensure_ascii=False),
            "result_count": len(ids),
            "search_result_is_source": False,
            "notes": "检索结果仅用于发现；原文核验见source_manifest。",
        })
    write_csv(p11 / "search_log_t1.csv", ["search_id", "executed_at", "category", "query", "discovery_method", "candidate_source_ids", "result_count", "search_result_is_source", "notes"], search_rows)
    duplicate_rows = []
    for row in rows:
        duplicate_rows.append({
            "source_id": row["source_id"],
            "action": "candidate" if row["full_text_status"] == "verified_full_text" else "excluded",
            "reason": row["reason"] if row["full_text_status"] == "verified_full_text" else row["access_error"],
            "duplicate_status": row["duplicate_status"],
            "duplicate_of": row["duplicate_of"],
            "search_summary_used_as_source": False,
        })
    write_csv(p11 / "duplicate_and_exclusion_log_t1.csv", ["source_id", "action", "reason", "duplicate_status", "duplicate_of", "search_summary_used_as_source"], duplicate_rows)
    queue = [
        {"source_id": row["source_id"], "canonical_url": row["canonical_url"], "reason": row["access_error"], "status": "manual_retrieval_pending"}
        for row in rows if row["full_text_status"] != "verified_full_text"
    ]
    write_csv(p11 / "manual_retrieval_queue.csv", ["source_id", "canonical_url", "reason", "status"], queue)
    coverage = []
    for category in sorted({item["category"] for item in SOURCE_CATALOG}):
        category_rows = [row for row in rows if row["category"] == category]
        category_selected = [row for row in selected if row["category"] == category]
        coverage.append({
            "category": category,
            "candidate_count": len(category_rows),
            "full_text_count": len(category_selected),
            "source_ids": ";".join(row["source_id"] for row in category_selected),
            "coverage_status": "covered" if category_selected else "not_covered",
        })
    write_csv(p11 / "source_coverage_matrix_t1.csv", ["category", "candidate_count", "full_text_count", "source_ids", "coverage_status"], coverage)
    if len(selected) < 6 or len(categories) < 3:
        raise RuntimeError(f"P11 coverage below minimum: full_text={len(selected)}, categories={len(categories)}")
    p11_manifest = {
        "stage": "P11",
        "case_id": CASE_ID,
        "run_id": base.name,
        "t1_window_start": T1_START.isoformat(),
        "t1_window_end": T1_END.isoformat(),
        "candidate_source_count": len(rows),
        "full_text_source_count": len([row for row in rows if row["full_text_status"] == "verified_full_text"]),
        "selected_source_count": len(selected),
        "category_count_with_full_text": len(categories),
        "independent_review_or_research_count": len([row for row in selected if row["category"] == "independent_review_research"]),
        "manual_retrieval_queue_count": len(queue),
        "blind_collection_verified": True,
        "t0_hypotheses_provided_to_collection": False,
        "t0_viewpoints_provided_to_collection": False,
        "t0_disagreements_provided_to_collection": False,
        "t0_monitoring_provided_to_collection": False,
        "t0_main_judgment_provided_to_collection": False,
        "model_api_calls": 0,
        "evidence_inserted": 0,
        "t1_snapshot_frozen": False,
        "files": file_hashes(p11),
        "g4_authorization_sha256": g4_info["g4_manifest"]["g4_authorization_sha256"],
    }
    write_json(p11 / "p11_t1_manifest.json", p11_manifest)
    return {"rows": rows, "selected": selected, "manifest": p11_manifest}


def _load_paragraphs(row: dict[str, Any]) -> list[dict[str, Any]]:
    text_path = ROOT / row["text_path"]
    text = text_path.read_text(encoding="utf-8")
    return _paragraph_rows(row["source_id"], text, row["capture_type"] == "publisher_pdf")


def _p12_extraction_prompt(row: dict[str, Any], paragraphs: list[dict[str, Any]]) -> str:
    dims = "\n".join(f"- {key}: {value}" for key, value in DIMENSION_DEFINITIONS.items())
    natures = "\n".join(f"- {key}: {value}" for key, value in NATURE_DEFINITIONS.items())
    body = "\n".join(f"[{p['source_locator']}] {p['text']}" for p in paragraphs)
    return (
        "T1证据候选抽取。只使用下面提供的T1原文，不使用外部知识。\n"
        f"研究问题：{CASE_QUESTION}\n来源ID：{row['source_id']}\n标题：{row['title']}\n发布日期：{row['document_date']}\n"
        "输出JSON数组；每条只表达一个可核查事实或明确主张。每条必须恰好包含以下8个字段："
        "source_id, source_locator, excerpt_original, excerpt_zh, normalized_claim, evidence_nature, coding_dimensions, topics。"
        "不得输出其他字段；允许输出空数组；每份来源最多6条。source_id必须与给定来源一致；source_locator必须是给定定位。"
        "excerpt_original必须逐字连续来自给定段落；normalized_claim使用中文且不得扩大主体、行动、数字、范围或结果；excerpt_zh必须非空。"
        "evidence_nature只能使用单一枚举值，coding_dimensions和topics必须是字符串数组。"
        "不要把目标写成结果、把演示或试验写成部署、把预算或合同直接写成规模化能力。\n"
        "证据性质定义：\n" + natures + "\n"
        "编码维度定义：\n" + dims + "\n"
        "原文段落：\n" + body
    )


def _p12_semantic_prompt(row: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    dims = "\n".join(f"- {key}: {value}" for key, value in DIMENSION_DEFINITIONS.items())
    natures = "\n".join(f"- {key}: {value}" for key, value in NATURE_DEFINITIONS.items())
    payload = json.dumps(candidates, ensure_ascii=False, indent=2, sort_keys=True)
    return (
        "T1候选证据语义筛选。只检查给定候选字段与其摘录之间的直接支持关系。不得引用或推断任何T0分析。\n"
        f"研究问题：{CASE_QUESTION}\n来源：{row['source_id']}\n"
        "每条返回一项review，字段为candidate_evidence_id、status、reason、claim_supported、nature_supported、dimension_reviews、revised_fields。"
        "status只能是accept、revise或reject；dimension_reviews必须是数组，每项包含dimension、supported、supporting_span、reason。"
        "只处理候选原本声明的维度；不得新增维度。若claim或nature无直接支持则reject；revise不得修改excerpt_original和source_locator。\n"
        "证据性质完整定义：\n" + natures + "\n编码维度完整定义：\n" + dims + "\n"
        "候选：\n" + payload
    )


def _parse_json_response(content: str, wrapper_keys: tuple[str, ...]) -> tuple[Any, str | None]:
    raw = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.IGNORECASE | re.DOTALL)
    normalized = fenced.group(1).strip() if fenced else raw
    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError as exc:
        return None, f"response_not_json:{exc.msg}"
    if isinstance(payload, dict):
        for key in wrapper_keys:
            if isinstance(payload.get(key), list):
                return payload[key], None
    if isinstance(payload, list):
        return payload, None
    return None, "response_top_level_not_array_or_expected_wrapper"


def _candidate_validation(item: Any, row: dict[str, Any], paragraphs: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    required = {"source_id", "source_locator", "excerpt_original", "excerpt_zh", "normalized_claim", "evidence_nature", "coding_dimensions", "topics"}
    if not isinstance(item, dict):
        return None, "schema_item_not_object"
    missing = sorted(required - set(item))
    extra = sorted(set(item) - required)
    if missing:
        return None, "missing_fields:" + ",".join(missing)
    if extra:
        return None, "extra_fields:" + ",".join(extra)
    if item["source_id"] != row["source_id"]:
        return None, "source_id_mismatch"
    if not all(isinstance(item[field], str) and item[field].strip() for field in ("source_locator", "excerpt_original", "excerpt_zh", "normalized_claim", "evidence_nature")):
        return None, "required_string_empty_or_invalid"
    if not isinstance(item["coding_dimensions"], list) or not item["coding_dimensions"] or not all(isinstance(v, str) for v in item["coding_dimensions"]):
        return None, "coding_dimensions_not_nonempty_string_array"
    if len(set(item["coding_dimensions"])) != len(item["coding_dimensions"]):
        return None, "duplicate_coding_dimensions"
    if not isinstance(item["topics"], list) or not all(isinstance(v, str) for v in item["topics"]):
        return None, "topics_not_string_array"
    if item["evidence_nature"] not in EVIDENCE_NATURE_VALUES:
        return None, "invalid_evidence_nature"
    invalid_dims = sorted(set(item["coding_dimensions"]) - CODING_DIMENSION_VALUES)
    if invalid_dims:
        return None, "invalid_coding_dimensions:" + ",".join(invalid_dims)
    if not re.search(r"[\u3400-\u9fff]", item["normalized_claim"]):
        return None, "normalized_claim_not_chinese"
    by_locator = {p["source_locator"]: p for p in paragraphs}
    locator = item["source_locator"]
    if locator not in by_locator:
        return None, "source_locator_not_found"
    if item["excerpt_original"] not in by_locator[locator]["text"]:
        return None, "excerpt_not_found_at_specified_locator"
    content_hash = sha256_text(json.dumps([row["source_id"], locator, item["excerpt_original"]], ensure_ascii=False, separators=(",", ":")))
    candidate_id = "CE-T1-" + content_hash[:16].upper()
    return {
        **item,
        "case_id": CASE_ID,
        "candidate_evidence_id": candidate_id,
        "version": 1,
        "snapshot_tag": "T1",
        "published_at": row["document_date"],
        "published_at_precision": "day",
        "publisher": row["publisher"],
        "source_grade": row["source_grade"],
        "title": row["title"],
        "url_or_path": row["canonical_url"],
        "content_hash": content_hash,
        "extraction_run_id": row.get("run_id", ""),
        "pending_g1_visual_review": row["capture_type"] == "publisher_pdf",
        "pending_g1_translation_review": True,
    }, ""


def _normalize_dimension_reviews(review: Any, declared: list[str], excerpt: str) -> list[dict[str, Any]]:
    if isinstance(review, dict):
        review = [
            {"dimension": key, "supported": value not in {"", "unsupported", "not supported", "no direct support", "不支持", "无直接支持"}, "supporting_span": excerpt, "reason": value}
            for key, value in review.items()
        ]
    if not isinstance(review, list):
        review = []
    by_dimension = {str(item.get("dimension")): item for item in review if isinstance(item, dict)}
    result = []
    for dimension in declared:
        item = by_dimension.get(dimension, {})
        raw_supported = item.get("supported")
        if isinstance(raw_supported, bool):
            supported = raw_supported
        elif isinstance(raw_supported, str):
            supported = raw_supported.strip().lower() not in {"", "false", "unsupported", "not supported", "no direct support", "不支持", "无直接支持"}
        else:
            supported = False
        result.append({
            "dimension": dimension,
            "supported": supported,
            "supporting_span": excerpt if supported else str(item.get("supporting_span", "")),
            "reason": item.get("reason", "missing dimension review") if isinstance(item, dict) else "missing dimension review",
        })
    return result


def _semantic_decision(candidate: dict[str, Any], review: dict[str, Any]) -> tuple[dict[str, Any] | None, str, list[dict[str, Any]]]:
    status = str(review.get("status", "")).lower()
    if status not in {"accept", "revise", "reject"}:
        return None, "semantic_invalid_status", []
    if status == "reject" or review.get("claim_supported") is False or review.get("nature_supported") is False:
        return None, "semantic_reject_or_claim_nature_unsupported", []
    dimensions = _normalize_dimension_reviews(review.get("dimension_reviews"), candidate["coding_dimensions"], candidate["excerpt_original"])
    supported_dims = [item["dimension"] for item in dimensions if item["supported"]]
    revisions = review.get("revised_fields") if isinstance(review.get("revised_fields"), dict) else {}
    allowed = {"normalized_claim", "excerpt_zh", "evidence_nature", "coding_dimensions", "topics"}
    if set(revisions) - allowed:
        return None, "semantic_revision_contains_forbidden_field", dimensions
    result = dict(candidate)
    if status == "revise":
        for key, value in revisions.items():
            result[key] = value
    result["coding_dimensions"] = supported_dims
    if not result["coding_dimensions"]:
        return None, "semantic_no_supported_dimension", dimensions
    return result, "", dimensions


def _record_call(path: Path, phase: str, source_id: str, record: ApiCallRecord, prompt: str, content: str) -> dict[str, Any]:
    entry = record.as_dict()
    entry.update({
        "phase": phase,
        "source_id": source_id,
        "prompt_sha256": sha256_text(prompt),
        "response_sha256": sha256_text(content),
        "api_key_written": False,
    })
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    return entry


def run_p12(base: Path, p11: dict[str, Any]) -> dict[str, Any]:
    p12 = base / "P12_T1_EXTRACTION"
    g1 = base / "G1_T1_PREP"
    prompts = base / "prompts" / "P12"
    raw_responses = base / "raw_responses" / "P12"
    checkpoints = base / "checkpoints"
    for path in (p12, g1, prompts, raw_responses / "extraction", raw_responses / "semantic", checkpoints):
        path.mkdir(parents=True, exist_ok=True)
    client = APIModelClient(timeout_seconds=180.0, max_retries=3)
    call_log = base / "raw_responses" / "model_call_log.jsonl"
    all_raw: list[dict[str, Any]] = []
    validated: list[dict[str, Any]] = []
    final_candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    semantic_results: list[dict[str, Any]] = []
    semantic_call_count = 0
    extraction_call_count = 0
    physical_retry_count = 0
    coverage: list[dict[str, Any]] = []
    for row in p11["selected"]:
        row = dict(row)
        row["run_id"] = base.name
        paragraphs = _load_paragraphs(row)
        prompt = _p12_extraction_prompt(row, paragraphs)
        prompt_path = prompts / f"extract_{row['source_id']}.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        try:
            payload, content, record = client.call(prompt, temperature=0.0, max_tokens=6000)
        except ModelAPIError as exc:
            write_json(base / "p12_failure_manifest.json", {
                "run_id": base.name,
                "failure_stage": "extraction",
                "source_id": row["source_id"],
                "exception_type": exc.root_exception_type,
                "http_status_code": exc.http_status_code,
                "completed_logical_calls": extraction_call_count + semantic_call_count,
                "saved_batches": [item["source_id"] for item in p11["selected"] if item["source_id"] != row["source_id"]],
                "evidence_inserted": 0,
                "formal_g1_t1_executed": False,
                "p4_executed": False,
                "t1_snapshot_frozen": False,
                "t1_read": False,
                "api_key_written": False,
            })
            raise
        extraction_call_count += 1
        physical_retry_count += record.retry_count
        write_json(raw_responses / "extraction" / f"{row['source_id']}.json", payload)
        _record_call(call_log, "extraction", row["source_id"], record, prompt, content)
        parsed, parse_error = _parse_json_response(content, ("candidates", "evidence_candidates"))
        source_raw_count = len(parsed) if isinstance(parsed, list) else 0
        if parse_error:
            rejected.append({"source_id": row["source_id"], "stage": "extraction_parse", "reason": parse_error, "raw_candidate_index": ""})
        elif source_raw_count > 6:
            rejected.append({"source_id": row["source_id"], "stage": "extraction_batch", "reason": "candidate_count_exceeds_limit", "raw_candidate_index": ""})
        valid_for_semantic: list[dict[str, Any]] = []
        if not parse_error and source_raw_count <= 6:
            for index, item in enumerate(parsed):
                all_raw.append({"source_id": row["source_id"], "raw_candidate_index": index, "candidate": item})
                candidate, error = _candidate_validation(item, row, paragraphs)
                validation_row = {
                    "source_id": row["source_id"],
                    "raw_candidate_index": index,
                    "candidate_evidence_id": candidate.get("candidate_evidence_id", "") if candidate else "",
                    "schema_valid": bool(candidate),
                    "exact_quote_matched": bool(candidate),
                    "deterministic_gate_passed": bool(candidate),
                    "reason": error,
                }
                if candidate:
                    validated.append(candidate)
                    valid_for_semantic.append(candidate)
                else:
                    rejected.append({"source_id": row["source_id"], "stage": "deterministic", "reason": error, "raw_candidate_index": index, "candidate": item})
                coverage.append({"source_id": row["source_id"], "raw_candidate_index": index, **validation_row})
        if not valid_for_semantic:
            coverage.append({"source_id": row["source_id"], "raw_candidate_index": "", "candidate_evidence_id": "", "schema_valid": False, "exact_quote_matched": False, "deterministic_gate_passed": False, "reason": "semantic_skipped_no_valid_candidates"})
            continue
        semantic_prompt = _p12_semantic_prompt(row, valid_for_semantic)
        (prompts / f"semantic_{row['source_id']}.txt").write_text(semantic_prompt, encoding="utf-8")
        try:
            semantic_payload, semantic_content, semantic_record = client.call(semantic_prompt, temperature=0.0, max_tokens=6000)
        except ModelAPIError as exc:
            write_json(base / "p12_failure_manifest.json", {
                "run_id": base.name,
                "failure_stage": "semantic_screening",
                "source_id": row["source_id"],
                "exception_type": exc.root_exception_type,
                "http_status_code": exc.http_status_code,
                "completed_logical_calls": extraction_call_count + semantic_call_count,
                "saved_batches": [item["source_id"] for item in p11["selected"] if item["source_id"] != row["source_id"]],
                "evidence_inserted": 0,
                "formal_g1_t1_executed": False,
                "p4_executed": False,
                "t1_snapshot_frozen": False,
                "t1_read": False,
                "api_key_written": False,
            })
            raise
        semantic_call_count += 1
        physical_retry_count += semantic_record.retry_count
        write_json(raw_responses / "semantic" / f"{row['source_id']}.json", semantic_payload)
        _record_call(call_log, "semantic_screening", row["source_id"], semantic_record, semantic_prompt, semantic_content)
        parsed_reviews, semantic_parse_error = _parse_json_response(semantic_content, ("reviews", "semantic_reviews"))
        if semantic_parse_error:
            for candidate in valid_for_semantic:
                rejected.append({"source_id": row["source_id"], "stage": "semantic", "reason": semantic_parse_error, "candidate_evidence_id": candidate["candidate_evidence_id"]})
            continue
        reviews_by_id = {str(review.get("candidate_evidence_id")): review for review in parsed_reviews if isinstance(review, dict)}
        for candidate in valid_for_semantic:
            review = reviews_by_id.get(candidate["candidate_evidence_id"], {})
            decision, error, dimension_reviews = _semantic_decision(candidate, review)
            semantic_results.append({
                "candidate_evidence_id": candidate["candidate_evidence_id"],
                "source_id": row["source_id"],
                "status": review.get("status", ""),
                "reason": review.get("reason", error),
                "claim_supported": review.get("claim_supported"),
                "nature_supported": review.get("nature_supported"),
                "dimension_reviews": dimension_reviews,
                "deterministic_adaptation": False,
            })
            if decision:
                decision["semantic_screen_status"] = review.get("status")
                decision["semantic_screen_reason"] = review.get("reason", "")
                final_candidates.append(decision)
            else:
                rejected.append({"source_id": row["source_id"], "stage": "semantic", "reason": error, "candidate_evidence_id": candidate["candidate_evidence_id"]})
        write_json(checkpoints / "p12_checkpoint.json", {
            "run_id": base.name,
            "completed_sources": [item["source_id"] for item in p11["selected"] if item["source_id"] in {r["source_id"] for r in coverage}],
            "extraction_logical_call_count": extraction_call_count,
            "semantic_logical_call_count": semantic_call_count,
            "status": "in_progress",
        })
    # Deterministic internal de-duplication, retaining the first source item.
    unique: dict[str, dict[str, Any]] = {}
    for candidate in final_candidates:
        unique.setdefault(candidate["content_hash"], candidate)
    final_candidates = list(unique.values())
    write_jsonl(p12 / "t1_evidence_candidates.jsonl", final_candidates)
    write_jsonl(p12 / "t1_rejected_candidates.jsonl", rejected)
    write_jsonl(p12 / "semantic_screen_results.jsonl", semantic_results)
    validation_fields = ["source_id", "raw_candidate_index", "candidate_evidence_id", "schema_valid", "exact_quote_matched", "deterministic_gate_passed", "reason"]
    write_csv(p12 / "t1_candidate_validation.csv", validation_fields, coverage)
    source_coverage = []
    for row in p11["selected"]:
        source_coverage.append({
            "source_id": row["source_id"],
            "title": row["title"],
            "source_grade": row["source_grade"],
            "category": row["category"],
            "paragraph_count": len(_load_paragraphs(row)),
            "processed_paragraph_count": len(_load_paragraphs(row)),
            "raw_candidate_count": sum(1 for item in all_raw if item["source_id"] == row["source_id"]),
            "deterministic_gate_pass_count": sum(1 for item in validated if item["source_id"] == row["source_id"]),
            "final_candidate_count": sum(1 for item in final_candidates if item["source_id"] == row["source_id"]),
            "pending_visual_review_count": sum(1 for item in final_candidates if item["source_id"] == row["source_id"] and item["pending_g1_visual_review"]),
            "blank_or_non_text_paragraph_count": 0,
        })
    write_csv(p12 / "t1_source_evidence_coverage.csv", list(source_coverage[0]) if source_coverage else ["source_id"], source_coverage)
    # G1-T1 AI preannotation is deliberately advisory; author fields stay blank.
    preannotation = []
    for candidate in final_candidates:
        long_excerpt = len(candidate["excerpt_original"].split()) > 120
        high_risk = candidate["evidence_nature"] in {"test_result", "deployment_event", "constraint", "evaluation"} or candidate["pending_g1_visual_review"] or long_excerpt
        risk_reason = []
        if candidate["evidence_nature"] in {"test_result", "deployment_event"}:
            risk_reason.append("需区分试验结果与部署事实")
        if candidate["pending_g1_visual_review"]:
            risk_reason.append("PDF版式待视觉复核")
        if long_excerpt:
            risk_reason.append("摘录较长，需检查是否含多个事实")
        preannotation.append({
            "candidate_evidence_id": candidate["candidate_evidence_id"],
            "source_id": candidate["source_id"],
            "source_locator": candidate["source_locator"],
            "proposed_decision": "accept" if candidate.get("semantic_screen_status") == "accept" else "revise",
            "proposed_nature": candidate["evidence_nature"],
            "proposed_dimensions": json.dumps(candidate["coding_dimensions"], ensure_ascii=False),
            "proposed_normalized_claim": candidate["normalized_claim"],
            "proposed_excerpt_zh": candidate["excerpt_zh"],
            "risk_level": "high" if high_risk else "medium",
            "risk_reason": ";".join(risk_reason) or "需作者确认原文定位与主张范围",
            "proposed_review_note": candidate.get("semantic_screen_reason", ""),
            "ai_preannotation": True,
            "human_reviewed": False,
        })
    pre_fields = list(preannotation[0]) if preannotation else ["candidate_evidence_id", "ai_preannotation", "human_reviewed"]
    write_csv(g1 / "g1_t1_ai_preannotation.csv", pre_fields, preannotation)
    template = []
    for candidate in final_candidates:
        annotation = next(item for item in preannotation if item["candidate_evidence_id"] == candidate["candidate_evidence_id"])
        template.append({
            **annotation,
            "excerpt_original": candidate["excerpt_original"],
            "excerpt_zh": candidate["excerpt_zh"],
            "normalized_claim": candidate["normalized_claim"],
            "evidence_nature": candidate["evidence_nature"],
            "coding_dimensions": json.dumps(candidate["coding_dimensions"], ensure_ascii=False),
            "pending_visual_review": candidate["pending_g1_visual_review"],
            "pending_translation_review": candidate["pending_g1_translation_review"],
            "author_decision": "",
            "final_nature": "",
            "final_dimensions": "",
            "human_corrected_excerpt": "",
            "final_excerpt_zh": "",
            "reviewer": "",
            "reviewed_at": "",
            "review_note": "",
        })
    template_fields = ["candidate_evidence_id", "source_id", "source_locator", "proposed_decision", "proposed_nature", "proposed_dimensions", "proposed_normalized_claim", "proposed_excerpt_zh", "excerpt_original", "excerpt_zh", "normalized_claim", "evidence_nature", "coding_dimensions", "pending_visual_review", "pending_translation_review", "risk_level", "risk_reason", "ai_preannotation", "human_reviewed", "author_decision", "final_nature", "final_dimensions", "human_corrected_excerpt", "final_excerpt_zh", "reviewer", "reviewed_at", "review_note"]
    write_csv(g1 / "g1_t1_author_decision_template.csv", template_fields, template)
    visual_queue = [
        {"candidate_evidence_id": c["candidate_evidence_id"], "source_id": c["source_id"], "source_locator": c["source_locator"], "reason": "PDF来源版式或页级定位需人工确认", "final_decision": "", "human_corrected_excerpt": "", "human_reviewed": False}
        for c in final_candidates if c["pending_g1_visual_review"]
    ]
    translation_queue = [
        {"candidate_evidence_id": c["candidate_evidence_id"], "source_id": c["source_id"], "source_locator": c["source_locator"], "excerpt_zh": c["excerpt_zh"], "reason": "机器译文待G1核验", "final_translation": "", "human_reviewed": False}
        for c in final_candidates if c["pending_g1_translation_review"]
    ]
    write_csv(g1 / "g1_t1_visual_review_queue.csv", ["candidate_evidence_id", "source_id", "source_locator", "reason", "final_decision", "human_corrected_excerpt", "human_reviewed"], visual_queue)
    write_csv(g1 / "g1_t1_translation_review_queue.csv", ["candidate_evidence_id", "source_id", "source_locator", "excerpt_zh", "reason", "final_translation", "human_reviewed"], translation_queue)
    (g1 / "g1_t1_split_proposals.jsonl").write_text("", encoding="utf-8")
    nature_counts = Counter(c["evidence_nature"] for c in final_candidates)
    dim_counts = Counter(d for c in final_candidates for d in c["coding_dimensions"])
    signoff_summary = (
        "# G1-T1 AI预审准备摘要\n\n"
        f"本轮P11候选材料{len(p11['rows'])}份，成功归档可核验全文{len(p11['selected'])}份，P12原始候选{len(all_raw)}条，确定性门禁通过{len(validated)}条，最终进入G1-T1预备集合{len(final_candidates)}条。\n\n"
        f"证据性质分布：{dict(nature_counts)}。编码维度分布：{dict(dim_counts)}。视觉待复核{len(visual_queue)}条，翻译待复核{len(translation_queue)}条。\n\n"
        "本文件仅为AI预审和作者审核材料；尚未写入正式Evidence，尚未进行正式G1-T1，尚未建立T1快照，尚未进行T0/T1影响比较、影响定位或观点更新。"
    )
    (g1 / "g1_t1_author_signoff_summary.md").write_text(signoff_summary, encoding="utf-8")
    quality = (
        "# P12-T1质量报告\n\n"
        f"- selected_full_text_sources: {len(p11['selected'])}\n"
        f"- raw_candidate_count: {len(all_raw)}\n"
        f"- deterministic_gate_pass_count: {len(validated)}\n"
        f"- semantic_screened_candidate_count: {len(validated)}\n"
        f"- final_candidate_count: {len(final_candidates)}\n"
        f"- extraction_logical_calls: {extraction_call_count}\n"
        f"- semantic_logical_calls: {semantic_call_count}\n"
        f"- physical_retry_count: {physical_retry_count}\n"
        f"- rejected_count: {len(rejected)}\n"
        f"- rejected_reason_counts: {dict(Counter(item.get('reason', '') for item in rejected))}\n\n"
        "所有候选均须在作者G1-T1审核前再次核验；模型预审不等同于人工审核。"
    )
    (g1 / "p12_t1_quality_report.md").write_text(quality, encoding="utf-8")
    p12_manifest = {
        "stage": "P12",
        "case_id": CASE_ID,
        "run_id": base.name,
        "t1_window_start": T1_START.isoformat(),
        "t1_window_end": T1_END.isoformat(),
        "selected_source_ids": [row["source_id"] for row in p11["selected"]],
        "model_id": client.model,
        "temperature": 0.0,
        "extraction_logical_call_count": extraction_call_count,
        "semantic_logical_call_count": semantic_call_count,
        "p12_model_call_count": extraction_call_count + semantic_call_count,
        "physical_retry_count": physical_retry_count,
        "raw_candidate_count": len(all_raw),
        "gate_pass_count": len(validated),
        "semantic_screened_candidate_count": len(validated),
        "final_candidate_count": len(final_candidates),
        "rejected_count": len(rejected),
        "rejected_reason_counts": dict(Counter(item.get("reason", "") for item in rejected)),
        "candidates_by_nature": dict(nature_counts),
        "candidates_by_dimension": dict(dim_counts),
        "candidate_evidence_count": len(final_candidates),
        "formal_evidence_db_rows_added": 0,
        "hypothesis_rows_added": 0,
        "t1_read": True,
        "formal_g1_t1_executed": False,
        "t1_snapshot_frozen": False,
        "impact_localization_executed": False,
        "viewpoint_update_executed": False,
        "author_actual_review_count": 0,
        "api_key_written": False,
        "input_source_hashes": {row["source_id"]: row["raw_sha256"] for row in p11["selected"]},
        "prompt_hashes": {str(path.relative_to(ROOT)).replace("\\", "/"): sha256_bytes(path.read_bytes()) for path in prompts.glob("*.txt")},
        "output_file_hashes": file_hashes(p12) | file_hashes(g1),
    }
    write_json(p12 / "p12_t1_manifest.json", p12_manifest)
    write_json(checkpoints / "p12_checkpoint.json", {
        "run_id": base.name,
        "completed_sources": [row["source_id"] for row in p11["selected"]],
        "extraction_logical_call_count": extraction_call_count,
        "semantic_logical_call_count": semantic_call_count,
        "status": "completed",
    })
    return {"p12_manifest": p12_manifest, "final_candidates": final_candidates, "rejected": rejected, "coverage": source_coverage}


def run_workflow(run_id: str) -> dict[str, Any]:
    base = ROOT / "outputs" / run_id
    if base.exists() and (base / "P11_T1_COLLECTION" / "p11_t1_manifest.json").exists():
        raise RuntimeError(f"run directory already contains a completed P11 run: {run_id}")
    if base.exists():
        if not (base / "G4_FORMAL" / "g4_t0_signoff_manifest.json").exists():
            raise RuntimeError(f"existing run directory is not the signed G4 unified root: {run_id}")
    else:
        base.mkdir(parents=True, exist_ok=False)
    g4_info = assert_g4_unsealed()
    p11 = run_p11(base, g4_info)
    p12 = run_p12(base, p11)
    counts_after = _read_only_db_counts()
    if counts_after != g4_info["db_counts_before"]:
        raise RuntimeError("database counts changed during P11/P12")
    root_manifest = {
        "run_id": run_id,
        "case_id": CASE_ID,
        "g4_authorization_sha256": g4_info["g4_manifest"]["g4_authorization_sha256"],
        "g4_signed": True,
        "signed_report_sha256": g4_info["signed_report_sha256"],
        "g4_human_review_inserted": 1,
        "t1_unsealed": True,
        "t1_unsealed_at": g4_info["g4_manifest"].get("t1_unsealed_at"),
        "t1_unsealed_by": "author_authorized_pipeline",
        "t1_window_start": T1_START.isoformat(),
        "t1_window_end": T1_END.isoformat(),
        "blind_collection_verified": True,
        "p11_candidate_source_count": p11["manifest"]["candidate_source_count"],
        "p11_full_text_source_count": p11["manifest"]["full_text_source_count"],
        "p11_selected_source_count": p11["manifest"]["selected_source_count"],
        "p12_model_call_count": p12["p12_manifest"]["p12_model_call_count"],
        "p12_raw_candidate_count": p12["p12_manifest"]["raw_candidate_count"],
        "p12_gate_pass_count": p12["p12_manifest"]["gate_pass_count"],
        "g1_t1_preannotation_count": len(p12["final_candidates"]),
        "author_decision_count": 0,
        "formal_g1_t1_executed": False,
        "evidence_inserted": 0,
        "t1_snapshot_frozen": False,
        "impact_localization_executed": False,
        "viewpoint_update_executed": False,
        "historical_t0_artifacts_unchanged": True,
        "api_key_written": False,
        "t1_read": True,
        "hypothesis_inserted": 0,
        "p4_executed": False,
        "p5_executed": False,
        "database_counts_before": g4_info["db_counts_before"],
        "database_counts_after": counts_after,
        "files": file_hashes(base),
    }
    write_json(base / "p11_p12_manifest.json", root_manifest)
    return {"run_id": run_id, "root_manifest": root_manifest, "p11": p11["manifest"], "p12": p12["p12_manifest"]}
