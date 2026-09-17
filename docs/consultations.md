# Asking an agent a question

Everything else in this control plane exists to reach an approved effect. A task is specified,
planned, implemented, reviewed and dispatched, and a person stands at each gate.

"Hey lead, I have this repo with terraform — what are your inputs if I deployed this?" is not
that. It changes nothing. The answer is worth having, and there is nothing to approve, because
nothing is going to happen.

A consultation is that question and the answer it gets. It is deliberately not a task.

## Why it has no approval gate

Because it reaches no effect. An approval on a question would be a gate with nothing behind
it, and gates with nothing behind them are how people learn to click through gates.

That argument is only sound while a consultation **cannot become** an effect. If a consultation
were a task with two extra states, someone could later add an edge from `answered` into the
workflow and the argument would quietly stop being true — with no test failing, because nothing
would have said out loud that the edge must not exist.

So consultations live in their own table with their own small lifecycle and no route into the
task state machine at all. `orch/consultations.py` imports no engine, calls nothing that
advances or approves anything, and creates no task, `ToolRequest`, `ExternalActionReceipt` or
approval. Two tests hold that:

- `test_a_consultation_leaves_no_task_no_approval_and_no_request_behind` asks a question, has
  it answered and closes it, then checks that every evidence and ownership table is still
  empty.
- `test_nothing_in_this_module_reaches_the_state_machine` parses the module and fails on an
  import of the engine or a call to `advance`, `approve`, `create_task` or `claim`.

The second one is the one that matters in a year. The first can be satisfied by a module that
is one edit away from not satisfying it.

## An answer is an opinion

Every report of a consultation carries `answer_is_untrusted: true` and `approves_nothing: true`,
and the window prints the same thing in words under each answer. This is not decoration. An
agent's answer is model output; reading one and then deciding to act is a person's job, and
acting is a task, which has its own gates.

## The lifecycle

```
asked ──take──▶ answering ──answer──▶ answered ──close──▶ closed
  │                  │
  └────withdraw──────┴──▶ withdrawn
```

`asked` — put to a role, waiting for an agent to pick it up.
`answering` — an agent said it is writing, so two agents of one role do not both answer.
`answered` — an opinion is recorded, attributed and timestamped.
`withdrawn` — the person took the question back. A withdrawn question is never answered.
`closed` — the person has read the answer. This exists so the window can stop asking them to;
a signal that is always on is a signal nobody reads.

`answering` is not a lease in the [ownership registry](agent-sessions.md) and does not count
against the [admission bound](worker-admission.md). Borrowing that machinery would be the first
step towards a consultation becoming a task. An agent that takes a question and vanishes holds
it for fifteen minutes, after which it is offered again.

## Who may ask, and who may answer

A question goes to a **role**, never to a container. `human` is refused: it is a role in the
workflow and never an agent, the same rule that stops an agent [claiming an approval
pause](agent-queue.md).

An agent is offered only questions put to a role it was enrolled for, and may answer only
those. The roles come from the service's own configuration rather than from the request, which
is the same gate claiming work passes. A junior container cannot answer as the lead by asking
nicely, and it is never told the lead was asked.

## The agent takes a question after it has thought about it

`agent-consultations` lists, `agent-consult-take` says "I am writing this one", and
`agent-consult-answer` records it. The loop in `orch/agent_runner.py` calls them in an order
that looks backwards and is not: it asks its decider first, and takes the question only once it
has something to say.

Taking first would mean a decider with nothing to say leaves a question held for a quarter of
an hour. Two agents that both think about one question waste a little model time; only one of
them can record an answer, which is what taking is for.

A decider that returns `None`, or raises `Rejected`, leaves the question where it is. The
default `Decider.advise` returns `None`, so a decider written before consultations existed
keeps working and simply never answers one.

Questions come before work in each round. A task in a queue is waiting; a person who asked a
question is waiting at their desk.

## Credentials in the text

A specification **refuses** text that looks like a secret. A consultation redacts it instead,
and says in the record that it did (`redacted: true`).

That is a deliberate difference. The first real use of this is "look at my terraform and tell
me what you think", and terraform is full of lines like `password = var.db_password`. Refusing
would make the feature useless for the thing it was asked for, and refusing an *answer* throws
away work already done. Redaction keeps the answer and keeps the secret out of the store; the
disclosure is there because silently altering text a person will read as quoted is its own
problem.

## How long an answer may be

A question is up to 4000 characters and an answer up to 5000. The answer bound is not a round
number chosen for tidiness: an answer travels in one authenticated request, that envelope is
bounded at 16 KiB of canonical JSON, and a character outside ASCII costs three bytes there. So
the limit is the length that can *always* be sent rather than the length that usually can.

The alternative was a larger bound that fails on some alphabets, and an agent whose answer was
refused for being long simply would not answer — with nobody told why. This is an opinion, not
a report.

## Referring to a task

A question may name a task as `about`, because "the junior opened this — what do you think?" is
a real question. The reference is read-only context and is checked to exist when the question is
asked. The answer still cannot approve, advance or change the task it discusses, and
`test_a_question_may_name_a_task_as_read_only_context` checks the task's revision, evidence and
ownership are all exactly as they were.

## Using it

From the window: the **Ask an agent** panel. Pick who you are asking, type the question, and
tick *About the selected task* to attach the task you have open. Answers appear in the same
panel; **Mark read** closes one, **Withdraw** takes back a question nobody has answered yet.
The **Ask** button on each agent in the roster points the form at that agent's role.

A question that is finished with — read, or taken back — leaves the panel and stays in the
store, where `consult-list` still shows it. A panel that only grows is one people stop reading.

From the command line:

```sh
python -m orch consult-ask --data .runtime/phase2 --role lead_planner --principal you \
    --question "I have a repo with terraform here. What are your inputs if I deployed this?"
python -m orch consult-list --data .runtime/phase2
python -m orch consult-close --data .runtime/phase2 --consultation <id> --principal you
```

`--from FILE` reads a long question from a file instead. An agent answers through the service,
never by opening the store:

```sh
python -m orch agent-consultations --agent-endpoint 127.0.0.1:PORT --agent-secret /run/secret
python -m orch agent-consult-answer --agent-endpoint 127.0.0.1:PORT --agent-secret /run/secret \
    --consultation <id> --answer "Three inputs, and nothing applies from a laptop."
```

## What this is not

It is not a chat. There is one question and one answer; a follow-up is another question.

It is not a way to instruct an agent. Nothing an agent says here can approve, advance or change
a task, and nothing a person says here creates one. When the answer makes you want something
done, [open a task](draft-spec.md) — and that goes through the gates.

It is not evidence. A consultation is current state in its own table, like
[presence](agent-sessions.md) and unlike the append-only record of what was done to a task. It
carries no append-only trigger, and closing or withdrawing one rewrites its row.
