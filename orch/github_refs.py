"""Compare offline GitHub observations; never authenticate them or dispatch requests."""
from .contracts import Rejected, canonical, digest
from .github_preview import prepare_pull_request


def assess_refs(intent, allowed_repository, repository_id, observations):
    preview = prepare_pull_request(intent, allowed_repository)
    if type(repository_id) is not int or not 0 < repository_id <= 9007199254740991:
        raise Rejected("An explicit positive repository ID is required")
    if not isinstance(observations, dict) or set(observations) != {"preview_sha256", "repository", "base", "head"}:
        raise Rejected("Invalid observation envelope")
    try:
        if len(canonical(observations)) > 65536:
            raise Rejected("Observations exceed budget")
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Rejected("Invalid observations") from exc
    root = f"https://api.github.com/repos/{allowed_repository}"
    blockers = []
    if observations["preview_sha256"] != preview["sha256"]:
        blockers.append("preview_mismatch")
    for kind in ("repository", "base", "head"):
        response = observations[kind]
        if not isinstance(response, dict) or set(response) != {"status", "body"} or type(response["status"]) is not int:
            raise Rejected("Invalid response envelope")
        body = response["body"]
        if response["status"] != 200:
            blockers.append(kind + "_unavailable")
            continue
        if not isinstance(body, dict):
            blockers.append(kind + "_malformed")
            continue
        if kind == "repository":
            if (type(body.get("id")) is not int or body["id"] != repository_id
                    or body.get("full_name") != allowed_repository or body.get("url") != root):
                blockers.append("repository_identity_mismatch")
            if body.get("archived") is not False or body.get("disabled") is not False:
                blockers.append("repository_not_active")
        else:
            name, sha = intent[kind], intent["expected_" + kind + "_sha"]
            obj = body.get("object")
            if (body.get("ref") != "refs/heads/" + name or body.get("url") != root + "/git/refs/heads/" + name
                    or not isinstance(obj, dict) or obj.get("type") != "commit" or obj.get("sha") != sha
                    or obj.get("url") != root + "/git/commits/" + sha):
                blockers.append(kind + "_ref_mismatch")
    binding = {"schema_version": "1.0.0", "preview_sha256": preview["sha256"],
               "repository_id": repository_id, "observations_sha256": digest(observations)}
    urls = {"repository": root, "base": root + "/git/ref/heads/" + intent["base"],
            "head": root + "/git/ref/heads/" + intent["head"]}
    return {"kind": "GitHubRefAssessment", "status": "blocked" if blockers else "matches",
            "blockers": blockers, "binding": binding, "sha256": digest(binding),
            "live_authorized": False, "remote_refs_verified": False,
            "read_requests": {kind: {"method": "GET", "url": url} for kind, url in urls.items()}}
