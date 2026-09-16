# How many workers may advance tasks at once

[Task ownership](task-ownership.md) made two workers possible: task work holds that task's
own lock, so two processes can advance two different tasks and never the same one. It did
not say how many may do so, and unbounded parallelism is not a neutral default. Every
advancing task can hold a container, a workspace and a provider process, so enough of them
exhaust the host, and work that fails halfway leaves an operation to reconcile rather than
a task that simply never started.

Admission puts a bound on it. At most **four** tasks may be advancing at once in one store.

```text
Too many tasks are being advanced at once (4); wait for one to finish
```

That refusal happens **before** anything is claimed or changed: the task keeps its state,
no claim is written, and the operator can start the same work again when a slot frees.
Refusing to start is the whole point, so there is no queue and no automatic retry.

## What the bound counts

It counts tasks whose worker is **alive** — a recorded claim whose task lock is still held —
and never claims left behind by a crash. A worker that dies therefore frees its slot
immediately, even though its claim stays behind as
[evidence that it was interrupted](task-ownership.md). A worker also never competes with
itself: its own task is excluded, so a long task takes as many steps as it needs once
admitted.

Counting is exact rather than advisory because claims are registered under the store-wide
lock, which is the reason ownership registered them that way. Two workers cannot both see
the last free slot.

## Why four, and what it is not

Four is a fixed application limit, like the journal record caps and the live pilot cap, and
it is chosen to be genuinely parallel while staying modest for one host. It is not a
throughput target, not a scheduler, and not a resource model: nothing here measures memory,
disk or CPU, or decides *which* task should run. Those belong to the conflict and
scheduling milestone that follows this one.

The bound is per store, and it is enforced by every worker that shares that store. It is
not a licence to run workers on two machines: ownership is enforced by advisory file locks
on one host, and that limit is unchanged here.

## Threads are not workers

File locks exclude other processes, not other threads in the same one. A process that tries
to advance a task it is already advancing is refused explicitly:

```text
This worker is already advancing that task
```

One worker per process is the supported arrangement. The engine holds a single SQLite
connection and was never thread-safe; this refusal makes that boundary visible rather than
leaving it to chance.

## Boundaries

Admission decides only whether a task may **start** being advanced. It changes no approval,
revision, receipt or reconciliation rule, and two workers under the bound are subject to
exactly the invariants one worker was. Nothing here starts workers, supervises them,
restarts them, or balances work between them; an operator still starts each one.
