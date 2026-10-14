# Locally built, disposable test probe. Python and locked wheels are acquired
# before this build and copied in; no registry token, network install or secret.
FROM postgres:17.11-bookworm@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826
COPY python /opt/python
COPY packages /opt/packages
ENTRYPOINT ["/opt/python/bin/python3.12", "-m", "pytest"]
