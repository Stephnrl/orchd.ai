# A provider for each role

The engine took one `provider_factory` and used it for every invocation, so every
`AgentInvocation` recorded the same `provider` and `model` whatever role produced it. That was
true while there was one provider. It stops being true the moment a junior is meant to run on
one model and a lead on another — which is the ordinary shape of a team, not an exotic
requirement.

```json
{"default": "in-process",
 "roles": {"junior": {"provider": "cli"},
           "lead_planner": {"provider": "in-process"}}}
```

```sh
python -m orch provider-roster --providers roster.json
python -m orch demo --providers roster.json --data STORE --trusted-fixture
```

A role nobody names gets the default. The evidence then records what actually answered:

```text
('junior',       'fixture_cli', 'fixture-v1')
('lead_planner', 'mock',        'deterministic-v1')
```

## Two things a roster may not do

**It may not introduce a provider the engine would otherwise refuse.** A roster is operator
configuration, and configuration must never become the way past admission. Every named
provider is checked against the same admitted set a single factory always was.

**It may not claim a model that did not answer.** Asking for a model a provider cannot run is
refused rather than recorded, because an invocation naming a model that never ran is false
evidence — and the evidence is the product.

## The refusal is the useful part today

No real provider is admitted, so the roster you actually want is refused, and the refusal says
precisely what is missing:

```sh
$ python -m orch provider-roster --providers wanted.json
error: Roster rejected: Unknown provider 'github_copilot_cli'; admitted providers are cli, in-process
```

Ask a fixture provider for a real model and it says the other half:

```text
Provider in-process runs deterministic-v1, not 'claude-sonnet-5';
a provider that runs it has not been admitted
```

So the roster can be written now, against the deployment you intend, and checked. It will keep
refusing until [a provider is admitted](provider-adapter.md) — and then the same file expresses
Sonnet for the junior and Opus for the lead without any further change.

## What this is not

It does not admit anything, and it does not choose a model for a provider that offers several.
A provider declares the model it runs; the roster selects the provider. When one executable can
be configured for two models, those are two admitted provider configurations, and a roster
names one of them per role.

It also does not change what a role may *do*. The role gate, the holding requirement and every
approval are exactly as they were; this decides only which provider answers when a role is
invoked.
