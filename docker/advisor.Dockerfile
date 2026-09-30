FROM ubuntu:24.04 AS pg-builder

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential ca-certificates bison flex libreadline-dev zlib1g-dev \
       libssl-dev libxml2-dev libxslt1-dev libicu-dev pkg-config perl \
    && rm -rf /var/lib/apt/lists/*
COPY postgresql-16.14.tar.bz2 /tmp/postgresql-16.14.tar.bz2
COPY pg/patches/postgresql-16.14-hypothetical-extstats.patch /tmp/hypothetical.patch
RUN echo 'f6d077142737920858ce958ccdb75c6ee137a63b5b0853c70693d401ac7e3471  /tmp/postgresql-16.14.tar.bz2' | sha256sum -c - \
    && mkdir -p /src/postgresql-16.14 \
    && tar -xjf /tmp/postgresql-16.14.tar.bz2 -C /src/postgresql-16.14 --strip-components=1 \
    && patch -d /src/postgresql-16.14 -p1 --batch --forward < /tmp/hypothetical.patch \
    && cd /src/postgresql-16.14 \
    && ./configure --prefix=/opt/postgresql-16.14-advisor --without-readline --without-zlib --without-icu \
    && make -j"$(nproc)" \
    && make install \
    && sha256sum /opt/postgresql-16.14-advisor/bin/postgres > /tmp/postgres.sha256 \
    && printf '%s\n' \
       '{' \
       '  "postgres_version": "16.14",' \
       '  "upstream_tarball_sha256": "f6d077142737920858ce958ccdb75c6ee137a63b5b0853c70693d401ac7e3471",' \
       '  "patch_sha256": "22c7f48632585e81fd8a557dc8bffba873ac5da070aca31713e22c60261c3b4f",' \
       '  "configure_args": ["--prefix=/opt/postgresql-16.14-advisor", "--without-readline", "--without-zlib", "--without-icu"],' \
       '  "compiler": "gcc",' \
       '  "compiler_version": "'"$(gcc --version | head -n1)'"' \
       '}' > /tmp/postgres-build-provenance.json

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
    PATH=/opt/venv/bin:/opt/postgresql-16.14-advisor/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PGEXT_ADVISOR_PG_PREFIX=/opt/postgresql-16.14-advisor
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10002 --shell /usr/sbin/nologin advisor
COPY --from=pg-builder /opt/postgresql-16.14-advisor /opt/postgresql-16.14-advisor
COPY --from=pg-builder /tmp/postgres.sha256 /opt/postgresql-16.14-advisor/postgres.sha256
COPY --from=pg-builder /tmp/postgres-build-provenance.json /opt/postgresql-16.14-advisor/build-provenance.json
COPY --from=wheel-builder /wheel/*.whl /tmp/
COPY docker/advisor-entrypoint.sh /usr/local/bin/pg-extstats-advisor-local
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir /tmp/*.whl 'psycopg[binary]>=3.2,<4' \
    && rm -rf /root/.cache /tmp/*.whl \
    && mkdir -p /work /artifacts /cache \
    && chown -R advisor:advisor /work /artifacts /cache /opt/venv /opt/postgresql-16.14-advisor
USER advisor
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/pg-extstats-advisor-local"]
