# Use the official Python 3.11 slim image
# syntax=docker/dockerfile:1.6

# Stage 1: Download external binaries
FROM python:3.11-slim AS fetcher

# Install tools needed to fetch external assets
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Support multi-arch builds (defaults to amd64 if not provided)
ARG TARGETARCH=amd64

# 1. Fetch Cloudflare Tunnel (cloudflared)
RUN curl -L "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${TARGETARCH}" \
        -o /usr/local/bin/cloudflared \
    && chmod +x /usr/local/bin/cloudflared

# 2. Add GitHub CLI apt repository (keyring + sources list)
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list


# Stage 2: Final Base Image
FROM python:3.11-slim AS base

# Prevent interactive prompts during build
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1


# 2. Define Build Arguments for UID and GID
ARG USER_ID=1000
ARG GROUP_ID=1000

# Update and install apt-based packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Networking & Debugging
    curl \
    wget \
    telnet \
    dnsutils \
    iputils-ping \
    openssh-server \
    openssl \
    ca-certificates \
    ripgrep \
    git \
    zip \
    p7zip-full \
    bzip2 \
    unrar-free \
    # Utilities
    ccrypt \
    xxd \
    tmux \
    jq \
    sqlite3 libsqlite3-dev \
    rsync \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy binaries/configs from fetcher stage
COPY --from=fetcher /usr/local/bin/cloudflared /usr/local/bin/cloudflared
COPY --from=fetcher /usr/share/keyrings/githubcli-archive-keyring.gpg /usr/share/keyrings/githubcli-archive-keyring.gpg
COPY --from=fetcher /etc/apt/sources.list.d/github-cli.list /etc/apt/sources.list.d/github-cli.list


# Install GitHub CLI (gh) now that the repo is added
RUN apt-get update && apt-get install -y --no-install-recommends gh \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Setup for SSHD (generate host keys + runtime dir)
RUN mkdir -p /var/run/sshd \
    && chmod 0755 /var/run/sshd \
    && ssh-keygen -A


# 4. Create User and Group matching Host Identity
RUN groupadd -g ${GROUP_ID} devgroup &&     useradd -l -u ${USER_ID} -g devgroup -m devuser

# 5. Setup Workspace
WORKDIR /workspace
RUN chown ${USER_ID}:${GROUP_ID} /workspace

# 6. Switch to Non-Root User
USER devuser

# Verify environment
RUN whoami && pwd && ls -ld /workspace

# Default command
CMD ["python3"]
