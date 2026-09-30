FROM ubuntu:24.04 AS wheel-builder

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv python3-pip ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md /src/
COPY src /src/src
RUN python3 -m venv /opt/wheel-venv \
    && /opt/wheel-venv/bin/pip install --no-cache-dir --upgrade pip build \
    && /opt/wheel-venv/bin/python -m build --wheel --outdir /wheel /src

FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN apt-get update \
    && (apt-get install -y --no-install-recommends postgresql-client-16 || apt-get install -y --no-install-recommends postgresql-client) \
    && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 advisorio \
    && useradd --create-home --uid 10001 --gid advisorio --shell /usr/sbin/nologin advisor
COPY --from=wheel-builder /wheel/*.whl /tmp/
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir /tmp/*.whl 'psycopg[binary]>=3.2,<4' \
    && rm -rf /root/.cache /tmp/*.whl
RUN mkdir -p /work /artifacts /cache && chown -R advisor:advisor /work /artifacts /cache /opt/venv
USER advisor
WORKDIR /work
ENTRYPOINT ["/opt/venv/bin/pg-extstats-advisor"]
