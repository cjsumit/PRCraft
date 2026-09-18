FROM python:3.11-slim AS base

# System deps: git (repo operations) + docker CLI (sandbox backend, talks to
# the host docker daemon via the mounted socket -- see README for the run
# command that mounts /var/run/docker.sock).
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo \
        "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
        https://download.docker.com/linux/debian bookworm stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /tmp/ghbot-workspaces \
    && chown -R appuser:appuser /srv/app /tmp/ghbot-workspaces
USER appuser

ENTRYPOINT ["python", "-m", "app.cli"]
