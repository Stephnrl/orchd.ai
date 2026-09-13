# Conservative artifact cleanup

The artifact garbage collector supports an explicit preview mode:

```sh
python -m orch gc --data .runtime/managed --dry-run
```

Dry-run takes the dispatcher lock, scans only the artifact directory, and reports the
exact eligible orphan names and byte sizes. It does not delete files, write workflow
records, invoke a provider or require Docker. A normal `gc` remains the explicit
deletion action and rechecks references and age under a fresh lock.

Both modes require an existing workflow database. Missing storage is rejected before
opening the Store, preventing a typo from creating an empty database. The report
includes `dry_run`, `candidates`, `removed` and `grace_seconds`; `removed` is always
empty in preview mode. Names must match the existing hash or staging-file grammar and
be unreferenced and at least one day old by default. The minimum configurable grace
period remains one hour.
Both modes open a current-version database read-only and never migrate it. Legacy
databases must be upgraded through the normal application path first. Path checks run
before opening storage, including checks for links and junctions in its ancestors.

GC now completes its scan before deleting any candidates, so a path or scan failure
found later in the directory leaves all candidates intact. Candidate order is stable
by filename and the scan uses one age cutoff. Invalid, nonfinite or boolean grace
values are rejected. Paths are rechecked immediately before each deletion.
Deletion itself is not transactional: an unlink failure or an external filesystem
change during deletion can still leave a partially completed cleanup. The dispatcher
lock coordinates application writers, not arbitrary host processes.

Links, junctions, directories and unexpected names are handled by the existing
conservative scanner; no recursive traversal or implicit cleanup is added. Candidate
names are operator-visible because they identify local files, but contents and workflow
artifact payloads are never read into the report. A dry-run is a point-in-time preview
and can become stale before deletion; run the deletion command only after reviewing it.

On September 13, 2026, the full local suite passed 128 tests with exactly five
expected Docker skips. New tests cover preview/deletion agreement, no deletion during
preview, age and path guards, missing-store rejection and CLI behavior. Contract
validation, Python compilation and whitespace checks passed. Repository CI adds
Windows/Linux offline coverage and the separate Linux Docker gate.
