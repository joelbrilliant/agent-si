# agent-si hub image. No image is published: build it yourself from a release wheel.
# Put the agent-si wheel from the releases page in dist/, then from the repository root:
#   docker build -f deploy/hub/Dockerfile -t agent-si-hub:<version> .
# python:3.13-slim, pinned by digest, matching the local Python pin.
FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

COPY dist/*.whl /tmp/wheel/
RUN python -m pip install --no-cache-dir /tmp/wheel/*.whl \
 && rm -rf /tmp/wheel \
 && brain --version \
 && useradd --uid 10001 --create-home --shell /usr/sbin/nologin brain \
 && mkdir -p /data /backups \
 && chown brain:brain /data /backups

USER brain
VOLUME ["/data", "/backups"]
EXPOSE 8080
# The slim image has no curl or wget, so the health check is Python's own urllib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=4).status == 200 else 1)"]
# No ENTRYPOINT, so `docker compose run --rm brain brain <subcommand>` works.
CMD ["brain", "hub", "serve", "--instance", "/data/brain", "--listen", "0.0.0.0:8080", "--backups", "/backups"]
