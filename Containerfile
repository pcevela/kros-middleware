# syntax=docker/dockerfile:1
# Build with: podman build --format docker -t localhost/kros-middleware:latest -f Containerfile .
# (--format docker keeps the HEALTHCHECK; the Quadlet defines its own health check anyway.)
# Base images are configurable; defaults are Red Hat UBI 9 (freely redistributable).
# Pin them to a digest for reproducible builds, e.g. BUILD_IMAGE=...@sha256:<digest>.
ARG BUILD_IMAGE=registry.access.redhat.com/ubi9/python-312:latest
ARG RUNTIME_IMAGE=registry.access.redhat.com/ubi9/python-312-minimal:latest

FROM ${BUILD_IMAGE} AS build
USER 0
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python3 -m venv /opt/kros-middleware \
 && /opt/kros-middleware/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/kros-middleware/bin/pip install --no-cache-dir . \
 && find /opt/kros-middleware -name '__pycache__' -prune -exec rm -rf {} +

# The runtime image has no compilers; the venv is built against the same Python version.
FROM ${RUNTIME_IMAGE}
ARG VERSION=dev
LABEL org.opencontainers.image.title="kros-middleware" \
      org.opencontainers.image.description="Restricting proxy and webhook receiver for the KROS OpenAPI" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}"
USER 0
COPY --from=build /opt/kros-middleware /opt/kros-middleware
RUN mkdir -p /data && chown 1001:0 /data && chmod 0700 /data
USER 1001
ENV PATH=/opt/kros-middleware/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KM_DB_PATH=/data/kros-middleware.sqlite3 \
    KM_LISTEN_HOST=0.0.0.0 \
    KM_LISTEN_PORT=8080
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD ["python3", "-c", "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('KM_LISTEN_PORT','8080'), timeout=4).status == 200 else 1)"]
ENTRYPOINT ["kros-middleware"]
