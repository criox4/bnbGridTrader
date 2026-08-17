# The grid trader: ONE process — the A2A seller (:9000), which also runs the
# grid monitor thread when GRID_MONITOR=1.
#
# Build context is bnbGridTrader/ (the git root), not app/agent/, because
# chain.py locates config/bsc-contracts.json by walking parent directories. The
# shared address book must sit inside the image ABOVE the agent, so the layout
# here mirrors the repo: /app/config/ and /app/app/agent/.
FROM python:3.13-slim

# curl is here for the compose healthcheck; nothing in the app shells out.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so a code edit does not re-resolve the whole tree.
COPY app/agent/pyproject.toml /app/app/agent/
RUN pip install --no-cache-dir -e /app/app/agent

COPY config/ /app/config/
COPY app/ /app/app/

# Durable by default. All three are on the /data volume in compose; declaring
# them here means a plain `docker run` without -v still does not lose them on
# restart.
#   GRID_STATE_DIR        — the grid state file AND the flock. The level->lot
#                           mapping exists ONLY here: the chain knows the
#                           balances but not which rung bought them, so losing
#                           this orphans open lots and the grid re-buys rungs it
#                           already owns.
#   STORAGE_LOCAL_PATH    — ERC-8183 deliverables, served back out by
#                           main.py's /erc8183/job/{id}/response route. Losing
#                           these 404s every already-paid job's deliverable.
#   STUDIO_AUDIT_LOG_PATH — the signing audit trail. Its default sits under a
#                           root-owned .studio/, which the non-root process
#                           cannot write; onto the volume, where it is both
#                           writable and durable.
ENV GRID_STATE_DIR=/data/state \
    STORAGE_LOCAL_PATH=/data/deliverables \
    STUDIO_AUDIT_LOG_PATH=/data/audit/audit-log.jsonl
RUN mkdir -p /data/state /data/deliverables /data/audit
VOLUME ["/data"]

# The keystore is NEVER baked in — .dockerignore excludes .studio/. Mount it
# read-only at runtime and pass WALLET_PASSWORD from the host env. The variable
# name is the SDK's own (wallet.KEYSTORE_DIR_ENV); without it the wallet loader
# falls back to a workspace-relative .studio/wallets that this image
# deliberately does not contain, and signing fails at the first quote.
ENV BNBAGENT_KEYSTORE_DIR=/secrets/wallets

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Non-root; /data must be writable by it. The keystore mount must also be
# readable by uid 10001 on the HOST — a 600 root:root keystore looks correct and
# boots fine, then fails at the first signature as a PermissionError surfaced
# through the A2A error channel.
RUN useradd --create-home --uid 10001 agent \
    && chown -R agent:agent /data
USER agent

EXPOSE 9000

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "/app/app/agent/main.py"]
