FROM ubuntu:24.04 AS pg-builder

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential ca-certificates bison flex libreadline-dev zlib1g-dev \
       libssl-dev libxml2-dev libxslt1-dev libicu-dev pkg-config perl \
    && rm -rf /var/lib/apt/lists/*
COPY postgresql-16.14.tar.bz2 /tmp/postgresql-16.14.tar.bz2
RUN echo 'f6d077142737920858ce958ccdb75c9f58f976f44e3471  /tmp/postgresql-16.14.tar.bz2' | sha256sum -c - \
    && mkdir -p /src/postgresql-16.14 \
    && tar -xjf /tmp/postgresql-16.14.tar.bz2 -C /src/postgresql-16.14 --strip-components=1 \
    && cd /src/postgresql-16.14 \
    && ./configure --prefix=/opt/postgresql-16.14-stock --without-readline --without-zlib --without-icu \
    && make -j"$(nproc)" \
    && make install

FROM ubuntu:24.04
ENV PATH=/opt/postgresql-16.14-stock/bin:$PATH \
    PGDATA=/var/lib/postgresql/data \
    PGPORT=5432
RUN useradd --create-home --uid 10003 --shell /usr/sbin/nologin postgres \
    && mkdir -p /var/lib/postgresql/data /work \
    && chown -R postgres:postgres /var/lib/postgresql /work
COPY --from=pg-builder /opt/postgresql-16.14-stock /opt/postgresql-16.14-stock
COPY docker/stock-entrypoint.sh /usr/local/bin/stock-postgres
RUN chown postgres:postgres /usr/local/bin/stock-postgres \
    && chmod 0755 /usr/local/bin/stock-postgres
USER postgres
ENTRYPOINT ["/usr/local/bin/stock-postgres"]
