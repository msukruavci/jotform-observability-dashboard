from __future__ import annotations

from collections import Counter, defaultdict
import json
import logging
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

# Search candidates for templates_dataset.json
CANDIDATE_PATHS = [
    Path("/data/mcp/mcp_server/assets/templates_dataset.json"),
    Path(__file__).resolve().parents[3] / "jotform-workflow-mcp" / "mcp_server" / "assets" / "templates_dataset.json",
    Path(__file__).resolve().parents[2] / "static" / "assets" / "templates_dataset.json",
    Path(__file__).resolve().parents[1] / "assets" / "templates_dataset.json",
]

SIMILARITY_CANDIDATE_PATHS = [
    Path("/data/mcp/mcp_server/assets/templates_similarity_analysis.json"),
    Path(__file__).resolve().parents[3] / "jotform-workflow-mcp" / "mcp_server" / "assets" / "templates_similarity_analysis.json",
    Path(__file__).resolve().parents[2] / "static" / "assets" / "templates_similarity_analysis.json",
    Path(__file__).resolve().parents[1] / "assets" / "templates_similarity_analysis.json",
]

_CACHED_DATASET: list[dict[str, Any]] | None = None
_CACHED_DATASET_MTIME: float = 0.0
_CACHED_SIMILARITY_ANALYSIS: dict[str, Any] | None = None
_CACHED_SIMILARITY_MTIME: float = 0.0


def get_dataset_path() -> Path | None:
    for path in CANDIDATE_PATHS:
        if path.exists() and path.is_file():
            return path
    return None


def load_templates_dataset() -> list[dict[str, Any]]:
    global _CACHED_DATASET, _CACHED_DATASET_MTIME
    path = get_dataset_path()
    if not path:
        LOGGER.warning("templates_dataset.json not found in candidate paths: %s", CANDIDATE_PATHS)
        return []

    try:
        mtime = path.stat().st_mtime
        if _CACHED_DATASET is not None and mtime == _CACHED_DATASET_MTIME:
            return _CACHED_DATASET

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                _CACHED_DATASET = data
                _CACHED_DATASET_MTIME = mtime
                return _CACHED_DATASET
    except Exception as e:
        LOGGER.error("Failed to load templates dataset: %s", e)
    return []


def get_templates_overview_metrics() -> dict[str, Any]:
    dataset = load_templates_dataset()
    if not dataset:
        return {
            "total_templates": 0,
            "total_categories": 0,
            "avg_complexity": 0.0,
            "avg_uniqueness": 0.0,
            "total_clones": 0,
            "avg_elements": 0.0,
            "categories_list": [],
        }

    total = len(dataset)
    categories = sorted({item.get("category") or "General Management" for item in dataset})
    total_clones = sum(int(item.get("clone_count") or 0) for item in dataset)
    complexities = [float(item.get("complexity_score") or 0) for item in dataset]
    uniquenesses = [float(item.get("uniqueness_score") or 0) for item in dataset]
    elements = [int(item.get("elements_count") or len(item.get("elements") or [])) for item in dataset]

    return {
        "total_templates": total,
        "total_categories": len(categories),
        "avg_complexity": round(sum(complexities) / total, 1) if total else 0.0,
        "avg_uniqueness": round(sum(uniquenesses) / total, 2) if total else 0.0,
        "total_clones": total_clones,
        "avg_elements": round(sum(elements) / total, 1) if total else 0.0,
        "categories_list": categories,
    }


def get_template_charts_data() -> dict[str, Any]:
    dataset = load_templates_dataset()
    if not dataset:
        return {
            "categories": [],
            "scatter": [],
            "top_cloned": [],
            "step_types": [],
        }

    # 1. Categories Distribution
    cat_counts: dict[str, int] = Counter()
    cat_clones: dict[str, int] = defaultdict(int)
    cat_complexities: dict[str, list[float]] = defaultdict(list)

    # 2. Step Types
    step_type_counts: dict[str, int] = Counter()

    # 3. Scatter (Complexity vs Uniqueness)
    scatter_data = []

    for item in dataset:
        cat = item.get("category") or "General Management"
        clones = int(item.get("clone_count") or 0)
        c_score = float(item.get("complexity_score") or 0)
        u_score = float(item.get("uniqueness_score") or 0)

        cat_counts[cat] += 1
        cat_clones[cat] += clones
        cat_complexities[cat].append(c_score)

        scatter_data.append({
            "id": str(item.get("id")),
            "title": item.get("title", ""),
            "category": cat,
            "complexity": c_score,
            "uniqueness": u_score,
            "clones": clones,
            "elements": int(item.get("elements_count") or 0),
        })

        for stype, count in (item.get("step_counts") or {}).items():
            clean_type = stype.replace("workflow_", "").replace("_", " ").title()
            step_type_counts[clean_type] += int(count)

    categories_chart = [
        {
            "name": cat,
            "value": count,
            "clones": cat_clones[cat],
            "avg_complexity": round(sum(cat_complexities[cat]) / len(cat_complexities[cat]), 1) if cat_complexities[cat] else 0,
        }
        for cat, count in cat_counts.most_common()
    ]

    # Top 10 Cloned Templates
    sorted_by_clones = sorted(dataset, key=lambda x: int(x.get("clone_count") or 0), reverse=True)[:10]
    top_cloned = [
        {
            "id": str(x.get("id")),
            "title": (x.get("title") or "Untitled")[:40] + ("..." if len(x.get("title", "")) > 40 else ""),
            "full_title": x.get("title") or "Untitled",
            "category": x.get("category") or "General Management",
            "clone_count": int(x.get("clone_count") or 0),
            "elements_count": int(x.get("elements_count") or 0),
        }
        for x in sorted_by_clones
    ]

    # Step types top 10
    step_types = [
        {"name": name, "count": count}
        for name, count in step_type_counts.most_common(10)
    ]

    return {
        "categories": categories_chart,
        "scatter": scatter_data,
        "top_cloned": top_cloned,
        "step_types": step_types,
    }


def filter_templates(
    query: str | None = None,
    category: str | None = None,
    sort_by: str = "clones_desc",
    page: int = 1,
    per_page: int = 20,
) -> dict[str, Any]:
    dataset = load_templates_dataset()
    filtered = dataset

    if category:
        filtered = [x for x in filtered if (x.get("category") or "").lower() == category.lower()]

    if query:
        q = query.strip().lower()
        filtered = [
            x for x in filtered
            if q in (x.get("title") or "").lower()
            or q in (x.get("description") or "").lower()
            or q in (x.get("tags") or "").lower()
            or q in (x.get("category") or "").lower()
            or any(q in s.lower() for s in x.get("steps_summary", []))
        ]

    # Sorting
    if sort_by == "clones_desc":
        filtered.sort(key=lambda x: int(x.get("clone_count") or 0), reverse=True)
    elif sort_by == "clones_asc":
        filtered.sort(key=lambda x: int(x.get("clone_count") or 0))
    elif sort_by == "complexity_desc":
        filtered.sort(key=lambda x: float(x.get("complexity_score") or 0), reverse=True)
    elif sort_by == "complexity_asc":
        filtered.sort(key=lambda x: float(x.get("complexity_score") or 0))
    elif sort_by == "uniqueness_desc":
        filtered.sort(key=lambda x: float(x.get("uniqueness_score") or 0), reverse=True)
    elif sort_by == "uniqueness_asc":
        filtered.sort(key=lambda x: float(x.get("uniqueness_score") or 0))
    elif sort_by == "elements_desc":
        filtered.sort(key=lambda x: int(x.get("elements_count") or 0), reverse=True)
    elif sort_by == "title_asc":
        filtered.sort(key=lambda x: (x.get("title") or "").lower())

    total_items = len(filtered)
    total_pages = max(1, (total_items + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))

    start = (page - 1) * per_page
    end = start + per_page
    page_items = filtered[start:end]

    return {
        "items": page_items,
        "total_items": total_items,
        "page": page,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_prev": page > 1,
        "next_page": page + 1 if page < total_pages else None,
        "prev_page": page - 1 if page > 1 else None,
    }


def get_template_by_id(template_id: str) -> dict[str, Any] | None:
    dataset = load_templates_dataset()
    for item in dataset:
        if str(item.get("id")) == str(template_id):
            return item
    return None


SESSION_LOG_CANDIDATES = [
    Path("/data/mcp/mcp_server/logs/sessions"),
    Path(__file__).resolve().parents[3] / "jotform-workflow-mcp" / "mcp_server" / "logs" / "sessions",
    Path(__file__).resolve().parents[2] / "logs" / "sessions",
]


def get_session_logs_dir() -> Path | None:
    for path in SESSION_LOG_CANDIDATES:
        if path.exists() and path.is_dir():
            return path
    return None


def extract_templates_from_result(result_data: Any, dataset_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    if not result_data:
        return []

    # 1. Direct structured_content
    if isinstance(result_data, dict):
        if "structured_content" in result_data and isinstance(result_data["structured_content"], dict):
            tmpls = result_data["structured_content"].get("templates")
            if isinstance(tmpls, list) and tmpls:
                return _enrich_templates(tmpls, dataset_map)

        # 2. Content array
        if "content" in result_data and isinstance(result_data["content"], list):
            for c in result_data["content"]:
                if isinstance(c, dict) and c.get("type") == "text":
                    try:
                        parsed = json.loads(c.get("text", ""))
                        if isinstance(parsed, dict) and "templates" in parsed and parsed["templates"]:
                            return _enrich_templates(parsed["templates"], dataset_map)
                    except Exception:
                        pass

        # 3. Preview string
        if "preview" in result_data and isinstance(result_data["preview"], str):
            prev_str = result_data["preview"]
            try:
                parsed_prev = json.loads(prev_str)
                for c in parsed_prev.get("content", []):
                    if isinstance(c, dict) and c.get("type") == "text":
                        try:
                            p = json.loads(c.get("text", ""))
                            if isinstance(p, dict) and "templates" in p and p["templates"]:
                                return _enrich_templates(p["templates"], dataset_map)
                        except Exception:
                            pass
            except Exception:
                pass

    # 4. Fallback: regex for all template IDs in raw result
    raw_str = str(result_data)
    import re
    found_ids = list(dict.fromkeys(re.findall(r'id[\\\"\'\s:]+(\d{10,20})', raw_str)))

    results = []
    for tid in found_ids:
        meta = dataset_map.get(tid, {})
        results.append({
            "id": tid,
            "title": meta.get("title") or f"Template #{tid}",
            "description": meta.get("description", ""),
            "tags": meta.get("tags", ""),
            "clone_count": int(meta.get("clone_count") or 0),
            "steps_summary": meta.get("steps_summary", []),
            "score": 0.0,
            "slug": meta.get("slug", ""),
            "category": meta.get("category") or "General Management",
            "jotform_url": f"https://www.jotform.com/workflow-templates/{meta.get('slug') or tid}",
        })
    return results


def _enrich_templates(templates: list[dict[str, Any]], dataset_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    enriched = []
    for t in templates:
        tid = str(t.get("id", ""))
        meta = dataset_map.get(tid, {})
        slug = t.get("slug") or meta.get("slug", "")
        item = {
            "id": tid,
            "title": t.get("title") or meta.get("title") or f"Template #{tid}",
            "description": t.get("description") or meta.get("description", ""),
            "tags": t.get("tags") or meta.get("tags", ""),
            "clone_count": int(t.get("clone_count") or meta.get("clone_count") or 0),
            "steps_summary": t.get("steps_summary") or meta.get("steps_summary", []),
            "score": round(float(t.get("score") or 0.0), 4),
            "slug": slug,
            "category": meta.get("category") or "General Management",
            "jotform_url": f"https://www.jotform.com/workflow-templates/{slug or tid}",
        }
        enriched.append(item)
    return enriched


def get_mcp_template_invocations(
    query_filter: str | None = None,
    tool_filter: str | None = None,
    page: int = 1,
    per_page: int = 15,
) -> dict[str, Any]:
    dataset = load_templates_dataset()
    dataset_map = {str(item.get("id")): item for item in dataset}
    session_dir = get_session_logs_dir()

    invocations: list[dict[str, Any]] = []

    # Map session IDs to Django session UUIDs if they exist
    from apps.traces.models import Session
    db_sessions = {s.external_session_id: str(s.id) for s in Session.objects.all()}

    if session_dir and session_dir.exists():
        import os
        for sfile in sorted(session_dir.glob("*.jsonl"), key=os.path.getmtime, reverse=True):
            session_raw = sfile.stem
            session_id = session_raw.split("_", 1)[1] if "_" in session_raw else session_raw
            django_session_id = db_sessions.get(session_id)

            try:
                with open(sfile, encoding="utf-8") as f:
                    lines = [json.loads(l) for l in f if l.strip()]
            except Exception:
                continue

            started_map = {
                l.get("request_id"): l
                for l in lines
                if l.get("event_type") == "mcp.tool_call.started"
            }

            for l in lines:
                if l.get("event_type") == "mcp.tool_call.completed":
                    req_id = l.get("request_id")
                    st = started_map.get(req_id, {})
                    tool_name = st.get("tool") or st.get("tool_name") or l.get("tool")
                    if tool_name not in ("search_workflow_templates", "get_workflow_template"):
                        continue

                    args = st.get("arguments") or {}
                    res_raw = l.get("result") or {}
                    timestamp = l.get("timestamp") or st.get("timestamp") or ""
                    duration_ms = l.get("duration_ms", 0)

                    search_query = args.get("query", "")
                    target_template_id = args.get("template_id", "")

                    templates_retrieved = []
                    if tool_name == "search_workflow_templates":
                        templates_retrieved = extract_templates_from_result(res_raw, dataset_map)
                    elif tool_name == "get_workflow_template" and target_template_id:
                        meta = dataset_map.get(str(target_template_id), {})
                        templates_retrieved = [{
                            "id": str(target_template_id),
                            "title": meta.get("title") or f"Template #{target_template_id}",
                            "description": meta.get("description", ""),
                            "tags": meta.get("tags", ""),
                            "clone_count": int(meta.get("clone_count") or 0),
                            "steps_summary": meta.get("steps_summary", []),
                            "score": 1.0,
                            "slug": meta.get("slug", ""),
                            "category": meta.get("category") or "General Management",
                            "jotform_url": f"https://www.jotform.com/workflow-templates/{meta.get('slug') or target_template_id}",
                        }]

                    invocations.append({
                        "request_id": req_id,
                        "session_id": session_id,
                        "django_session_id": django_session_id,
                        "timestamp": timestamp,
                        "tool_name": tool_name,
                        "is_search": tool_name == "search_workflow_templates",
                        "is_inspection": tool_name == "get_workflow_template",
                        "query": search_query,
                        "top_k": args.get("top_k", len(templates_retrieved)),
                        "template_id": target_template_id,
                        "templates": templates_retrieved,
                        "template_count": len(templates_retrieved),
                        "duration_ms": round(float(duration_ms), 1) if duration_ms else None,
                        "is_error": bool(l.get("is_error", False)),
                    })

    # Sort by timestamp descending
    invocations.sort(key=lambda x: x.get("timestamp") or "", reverse=True)

    # Calculate overall stats
    total_searches = sum(1 for inv in invocations if inv["is_search"])
    total_inspections = sum(1 for inv in invocations if inv["is_inspection"])
    all_retrieved_ids = [t["id"] for inv in invocations for t in inv["templates"]]
    unique_retrieved_count = len(set(all_retrieved_ids))

    all_scores = [t["score"] for inv in invocations if inv["is_search"] for t in inv["templates"] if t.get("score")]
    avg_score = round(sum(all_scores) / len(all_scores), 3) if all_scores else 0.0

    # Top retrieved templates in our MCP sessions
    template_counts = Counter(all_retrieved_ids)
    top_retrieved_in_sessions = []
    for tid, count in template_counts.most_common(8):
        meta = dataset_map.get(tid, {})
        top_retrieved_in_sessions.append({
            "id": tid,
            "title": meta.get("title") or f"Template #{tid}",
            "category": meta.get("category") or "General Management",
            "count": count,
            "slug": meta.get("slug", ""),
            "jotform_url": f"https://www.jotform.com/workflow-templates/{meta.get('slug') or tid}",
        })

    # Filtering
    filtered = invocations
    if tool_filter:
        if tool_filter == "search":
            filtered = [x for x in filtered if x["is_search"]]
        elif tool_filter == "inspection":
            filtered = [x for x in filtered if x["is_inspection"]]

    if query_filter:
        q = query_filter.strip().lower()
        filtered = [
            x for x in filtered
            if q in x["session_id"].lower()
            or q in (x["query"] or "").lower()
            or q in (x["template_id"] or "").lower()
            or any(
                q in t["title"].lower() or q in t["id"].lower() or q in t["description"].lower()
                for t in x["templates"]
            )
        ]

    total_items = len(filtered)
    total_pages = max(1, (total_items + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))

    start = (page - 1) * per_page
    end = start + per_page
    page_items = filtered[start:end]

    return {
        "invocations": page_items,
        "total_items": total_items,
        "page": page,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_prev": page > 1,
        "next_page": page + 1 if page < total_pages else None,
        "prev_page": page - 1 if page > 1 else None,
        "stats": {
            "total_invocations": len(invocations),
            "total_searches": total_searches,
            "total_inspections": total_inspections,
            "unique_retrieved_count": unique_retrieved_count,
            "avg_similarity_score": avg_score,
            "top_retrieved": top_retrieved_in_sessions,
        },
    }


def get_similarity_analysis_path() -> Path | None:
    for path in SIMILARITY_CANDIDATE_PATHS:
        if path.exists() and path.is_file():
            return path
    return None


def load_similarity_analysis() -> dict[str, Any]:
    global _CACHED_SIMILARITY_ANALYSIS, _CACHED_SIMILARITY_MTIME
    path = get_similarity_analysis_path()
    if not path:
        LOGGER.warning("templates_similarity_analysis.json not found in candidate paths.")
        return {}

    try:
        mtime = path.stat().st_mtime
        if _CACHED_SIMILARITY_ANALYSIS is not None and mtime == _CACHED_SIMILARITY_MTIME:
            return _CACHED_SIMILARITY_ANALYSIS

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                _CACHED_SIMILARITY_ANALYSIS = data
                _CACHED_SIMILARITY_MTIME = mtime
                return _CACHED_SIMILARITY_ANALYSIS
    except Exception as e:
        LOGGER.error("Failed to load templates similarity analysis: %s", e)
    return {}


def get_cross_similarity_analysis_data(
    selected_bucket: str | None = None,
    page: int = 1,
    per_page: int = 15,
) -> dict[str, Any]:
    data = load_similarity_analysis()
    if not data:
        return {
            "total_templates": 0,
            "total_pairs": 0,
            "min_similarity": 0.0,
            "max_similarity": 0.0,
            "mean_similarity": 0.0,
            "median_similarity": 0.0,
            "std_dev": 0.0,
            "buckets": [],
            "most_similar_pairs": [],
            "most_distinct_pairs": [],
            "selected_bucket": "",
            "pairs_page": {
                "items": [],
                "total_items": 0,
                "page": 1,
                "total_pages": 1,
                "has_next": False,
                "has_prev": False,
            },
        }

    buckets = data.get("buckets", [])
    bucket_pairs_map = data.get("bucket_pairs", {})

    # Default to the most interesting non-empty bucket if none selected (e.g. highest similarity bucket with items)
    if not selected_bucket:
        # Find highest bucket with items e.g. 0.95-1.00 or 0.90-0.95
        non_empty = [b["key"] for b in reversed(buckets) if b.get("count", 0) > 0]
        selected_bucket = non_empty[0] if non_empty else (buckets[0]["key"] if buckets else "")

    raw_pairs = bucket_pairs_map.get(selected_bucket, [])
    total_items = len(raw_pairs)
    total_pages = max(1, (total_items + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))

    start = (page - 1) * per_page
    end = start + per_page
    page_items = raw_pairs[start:end]

    return {
        "total_templates": data.get("total_templates", 0),
        "total_pairs": data.get("total_pairs", 0),
        "min_similarity": data.get("min_similarity", 0.0),
        "max_similarity": data.get("max_similarity", 0.0),
        "mean_similarity": data.get("mean_similarity", 0.0),
        "median_similarity": data.get("median_similarity", 0.0),
        "std_dev": data.get("std_dev", 0.0),
        "buckets": buckets,
        "most_similar_pairs": data.get("most_similar_pairs", [])[:8],
        "most_distinct_pairs": data.get("most_distinct_pairs", [])[:8],
        "selected_bucket": selected_bucket,
        "pairs_page": {
            "items": page_items,
            "total_items": total_items,
            "page": page,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_prev": page > 1,
            "next_page": page + 1 if page < total_pages else None,
            "prev_page": page - 1 if page > 1 else None,
        },
    }

