# A container that runs the agent loop and holds nothing that can change the world.
#
# What goes in: the control plane's client and runner, and the protocol the agent works by.
# What deliberately does not: any credential. A GitHub or Jira token lives on the broker,
# under its own account, because a container that holds one can act whenever it likes while
# the evidence records a single approved call. See docs/outbound-dispatch.md.
#
# What this container is given at run time is an endpoint and its own secret file, mounted
# read-only. The secret proves which agent it is and grants nothing else.
#
#   docker build -f containers/agent.Dockerfile -t orchd-agent .
#   docker run --rm --network host \
#       -v /etc/orchd/agents/project-manager/secret:/run/secrets/agent:ro \
#       -e ORCHD_ENDPOINT=127.0.0.1:8721 \
#       -e ORCHD_ROLE=project_manager \
#       orchd-agent
#
# The decider is a fixture: it drafts nothing of substance and exists so the loop can be run
# and watched before there is an intelligence behind it. Replace `ORCHD_DECIDER` with an
# import path to your own once you have one.

FROM python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

# Nothing here needs root, and an agent that cannot write to its own image cannot be
# persuaded to modify itself.
RUN useradd --create-home --uid 10001 agent
WORKDIR /opt/orchd

COPY requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

# Only what an agent needs: the package, its contracts, and the protocol it works by.
COPY orch ./orch
COPY contracts ./contracts
COPY skills/orchd-protocol ./skills/orchd-protocol

USER agent
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    ORCHD_SECRET_FILE=/run/secrets/agent \
    ORCHD_DECIDER=orch.agent_main:Idle

# No HEALTHCHECK that talks to the control plane: a container reporting itself unhealthy
# because the service restarted would be restarted in turn, and two things restarting each
# other is worse than one of them waiting.
ENTRYPOINT ["python", "-m", "orch.agent_main"]
