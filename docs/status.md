# One answer instead of six

The signals were all there and all separate. Answering *"is everything healthy, what are my
agents doing, what is waiting on me"* meant running `doctor`, `storage-usage`, `agent-sessions`,
a journal usage command, `broker-check` and `deployment-check`, and holding the answers in your
head.

```sh
python -m orch status --data STORE
python -m orch status --data STORE --journal github.sqlite --jira-journal jira.sqlite
```

The same report is served at `GET /status` in [the authenticated local API](phase2.md), so the
operator window can show it.

## The line worth reading

```json
{"tasks_active": 2, "waiting_on_you": 1, "agents_holding": 1,
 "work_available": 0, "at_bound": false, "attention": true}
```

`attention` is true when something needs a person: a task waiting on one, the admission bound
reached, or a lease that lapsed. Everything else is the machine working, which is not news.

## Busy and stopped are different questions

A task in `AWAITING_PLAN_APPROVAL`, `AWAITING_ACTION_APPROVAL`, `AWAITING_CLARIFICATION` or
`BLOCKED` is **the machine stopped and you are the reason**. A task being drafted by an agent
holding a live lease is the machine working. Lumping those together would produce a number that
goes up for two opposite reasons.

So `waiting_on_a_person` lists them separately, with the state and when it last changed, and
the rest of the report describes what is running.

## It answers while work is happening

`storage_usage` takes the store-wide lock and asks for quiescence. That is right for
maintenance — it must not describe a moving target. A status view doing the same would refuse
exactly when it was most wanted: while everything is busy.

So this reads without excluding anything. It describes a moment rather than a transaction,
which is what a status view should describe.

## What it reports

| Section | |
| --- | --- |
| `tasks` | Counts by state, how many are active, which are waiting on a person and since when |
| `agents` | Who holds what, and leases that lapsed — evidence a container stopped without releasing |
| `work` | What could be claimed, by role, with the admission bound alongside |
| `artifacts` | Quota accounting, read without excluding the work producing it |
| `journals` | Records and remaining capacity for any journal path given |

A journal that cannot be opened is reported as `unavailable` rather than raised. A status view
that dies because one of the things it describes is broken is the opposite of useful.

## What it is not

It changes nothing, decides nothing and authorizes nothing. It does not say whether a
deployment is ready — that is [`deployment-check`](deployment-readiness.md), which is a
different question with gates and next steps. It does not check integrity — that is `audit`
and `verify-backup`. And `attention: true` is never an error: it means a person is needed, not
that anything is broken, which is why the command exits zero either way.

Rendering this in the operator window is a separate step; the data is here and the endpoint
serves it.
