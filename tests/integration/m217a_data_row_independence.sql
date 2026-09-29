\set ON_ERROR_STOP on
SET client_min_messages = warning;
SET default_statistics_target=1000;

CREATE OR REPLACE FUNCTION plan_rows(query text) RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE p json;
BEGIN
  EXECUTE 'EXPLAIN (FORMAT JSON) ' || query INTO p;
  RETURN (p->0->'Plan'->>'Plan Rows')::bigint;
END $$;

CREATE TABLE t(a int,b int,c int);
INSERT INTO t SELECT g % 20, (g/20)%20, g%7 FROM generate_series(1,20000) g;
CREATE STATISTICS st_mcv (mcv) ON a,b FROM t;
CREATE STATISTICS st_fd (dependencies) ON a,b FROM t;
ANALYZE t;
CREATE STATISTICS st_mcv_nodata (mcv) ON a,b FROM t;
CREATE STATISTICS st_fd_nodata (dependencies) ON a,b FROM t;

CREATE TABLE u(a int,b int);
INSERT INTO u SELECT g % 20, (g/20)%20 FROM generate_series(1,20000) g;
CREATE STATISTICS u_mcv_nodata (mcv) ON a,b FROM u;

CREATE TEMP TABLE expected(name text PRIMARY KEY, rows bigint);
INSERT INTO expected VALUES
  ('physical_mcv', plan_rows('SELECT * FROM t WHERE a=1 AND b=1'));

-- Physical data row present: PRESENT MCV plus ABSENT_NATIVE FD.
SELECT pg_hypothetical_extstats_reset();
SELECT pg_hypothetical_extstats_register(
  (SELECT oid FROM pg_statistic_ext WHERE stxname='st_mcv'),
  't'::regclass::oid, 'm',
  pg_mcv_list_send((SELECT stxdmcv FROM pg_statistic_ext_data
                    WHERE stxoid=(SELECT oid FROM pg_statistic_ext WHERE stxname='st_mcv'))));
SELECT pg_hypothetical_extstats_register_absent(
  (SELECT oid FROM pg_statistic_ext WHERE stxname='st_fd'),
  't'::regclass::oid, 'f');
SELECT pg_hypothetical_extstats_activate(ARRAY[
  (SELECT oid FROM pg_statistic_ext WHERE stxname='st_mcv'),
  (SELECT oid FROM pg_statistic_ext WHERE stxname='st_fd')]);
INSERT INTO expected VALUES ('present_row', plan_rows('SELECT * FROM t WHERE a=1 AND b=1'));

-- Definition-only row absent: the same PRESENT/ABSENT_NATIVE realization.
SELECT pg_hypothetical_extstats_reset();
SELECT pg_hypothetical_extstats_register(
  (SELECT oid FROM pg_statistic_ext WHERE stxname='st_mcv_nodata'),
  't'::regclass::oid, 'm',
  pg_mcv_list_send((SELECT stxdmcv FROM pg_statistic_ext_data
                    WHERE stxoid=(SELECT oid FROM pg_statistic_ext WHERE stxname='st_mcv'))));
SELECT pg_hypothetical_extstats_register_absent(
  (SELECT oid FROM pg_statistic_ext WHERE stxname='st_fd_nodata'),
  't'::regclass::oid, 'f');
SELECT pg_hypothetical_extstats_activate(ARRAY[
  (SELECT oid FROM pg_statistic_ext WHERE stxname='st_mcv_nodata'),
  (SELECT oid FROM pg_statistic_ext WHERE stxname='st_fd_nodata')]);
INSERT INTO expected VALUES ('absent_row', plan_rows('SELECT * FROM t WHERE a=1 AND b=1'));

DO $$ BEGIN
  ASSERT (SELECT rows FROM expected WHERE name='present_row') =
         (SELECT rows FROM expected WHERE name='absent_row');
END $$;

-- Definition-only, unregistered statistics remain ignored exactly upstream.
SELECT pg_hypothetical_extstats_reset();
INSERT INTO expected VALUES ('u_no_registration', plan_rows('SELECT * FROM u WHERE a=1 AND b=1'));
DO $$ BEGIN
  ASSERT (SELECT count(*) FROM pg_statistic_ext_data
          WHERE stxoid=(SELECT oid FROM pg_statistic_ext WHERE stxname='u_mcv_nodata')) = 0;
  ASSERT plan_rows('SELECT * FROM u WHERE a=1 AND b=1') =
         (SELECT rows FROM expected WHERE name='u_no_registration');
END $$;

-- Activation of a definition without a registry entry still fails.
DO $$ BEGIN
  BEGIN
    PERFORM pg_hypothetical_extstats_activate(ARRAY[
      (SELECT oid FROM pg_statistic_ext WHERE stxname='u_mcv_nodata')]);
    RAISE EXCEPTION 'expected unregistered activation failure';
  EXCEPTION WHEN others THEN
    ASSERT position('unregistered statistics object' in SQLERRM) > 0;
  END;
END $$;

-- Registration without activation leaves the physical plan unchanged.
SELECT pg_hypothetical_extstats_register(
  (SELECT oid FROM pg_statistic_ext WHERE stxname='st_mcv_nodata'),
  't'::regclass::oid, 'm',
  pg_mcv_list_send((SELECT stxdmcv FROM pg_statistic_ext_data
                    WHERE stxoid=(SELECT oid FROM pg_statistic_ext WHERE stxname='st_mcv'))));
DO $$ BEGIN
  ASSERT plan_rows('SELECT * FROM t WHERE a=1 AND b=1') =
         (SELECT rows FROM expected WHERE name='physical_mcv');
END $$;

SELECT name, rows FROM expected ORDER BY name;
SELECT e.stxname, e.oid, d.stxoid IS NOT NULL AS data_row,
       d.stxdmcv IS NOT NULL AS mcv_payload,
       d.stxddependencies IS NOT NULL AS fd_payload
FROM pg_statistic_ext e LEFT JOIN pg_statistic_ext_data d ON d.stxoid=e.oid
WHERE e.stxname IN ('st_mcv','st_fd','st_mcv_nodata','st_fd_nodata','u_mcv_nodata')
ORDER BY e.stxname;
