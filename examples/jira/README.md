# Synthetic Jira action examples

All names, IDs and responses here are fictional. `profile.json` is a codec fixture, not
a ready-to-use deployment configuration. No file contains credentials or grants authority.

Each `ACTION-intent.json` pairs with `ACTION-capture.json` and the common profile.
The create example prepares a Story linked to an explicit existing Epic. The remaining
examples cover a status transition, Jira issue link, GitHub PR association and first Epic
attachment. Captures are canonical JSON and bind the exact generated read plan.

```sh
python -m orch jira-action-preview --jira-profile examples/jira/profile.json --intent examples/jira/transition-intent.json --transcript examples/jira/transition-capture.json
python scripts/jira_acceptance.py --destination .runtime/jira-acceptance-new
```

The acceptance command generates all plans, previews, review/preflight data, recovery
captures and backup evidence in a **new** directory. It consumes synthetic reservation
slots in that fixture database without posting anything. Its generated approval timestamps
expire; rerun into another new directory for a current fixture review.

See [the complete contract and deployment boundaries](../../docs/jira-integration.md).
