# syntax=docker/dockerfile:1.7
ARG NODE_IMAGE
ARG PYTHON_IMAGE
FROM ${NODE_IMAGE} AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --ignore-scripts
COPY web/ ./
RUN npm run build

FROM ${PYTHON_IMAGE} AS build
WORKDIR /build
COPY pyproject.toml ./
COPY src ./src
COPY --from=web /web/dist ./src/dgx_control/web
RUN python -m pip install --no-cache-dir --prefix=/install .

FROM ${PYTHON_IMAGE}
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 10001 control && useradd --uid 10001 --gid 10001 --no-create-home control
COPY --from=build /install /usr/local
USER 10001:10001
WORKDIR /srv/dgx-control
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
