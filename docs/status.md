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

## The operator window shows it first

The page was a column of cards with evidence behind `<details>`. That is a good reference and
a poor starting point: it assumes you already know the workflow, and it asks you to translate
`AWAITING_PLAN_APPROVAL` in your head to learn whether anything is waiting on you.

**What needs you now** is now the first thing on the page:

```text
1 thing needs you.

  d9f134d1e702   Waiting for you to approve the plan        5m   [Open task]

Being worked on
  a5c3f039310b   project-manager is holding this as project manager
```

Three things changed and nothing else did.

**A state is shown as a sentence.** `AWAITING_CLARIFICATION` reads "Waiting for your answer to
a question", in the panel and in the task list. The identifier is a protocol detail and stays
in the evidence, where it belongs.

**Work stopped on a person is separated from work in progress.** They are different questions
and a single count would rise for two opposite reasons. A task an agent holds appears under
"Being worked on", greyed and dashed, because it is information rather than a request.

**Every row opens the task it names**, so the answer to "what do I look at next" is a button
rather than a search.

The panel adds no authority. It reads `GET /status`, which changes nothing, and every decision
still happens where it always did, behind the same approval with the same revision fence.

## What it is not

It changes nothing, decides nothing and authorizes nothing. It does not say whether a
deployment is ready — that is [`deployment-check`](deployment-readiness.md), which is a
different question with gates and next steps. It does not check integrity — that is `audit`
and `verify-backup`. And `attention: true` is never an error: it means a person is needed, not
that anything is broken, which is why the command exits zero either way.
