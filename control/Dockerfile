# syntax=docker/dockerfile:1.7
ARG NODE_IMAGE
ARG PYTHON_IMAGE
FROM ${NODE_IMAGE} AS web
WORKDIR /web
COPY control/web/package.json control/web/package-lock.json ./
RUN npm ci --ignore-scripts
COPY control/web/ ./
RUN npm run build

FROM ${PYTHON_IMAGE} AS build
WORKDIR /build
COPY control/pyproject.toml ./
COPY control/src ./src
COPY --from=web /web/dist ./src/dgx_control/web
COPY inventory/wheels/dgx_agent_protocol-1.0.0-py3-none-any.whl /wheels/
RUN python -m pip install --no-cache-dir --prefix=/install \
    /wheels/dgx_agent_protocol-1.0.0-py3-none-any.whl .

FROM ${PYTHON_IMAGE}
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 10001 control && useradd --uid 10001 --gid 10001 --no-create-home control
COPY --from=build /install /usr/local
USER 10001:10001
WORKDIR /srv/dgx-control
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
