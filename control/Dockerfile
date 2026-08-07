# syntax=docker/dockerfile:1.7
ARG NODE_IMAGE=node:24-bookworm-slim@sha256:235600a8101ab264e117b1768e925532262668dc9b581ef1dd7d96ced463b8e7
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b
FROM ${NODE_IMAGE} AS web
WORKDIR /web
COPY control/web/package.json control/web/package-lock.json ./
RUN npm ci --ignore-scripts
COPY control/web/ ./
RUN npm run build

FROM ${PYTHON_IMAGE} AS build
WORKDIR /build
COPY pyproject.toml /vonk-cluster-profiles/
COPY src/cluster_profiles /vonk-cluster-profiles/src/cluster_profiles
COPY control/pyproject.toml ./
COPY control/src ./src
COPY --from=web /web/dist ./src/vonk_control/web
COPY inventory/wheels/vonk_agent_protocol-2.1.0-py3-none-any.whl /wheels/
RUN python -m pip install --no-cache-dir --prefix=/install \
    /wheels/vonk_agent_protocol-2.1.0-py3-none-any.whl /vonk-cluster-profiles .

FROM ${PYTHON_IMAGE} AS runtime
RUN groupadd --gid 10001 control && useradd --uid 10001 --gid 10001 --no-create-home control
COPY --from=build /install /usr/local
WORKDIR /srv/dgx-control
COPY control/alembic.ini /srv/dgx-control/alembic.ini
COPY control/migrations /srv/dgx-control/migrations
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

FROM runtime AS worker
USER 10001:10001

FROM runtime AS api
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*
USER 10001:10001
