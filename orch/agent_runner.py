"""The loop a container runs: the first thing that has ever had to obey the protocol.

Everything until now spoke to the agent API as a scripted sequence — a test or an acceptance
walkthrough that knew which call came next. The rules the protocol states were written and
never followed by anything: wake with no instruction and decide what to do, renew a lease
*while* working, back off when the admission bound is full rather than claiming into a
refusal, release on shutdown instead of leaving a lease to lapse, treat a refusal as final.

This runs that loop. What it deliberately does not contain is any judgement about the work
itself: deciding what a specification should say is the agent's own business and belongs in
whatever drives it. A `Decider` supplies that, and this supplies everything around it, so the
protocol can be proven followable before there is an intelligence to follow it.

Three things it does that a naive loop would not.

**It renews on a schedule it derives from the lease**, not on a fixed timer, and it renews
before working rather than after: work that takes longer than expected must not be the reason
a lease lapses mid-write.

**It releases on shutdown.** A container that is powered off mid-task should hand the task
back immediately rather than leaving it stuck for the rest of a lease nobody is using. That is
what `stop()` is for, and it is why the loop checks for it between every step rather than only
between tasks.

**It distinguishes a refusal from an outage.** A refusal means the answer is no and asking
again will not change it, so the loop moves on to other work. An unreachable service means it
does not know, so it waits and tries again. Treating those alike is how an agent either spins
against a closed gate or gives up on a service that was merely restarting.
"""
import threading
import time

from .contracts import Rejected, Transient

MIN_LEASE, DEFAULT_LEASE = 60, 900
# Renew with a third of the lease still to run: enough slack that one slow call or one
# restarted service does not cost the task, without renewing constantly.
RENEW_FRACTION = 0.66
IDLE_SECONDS = 15.0
BACKOFF_SECONDS = 30.0
OUTAGE_SECONDS = 5.0


class Decider:
    """What the agent thinks. Everything here is a judgement this module refuses to make.

    `consider` receives the draft report for a task the agent is holding and returns one of:

        ("draft", {specification fields})   write the next revision
        ("ask", [questions])                stop and wait for a person
        ("release", None)                   nothing more to do here

    Raising `Rejected` is also an answer: it means this agent cannot do this task, and the
    loop releases it for someone else rather than holding it while failing.
    """

    def consider(self, draft):
        raise NotImplementedError

    def advise(self, question):
        """What this agent thinks about a question. A string, or None to say nothing.

        Separate from `consider` because it is a different kind of answer: `consider` decides
        what happens to a task, and this decides nothing at all. Returning None, or raising
        `Rejected`, leaves the question for another agent rather than recording an empty
        opinion. The default says nothing, so a decider written before consultations existed
        keeps working and simply never answers one.
        """
        return None


class Runner:
    """One container's working life: find work, hold it, do it, hand it back."""

    def __init__(self, client, decider, role, lease=DEFAULT_LEASE, clock=time.monotonic,
                 sleeper=None):
        if lease < MIN_LEASE:
            raise Rejected("A runner's lease is at least %d seconds" % MIN_LEASE)
        self.client, self.decider, self.role, self.lease = client, decider, role, lease
        self.clock = clock
        self._stop = threading.Event()
        # Waiting on the stop event rather than sleeping means a shutdown is noticed the
        # moment it is asked for, not at the end of a backoff nobody is waiting out.
        self.sleeper = sleeper or self._stop.wait
        self.held = None
        self.renewed_at = 0.0
        self.worked = 0
        self.answered = 0

    # Shutdown

    def stop(self):
        """Ask the loop to finish. Checked between every step, not only between tasks."""
        self._stop.set()

    @property
    def stopping(self):
        return self._stop.is_set()

    # The loop

    def run(self, rounds=None):
        """Work until stopped, or for a bounded number of rounds when a caller wants one."""
        completed = 0
        try:
            while not self.stopping and (rounds is None or completed < rounds):
                self.round()
                completed += 1
        finally:
            # However this ends — stopped, exhausted, or thrown out of — the task goes back.
            self.let_go()
        return completed

    def round(self):
        """One pass: answer a question, take work if there is any, do a step of it, or wait."""
        if self.held is None:
            # Questions first. A task in a queue is waiting; a person who asked a question is
            # waiting at their desk, and answering changes nothing, so it cannot be the thing
            # that goes wrong while somebody is watching.
            if self.consult():
                return
            if not self.take():
                return
        if self.stopping:
            return
        self.step()

    def take(self):
        """Claim one available task, or report that there was nothing to take."""
        try:
            offered = self.client.available()
        except Rejected:
            # Not knowing is different from being told no. Wait and ask again.
            self.wait(OUTAGE_SECONDS)
            return False
        if offered["at_bound"]:
            # The control plane is already advancing as much as it allows. Claiming would be
            # refused, so waiting is the only thing that is not noise.
            self.wait(BACKOFF_SECONDS)
            return False
        for row in offered["available"]:
            if row["role"] != self.role:
                continue
            try:
                self.client.claim(row["task_id"], self.role, self.lease,
                                  expected_revision=row["revision"])
            except Transient:
                # Somebody took it between the listing and the claim, or the bound filled.
                # It may be free again shortly, but not this instant: try the next one.
                continue
            except Rejected:
                # A refusal that will not change: this task is not this agent's to take.
                continue
            self.held = row["task_id"]
            self.renewed_at = self.clock()
            return True
        self.wait(IDLE_SECONDS)
        return False

    def consult(self):
        """Answer one question put to this role, or report that there was none to answer.

        Nothing here claims a task, renews a lease or releases one, because a consultation is
        not a task: it has no owner in the registry, it counts against no admission bound,
        and answering it advances nothing. The loop is the same shape as taking work and that
        is the whole resemblance.
        """
        try:
            offered = self.client.consultations()
        except Rejected:
            # Not knowing is different from being told no, and a service that is down has no
            # work to offer either. Wait, and let the next round ask again.
            self.wait(OUTAGE_SECONDS)
            return True
        for row in offered["consultations"]:
            if row["role"] != self.role:
                continue
            try:
                reply = self.decider.advise(row)
            except Rejected:
                # This agent cannot answer this one. Another may; leave it where it is.
                continue
            if not reply:
                continue
            try:
                # Taking *after* thinking rather than before. Taking first would mean a
                # decider with nothing to say leaves the question held for a quarter of an
                # hour. Two agents that both think about one question waste a little work;
                # only one of them can record an answer, which is what taking is for.
                self.client.consult_take(row["consultation_id"])
                self.client.consult_answer(row["consultation_id"], reply)
            except Rejected:
                continue
            self.answered += 1
            return True
        return False

    def step(self):
        """Renew if the lease is running down, then do one unit of the work."""
        self.keep()
        # A renewal that failed gave the task away; there is nothing here to work on.
        if self.held is None or self.stopping:
            return
        try:
            draft = self.client.specification(self.held)["draft"]
            action, payload = self.decider.consider(draft)
        except Rejected:
            # The agent cannot do this one. Holding it while failing helps nobody.
            self.let_go()
            return
        if action not in ("draft", "ask", "release"):
            # A decider that answers something else has a bug, and quietly treating it as
            # "release" would hide it. The task still goes back, in `run`'s `finally`.
            raise Rejected("A decider answers draft, ask or release, not " + repr(action))
        try:
            if action == "draft":
                self.client.draft(self.held, payload, expected_revision=draft["revision"])
            elif action == "ask":
                self.client.ask(self.held, payload, expected_revision=draft["revision"])
                # Having asked, the next move is a person's; the task is no longer ours.
                self.let_go()
                return
            else:
                self.let_go()
                return
        except Transient:
            # The work is still this agent's to do; the moment was wrong. Keep the task and
            # try the same step again rather than handing back work nobody else can take.
            self.wait(OUTAGE_SECONDS)
            return
        except Rejected:
            # A refusal is final. The task may have moved, been confirmed, or left this
            # agent's step entirely; either way, asking again unchanged will not help.
            self.let_go()
            return
        self.worked += 1

    def keep(self):
        """Renew before the lease runs down, and before working rather than after."""
        if self.held is None:
            return
        if self.clock() - self.renewed_at < self.lease * RENEW_FRACTION:
            return
        try:
            self.client.renew(self.held, self.lease)
            self.renewed_at = self.clock()
        except Rejected:
            # The lease is gone and so is the task. Someone else may already hold it.
            self.held = None

    def let_go(self):
        """Hand the task back, and never let failing to do so end the loop."""
        if self.held is None:
            return
        task, self.held = self.held, None
        try:
            self.client.release(task)
        except Rejected:
            pass

    def wait(self, seconds):
        """Pause, waking immediately if asked to stop."""
        self.sleeper(seconds)
