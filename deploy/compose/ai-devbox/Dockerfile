FROM ubuntu:24.04

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

RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh \
    | env UV_INSTALL_DIR=/usr/local/bin sh

RUN mkdir -p /opt/rust \
    && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
        | sh -s -- -y --profile minimal --no-modify-path \
    && chmod -R a+rX /opt/rust

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
