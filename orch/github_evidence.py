"""Task-owned local artifact byte checks; no semantic approval or execution authority."""
import hashlib
import json

from jsonschema import Draft202012Validator

from .contracts import Rejected, SCHEMA, canonical, digest
from .github_approval import _validate_evidence

ARTIFACT = Draft202012Validator({'$defs': SCHEMA['$defs'], '$ref': '#/$defs/ArtifactRef'})
MAX_BYTES = 1024 * 1024


def check_evidence(store, evidence):
    _validate_evidence(evidence)
    directory = store.root / 'artifacts'
    if directory.is_symlink() or not directory.is_dir():
        raise Rejected("Invalid artifact directory")
    store.db.execute('BEGIN')
    try:
        if not store.db.execute('SELECT 1 FROM tasks WHERE id=?', (evidence['task_id'],)).fetchone():
            raise Rejected("Unknown evidence task")
        artifacts = {}
        for role in ('patch', 'test', 'review', 'policy'):
            sha = evidence[role + '_sha256']
            row = store.db.execute("SELECT id, CASE WHEN length(CAST(payload AS BLOB))<=8192 THEN payload END AS payload FROM artifacts WHERE task_id=? AND json_extract(payload,'$.sha256')=? ORDER BY id LIMIT 1", (evidence['task_id'], sha)).fetchone()
            if row is None:
                raise Rejected("Evidence artifact is not owned by the task")
            try:
                item = json.loads(row['payload'])
                if (not ARTIFACT.is_valid(item) or item['artifact_id'] != row['id']
                        or item['sha256'] != sha or canonical(item).decode() != row['payload']):
                    raise Rejected("Invalid artifact metadata")
                if type(item['size_bytes']) is not int or not 0 <= item['size_bytes'] <= MAX_BYTES:
                    raise Rejected("Evidence artifact exceeds budget")
            except (ValueError, TypeError, KeyError, RecursionError) as exc:
                raise Rejected("Invalid evidence artifact metadata") from exc
            path = directory / sha
            if path.is_symlink() or not path.is_file():
                raise Rejected("Evidence artifact must be a regular file")
            with path.open('rb') as stream:
                raw = stream.read(MAX_BYTES + 1)
            if len(raw) != item['size_bytes'] or hashlib.sha256(raw).hexdigest() != sha:
                raise Rejected("Evidence artifact byte mismatch")
            artifacts[role] = {"artifact_id": item['artifact_id'], "sha256": sha, "size_bytes": len(raw)}
        binding = {"schema_version": "1.0.0", "task_id": evidence['task_id'],
                   "evidence_sha256": digest(evidence), "artifacts": artifacts}
        result = {"kind": "GitHubArtifactEvidenceAssessment", "binding": binding, "sha256": digest(binding),
                  "status": "bytes_match", "artifact_bytes_verified": True, "evidence_verified": False,
                  "live_authorized": False, "retry_allowed": False}
        store.db.execute('COMMIT')
        return result
    except BaseException:
        store.db.execute('ROLLBACK')
        raise
