"""Which provider answers for which role, instead of one provider answering for all.

The engine has always taken a single `provider_factory` and used it for every invocation, so
every `AgentInvocation` recorded the same `provider` and `model` whatever role it was for.
That was true while there was one fixture provider, and stops being true the moment a junior
is meant to run on one model and a lead on another.

A roster maps a role to the provider that answers for it. Two properties make it worth having
rather than a convenience:

**Every entry passes the same admission it always did.** A roster is operator configuration,
and configuration must not become a way to introduce a provider the engine would otherwise
refuse. Each named provider is checked against the admitted set exactly as a single one was.

**A model must be one the provider actually offers.** Asking for a model a provider cannot run
is refused rather than recorded, because an `AgentInvocation` naming a model that never
answered is false evidence, and evidence is the thing this control plane is for. That refusal
is the useful part today: a roster asking for a real model on a fixture provider says exactly
what has not been admitted yet.
"""
from .cli_provider import FixtureCliProvider
from .contracts import Rejected
from .provider import MockProvider

# The providers the engine admits, by the name an operator writes in a roster. This is the
# same set the engine has always checked; naming them does not widen it.
ADMITTED = {"in-process": MockProvider, "cli": FixtureCliProvider}
MAX_ROLES = 16


def offered(factory):
    """The model this provider actually runs, which is the only one it may claim to."""
    return getattr(factory, "model_id", "deterministic-v1")


def resolved(name, model=None):
    """One roster entry: a provider that is admitted, and a model it can honestly claim."""
    if name not in ADMITTED:
        raise Rejected("Unknown provider " + repr(name) + "; admitted providers are "
                       + ", ".join(sorted(ADMITTED)))
    factory = ADMITTED[name]
    if model is not None and model != offered(factory):
        # Recording a model that did not answer would make the invocation record a lie, and
        # the record is the product. Refusing says what is missing instead.
        raise Rejected("Provider " + name + " runs " + offered(factory) + ", not " + repr(model)
                       + "; a provider that runs it has not been admitted")
    return factory


class Roster:
    """A provider for every role, with one answering for the roles nobody named."""

    def __init__(self, default="in-process", roles=None, model=None):
        self.default = resolved(default, model)
        self.roles = {}
        for role, entry in (roles or {}).items():
            if not isinstance(role, str) or not role:
                raise Rejected("A roster names roles as strings")
            if len(self.roles) >= MAX_ROLES:
                raise Rejected("A roster names at most %d roles" % MAX_ROLES)
            if isinstance(entry, str):
                entry = {"provider": entry}
            if not isinstance(entry, dict) or set(entry) - {"provider", "model"} or "provider" not in entry:
                raise Rejected("A roster entry is a provider name, or an object of provider and model")
            self.roles[role] = resolved(entry["provider"], entry.get("model"))

    def for_role(self, role):
        """The provider that answers for this role, or the default that answers for the rest."""
        return self.roles.get(role, self.default)

    def report(self):
        """What an operator configured, and what each of those providers actually runs."""
        entries = {role: {"provider": name(factory), "model": offered(factory)}
                   for role, factory in sorted(self.roles.items())}
        return {"kind": "ProviderRoster", "default": {"provider": name(self.default),
                                                      "model": offered(self.default)},
                "roles": entries, "live_authorized": False}


def name(factory):
    for label, admitted in ADMITTED.items():
        if admitted is factory:
            return label
    raise Rejected("That provider is not admitted")


def load(value):
    """A roster from an operator's JSON, with nothing inferred.

        {"default": "in-process", "roles": {"junior": {"provider": "cli"}}}
    """
    if value is None:
        return Roster()
    if not isinstance(value, dict) or set(value) - {"default", "roles", "model"}:
        raise Rejected("A roster is an object of default, roles and an optional model")
    return Roster(value.get("default", "in-process"), value.get("roles"), value.get("model"))
