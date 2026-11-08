FROM python:3.13-slim@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1 AS dependencies
COPY requirements.lock /requirements.lock
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir --only-binary=:all: --require-hashes -r /requirements.lock

FROM python:3.13-slim@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /app
COPY requirements.lock /app/requirements.lock
COPY LICENSE NOTICE /app/
COPY --from=dependencies /opt/venv /opt/venv
RUN groupadd -g 10001 app && useradd -u 10001 -g 10001 -M app && mkdir -p /var/lib/spec-enricher /backups /exports /config /sources && chown 10001:10001 /var/lib/spec-enricher /backups /exports && chmod 700 /var/lib/spec-enricher /backups /exports
COPY spec_enricher /app/spec_enricher
COPY catalog/requirements.schema.json catalog/requirements.full.json /app/catalog/
COPY templates /app/templates
COPY sources /app/sources
COPY qa /app/qa
USER 10001:10001
ENV APP_HOST=0.0.0.0 APP_PORT=8080 APP_DATA_DIR=/var/lib/spec-enricher APP_ALLOWED_ORIGIN=http://127.0.0.1:8080
EXPOSE 8080
CMD ["python", "-m", "spec_enricher.cli", "serve"]
