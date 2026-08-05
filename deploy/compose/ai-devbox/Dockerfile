ARG UBUNTU_IMAGE=ubuntu:24.04@sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea
ARG NODE_IMAGE=node:22-bookworm-slim@sha256:f32b81066cde10a75dbac96646099533316d94bac4150c55da1636e1f0ffdc46
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.8.14@sha256:f3660c56d5b08d6c516360981bedc439f499b9bf37f46a216018da3777a74011
ARG RUST_IMAGE=rust:1.89-bookworm@sha256:948f9b08a66e7fe01b03a98ef1c7568292e07ec2e4fe90d88c07bb14563c84ff

FROM ${NODE_IMAGE} AS node-runtime
FROM ${UV_IMAGE} AS uv-runtime
FROM ${RUST_IMAGE} AS rust-runtime
FROM ${UBUNTU_IMAGE} AS runtime

ARG USERNAME=ai-dev
ARG USER_UID=1100
ARG USER_GID=1100

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Europe/Amsterdam \
    NPM_CONFIG_PREFIX=/home/ai-dev/.local \
    UV_CACHE_DIR=/cache/uv \
    PIP_CACHE_DIR=/cache/pip \
    RUSTUP_HOME=/opt/rust/rustup \
    CARGO_HOME=/opt/rust/cargo \
    CARGO_INSTALL_ROOT=/home/ai-dev/.local \
    PATH=/home/ai-dev/.local/bin:/opt/rust/cargo/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        openssh-server ca-certificates curl wget git git-lfs gnupg jq less tmux \
        ripgrep fd-find fzf tree htop btop vim nano unzip zip tar xz-utils \
        build-essential pkg-config python3 python3-pip python3-venv \
        python-is-python3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node-runtime /usr/local/ /usr/local/
COPY --from=uv-runtime /uv /uvx /usr/local/bin/
COPY --from=rust-runtime /usr/local/cargo/ /opt/rust/cargo/
COPY --from=rust-runtime /usr/local/rustup/ /opt/rust/rustup/

RUN chmod -R a+rX /opt/rust

RUN groupadd --gid "${USER_GID}" "${USERNAME}" \
    && useradd \
        --uid "${USER_UID}" \
        --gid "${USER_GID}" \
        --create-home \
        --home-dir "/home/${USERNAME}" \
        --shell /bin/bash \
        --no-log-init \
        "${USERNAME}" \
    && passwd -d "${USERNAME}"

RUN mkdir -p \
        /cache \
        /etc/ai-devbox/skel \
        /etc/ssh/sshd_config.d \
        /run/sshd \
        /var/lib/ai-devbox/ssh-host-keys \
        /workspaces \
    && printf '%s\n' \
        'export PATH="$HOME/.local/bin:/opt/rust/cargo/bin:$PATH"' \
        'export NPM_CONFIG_PREFIX="$HOME/.local"' \
        'export UV_CACHE_DIR="/cache/uv"' \
        'export PIP_CACHE_DIR="/cache/pip"' \
        'export CARGO_INSTALL_ROOT="$HOME/.local"' \
        'export EDITOR=vim' \
        'export VISUAL=vim' \
        'alias ll="ls -lah"' \
        'alias fd="fdfind"' \
        > /etc/ai-devbox/skel/.bashrc \
    && printf '%s\n' \
        'set -g mouse on' \
        'set -g history-limit 100000' \
        'set -g status-interval 5' \
        'set -g default-terminal "screen-256color"' \
        'set -g allow-rename off' \
        > /etc/ai-devbox/skel/.tmux.conf \
    && chown "${USER_UID}:${USER_GID}" /cache /workspaces

COPY sshd_config /etc/ssh/sshd_config.d/ai-devbox.conf
COPY entrypoint.sh /usr/local/bin/start-ai-devbox

RUN chmod 0644 /etc/ssh/sshd_config.d/ai-devbox.conf \
    && chmod 0755 /usr/local/bin/start-ai-devbox \
    && ssh-keygen -q -t ed25519 -N '' \
        -f /var/lib/ai-devbox/ssh-host-keys/ssh_host_ed25519_key \
    && ssh-keygen -q -t rsa -b 4096 -N '' \
        -f /var/lib/ai-devbox/ssh-host-keys/ssh_host_rsa_key \
    && /usr/sbin/sshd -t -f /etc/ssh/sshd_config.d/ai-devbox.conf \
    && find /var/lib/ai-devbox/ssh-host-keys -type f -delete

WORKDIR /workspaces
EXPOSE 22
CMD ["/usr/local/bin/start-ai-devbox"]
