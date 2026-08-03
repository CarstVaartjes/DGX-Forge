# syntax=docker/dockerfile:1.7
ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE} AS build
WORKDIR /build
COPY pyproject.toml ./
COPY src ./src
RUN python -m pip install --no-cache-dir --prefix=/install .

ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}
RUN groupadd --gid 10001 control && useradd --uid 10001 --gid 10001 --no-create-home control
COPY --from=build /install /usr/local
USER 10001:10001
WORKDIR /srv/dgx-control
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
