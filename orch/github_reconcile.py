"""Offline uncertain-result classification; no retry or success authority."""
from urllib.parse import urlencode

from .contracts import Rejected, canonical, digest
from .github_preview import prepare_pull_request


def positive_id(value):
    return type(value) is int and 0 < value <= 9007199254740991


def reconcile_pull_request(intent, allowed_repository, repository_id, observations):
    preview = prepare_pull_request(intent, allowed_repository)
    if not positive_id(repository_id):
        raise Rejected("An explicit repository ID is required")
    if not isinstance(observations, dict) or set(observations) != {"preview_sha256", "pages"}:
        raise Rejected("Invalid reconciliation envelope")
    try:
        if len(canonical(observations)) > 65536:
            raise Rejected("Reconciliation observations exceed budget")
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Rejected("Invalid reconciliation observations") from exc
    pages = observations["pages"]
    if not isinstance(pages, list) or len(pages) > 10:
        raise Rejected("Invalid page collection")
    reasons, candidates = [], []
    if observations["preview_sha256"] != preview["sha256"]:
        reasons.append("preview_mismatch")
    root = "https://api.github.com/repos/" + allowed_repository
    for index, page in enumerate(pages, 1):
        if (not isinstance(page, dict) or set(page) != {"page", "status", "has_next", "body"}
                or type(page["page"]) is not int or type(page["status"]) is not int or type(page["has_next"]) is not bool):
            raise Rejected("Invalid page envelope")
        if page["page"] != index or page["has_next"] != (index < len(pages)):
            reasons.append("pagination_incomplete_or_inconsistent")
        if page["status"] != 200:
            reasons.append("page_unavailable")
            continue
        if not isinstance(page["body"], list) or len(page["body"]) > 100:
            reasons.append("page_malformed")
            continue
        for pr in page["body"]:
            if not isinstance(pr, dict):
                reasons.append("candidate_malformed")
                continue
            number = pr.get("number")
            matched = (positive_id(pr.get("id")) and positive_id(number)
                       and pr.get("url") == root + f"/pulls/{number}"
                       and pr.get("html_url") == f"https://github.com/{allowed_repository}/pull/{number}"
                       and pr.get("title") == intent["title"] and pr.get("body") == intent["body"]
                       and pr.get("state") == "open" and pr.get("draft") is True
                       and "merged_at" in pr and pr["merged_at"] is None)
            for kind in ("base", "head"):
                side = pr.get(kind)
                if not isinstance(side, dict) or not isinstance(side.get("repo"), dict):
                    matched = False
                    continue
                repo = side["repo"]
                matched = (matched and side.get("ref") == intent[kind]
                           and side.get("sha") == intent["expected_" + kind + "_sha"]
                           and positive_id(repo.get("id")) and repo["id"] == repository_id
                           and repo.get("full_name") == allowed_repository and repo.get("url") == root)
            if matched:
                candidates.append({"id": pr["id"], "number": number, "url": pr["html_url"]})
            else:
                reasons.append("candidate_scope_or_state_mismatch")
    if not candidates:
        reasons.append("no_matching_candidate")
    elif len(candidates) > 1:
        reasons.append("multiple_candidates")
    binding = {"schema_version": "1.0.0", "preview_sha256": preview["sha256"],
               "repository_id": repository_id, "observations_sha256": digest(observations)}
    query = urlencode({"state": "all", "head": allowed_repository.split("/")[0] + ":" + intent["head"], "per_page": 100, "page": 1})
    return {"kind": "GitHubReconciliationAssessment", "binding": binding, "sha256": digest(binding),
            "status": "unresolved" if reasons else "candidate_observed", "reasons": sorted(set(reasons)),
            "candidate": None if reasons else candidates[0],
            "read_request": {"method": "GET", "url": root + "/pulls?" + query},
            "retry_allowed": False, "remote_effect_confirmed": False, "live_authorized": False}
