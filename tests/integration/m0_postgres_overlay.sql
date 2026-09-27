\set ON_ERROR_STOP on
SET client_min_messages = warning;
SET default_statistics_target = 10000;

CREATE FUNCTION plan_rows(query text) RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE p json;
BEGIN
  EXECUTE 'EXPLAIN (FORMAT JSON) ' || query INTO p;
  RETURN (p->0->'Plan'->>'Plan Rows')::bigint;
END $$;

CREATE TABLE single_mcv(a int, b int);
INSERT INTO single_mcv SELECT g % 10, g % 10 FROM generate_series(1,10000) g;
ANALYZE single_mcv;
CREATE TEMP TABLE expected(name text PRIMARY KEY, rows bigint);
INSERT INTO expected VALUES
  ('single_empty', plan_rows('SELECT * FROM single_mcv WHERE a=1 AND b=1'));
CREATE STATISTICS single_mcv_ab (mcv) ON a,b FROM single_mcv;
ANALYZE single_mcv;
INSERT INTO expected VALUES
  ('single_physical', plan_rows('SELECT * FROM single_mcv WHERE a=1 AND b=1'));

CREATE TABLE overlap_mcv(a int, b int, c int);
INSERT INTO overlap_mcv
SELECT g % 10, g % 10,
       CASE WHEN g % 10 = 1 THEN CASE WHEN g % 5 < 4 THEN 1 ELSE 2 END
            WHEN g % 8 = 0 THEN 1 ELSE (g + 3) % 10 END
FROM generate_series(1,10000) g;
CREATE STATISTICS overlap_ab (mcv) ON a,b FROM overlap_mcv;
CREATE STATISTICS overlap_bc (mcv) ON b,c FROM overlap_mcv;
ANALYZE overlap_mcv;
INSERT INTO expected VALUES
  ('overlap_physical', plan_rows('SELECT * FROM overlap_mcv WHERE a=1 AND b=1 AND c=1'));

CREATE TABLE only_fd(a int, b int);
INSERT INTO only_fd SELECT g % 100, g % 100 FROM generate_series(1,10000) g;
CREATE STATISTICS only_fd_ab (dependencies) ON a,b FROM only_fd;
ANALYZE only_fd;
INSERT INTO expected VALUES
  ('fd_physical', plan_rows('SELECT * FROM only_fd WHERE a=1 AND b=1'));

CREATE TABLE mixed_stats(a int, b int, c int);
INSERT INTO mixed_stats SELECT g % 20, g % 20, g % 20 FROM generate_series(1,10000) g;
CREATE STATISTICS mixed_ab (mcv) ON a,b FROM mixed_stats;
CREATE STATISTICS mixed_bc (dependencies) ON b,c FROM mixed_stats;
ANALYZE mixed_stats;
INSERT INTO expected VALUES
  ('mixed_physical', plan_rows('SELECT * FROM mixed_stats WHERE a=1 AND b=1 AND c=1'));

CREATE TABLE unrelated(a int, b int);
INSERT INTO unrelated SELECT g % 10, g % 10 FROM generate_series(1,10000) g;
CREATE STATISTICS unrelated_ab (mcv) ON a,b FROM unrelated;
ANALYZE unrelated;
INSERT INTO expected VALUES
  ('unrelated_physical', plan_rows('SELECT * FROM unrelated WHERE a=1 AND b=1'));

CREATE TABLE error_stats(a int, b int);
INSERT INTO error_stats SELECT g % 10, g % 10 FROM generate_series(1,10000) g;
CREATE STATISTICS error_both (mcv, dependencies) ON a,b FROM error_stats;
CREATE STATISTICS error_both_fd_missing (dependencies) ON a,b FROM error_stats;
ANALYZE error_stats;

CREATE TEMP TABLE catalog_fingerprint AS
SELECT md5(string_agg(x, ',' ORDER BY x)) AS definitions,
       (SELECT md5(string_agg(y, ',' ORDER BY y)) FROM
          (SELECT stxoid::text || ':' || stxdinherit::text || ':' ||
                  coalesce(pg_mcv_list_send(stxdmcv)::text, '') || ':' ||
                  coalesce(pg_dependencies_send(stxddependencies)::text, '') y
             FROM pg_statistic_ext_data) d) AS payloads
FROM (SELECT oid::text || ':' || stxrelid::text || ':' || stxname x
        FROM pg_statistic_ext) s;

SELECT pg_hypothetical_extstats_register(e.oid, e.stxrelid, 'm',
                                         pg_mcv_list_send(d.stxdmcv))
FROM pg_statistic_ext e JOIN pg_statistic_ext_data d ON d.stxoid=e.oid
WHERE e.stxname IN ('single_mcv_ab','overlap_ab','overlap_bc','mixed_ab');
SELECT pg_hypothetical_extstats_register(e.oid, e.stxrelid, 'f',
                                         pg_dependencies_send(d.stxddependencies))
FROM pg_statistic_ext e JOIN pg_statistic_ext_data d ON d.stxoid=e.oid
WHERE e.stxname IN ('only_fd_ab','mixed_bc');
-- Deliberately register only FD for a definition that also has MCV.
SELECT pg_hypothetical_extstats_register(e.oid, e.stxrelid, 'f',
                                         pg_dependencies_send(d.stxddependencies))
FROM pg_statistic_ext e JOIN pg_statistic_ext_data d ON d.stxoid=e.oid
WHERE e.stxname='error_both';
-- Deliberately register only MCV for another definition that also has FD.
SELECT pg_hypothetical_extstats_register(target.oid, target.stxrelid, 'm',
                                         pg_mcv_list_send(source_data.stxdmcv))
FROM pg_statistic_ext target
JOIN pg_statistic_ext source ON source.stxname='error_both'
JOIN pg_statistic_ext_data source_data ON source_data.stxoid=source.oid
WHERE target.stxname='error_both_fd_missing';

CREATE FUNCTION expect_error(command text, fragment text) RETURNS void LANGUAGE plpgsql AS $$
DECLARE caught boolean := false;
BEGIN
  BEGIN
    EXECUTE command;
  EXCEPTION WHEN others THEN
    caught := position(fragment in SQLERRM) > 0;
  END;
  ASSERT caught, format('expected error containing %L from %s', fragment, command);
END $$;
SELECT expect_error('SELECT pg_hypothetical_extstats_activate(ARRAY[0::oid])',
                    'unregistered statistics object');
SELECT expect_error(format('SELECT pg_hypothetical_extstats_activate(ARRAY[%s::oid,%s::oid])',
                           (SELECT oid FROM pg_statistic_ext WHERE stxname='single_mcv_ab'),
                           (SELECT oid FROM pg_statistic_ext WHERE stxname='single_mcv_ab')),
                    'duplicate active statistics object');
SELECT expect_error(format('SELECT pg_hypothetical_extstats_register(%s::oid,%s::oid,''x'',decode(''00'',''hex''))',
                           (SELECT oid FROM pg_statistic_ext WHERE stxname='single_mcv_ab'),
                           'single_mcv'::regclass::oid), 'unsupported hypothetical statistics kind');
SELECT expect_error(format('SELECT pg_hypothetical_extstats_register(%s::oid,%s::oid,''m'',decode(''00'',''hex''))',
                           (SELECT oid FROM pg_statistic_ext WHERE stxname='single_mcv_ab'),
                           'single_mcv'::regclass::oid), 'invalid MCV size');
SELECT expect_error(format('SELECT pg_hypothetical_extstats_register(%s::oid,%s::oid,''m'',pg_mcv_list_send(d.stxdmcv)) FROM pg_statistic_ext_data d WHERE d.stxoid=%s',
                           (SELECT oid FROM pg_statistic_ext WHERE stxname='single_mcv_ab'),
                           'unrelated'::regclass::oid,
                           (SELECT oid FROM pg_statistic_ext WHERE stxname='single_mcv_ab')),
                    'belongs to relation');
SELECT expect_error(format('SELECT pg_hypothetical_extstats_register(%s::oid,%s::oid,''m'',pg_mcv_list_send(d.stxdmcv)) FROM pg_statistic_ext_data d WHERE d.stxoid=%s',
                           (SELECT oid FROM pg_statistic_ext WHERE stxname='single_mcv_ab'),
                           'single_mcv'::regclass::oid,
                           (SELECT oid FROM pg_statistic_ext WHERE stxname='single_mcv_ab')),
                    'already registered');

-- Registration alone is default-off.
DO $$ BEGIN
  ASSERT plan_rows('SELECT * FROM single_mcv WHERE a=1 AND b=1') =
         (SELECT rows FROM expected WHERE name='single_physical');
END $$;

-- Empty design hides registered candidates but leaves unrelated relations alone.
SELECT pg_hypothetical_extstats_activate(ARRAY[]::oid[]);
DO $$ BEGIN
  ASSERT plan_rows('SELECT * FROM single_mcv WHERE a=1 AND b=1') =
         (SELECT rows FROM expected WHERE name='single_empty');
  ASSERT plan_rows('SELECT * FROM unrelated WHERE a=1 AND b=1') =
         (SELECT rows FROM expected WHERE name='unrelated_physical');
END $$;

-- Single MCV exact equality.
SELECT pg_hypothetical_extstats_activate(ARRAY[(SELECT oid FROM pg_statistic_ext WHERE stxname='single_mcv_ab')]);
DO $$ BEGIN
  ASSERT plan_rows('SELECT * FROM single_mcv WHERE a=1 AND b=1') =
         (SELECT rows FROM expected WHERE name='single_physical');
END $$;

-- Catalog order A,B and explicitly reversed active order B,A.
SELECT pg_hypothetical_extstats_activate(ARRAY[
  (SELECT oid FROM pg_statistic_ext WHERE stxname='overlap_bc'),
  (SELECT oid FROM pg_statistic_ext WHERE stxname='overlap_ab')]);
CREATE TEMP TABLE observed(name text PRIMARY KEY, rows bigint, active oid[]);
INSERT INTO observed VALUES
 ('overlap_ba', plan_rows('SELECT * FROM overlap_mcv WHERE a=1 AND b=1 AND c=1'),
  pg_hypothetical_extstats_active());
SELECT pg_hypothetical_extstats_activate(ARRAY[
  (SELECT oid FROM pg_statistic_ext WHERE stxname='overlap_ab'),
  (SELECT oid FROM pg_statistic_ext WHERE stxname='overlap_bc')]);
INSERT INTO observed VALUES
 ('overlap_ab', plan_rows('SELECT * FROM overlap_mcv WHERE a=1 AND b=1 AND c=1'),
  pg_hypothetical_extstats_active());
DO $$ BEGIN
  ASSERT (SELECT active[1] FROM observed WHERE name='overlap_ba') =
         (SELECT oid FROM pg_statistic_ext WHERE stxname='overlap_bc');
  ASSERT (SELECT rows FROM observed WHERE name='overlap_ab') =
         (SELECT rows FROM expected WHERE name='overlap_physical');
  ASSERT (SELECT rows FROM observed WHERE name='overlap_ab') <>
         (SELECT rows FROM observed WHERE name='overlap_ba');
END $$;

-- FD-only and mixed native paths.
SELECT pg_hypothetical_extstats_activate(ARRAY[(SELECT oid FROM pg_statistic_ext WHERE stxname='only_fd_ab')]);
DO $$ BEGIN ASSERT plan_rows('SELECT * FROM only_fd WHERE a=1 AND b=1') =
  (SELECT rows FROM expected WHERE name='fd_physical'); END $$;
SELECT pg_hypothetical_extstats_activate(ARRAY[
  (SELECT oid FROM pg_statistic_ext WHERE stxname='mixed_ab'),
  (SELECT oid FROM pg_statistic_ext WHERE stxname='mixed_bc')]);
DO $$ BEGIN ASSERT plan_rows('SELECT * FROM mixed_stats WHERE a=1 AND b=1 AND c=1') =
  (SELECT rows FROM expected WHERE name='mixed_physical'); END $$;

-- Repeated activation: Y1, Y2, empty, Y1.
SELECT pg_hypothetical_extstats_activate(ARRAY[(SELECT oid FROM pg_statistic_ext WHERE stxname='single_mcv_ab')]);
CREATE TEMP TABLE repeat_result AS SELECT plan_rows('SELECT * FROM single_mcv WHERE a=1 AND b=1') y1;
SELECT pg_hypothetical_extstats_activate(ARRAY[(SELECT oid FROM pg_statistic_ext WHERE stxname='only_fd_ab')]);
SELECT pg_hypothetical_extstats_activate(ARRAY[]::oid[]);
SELECT pg_hypothetical_extstats_activate(ARRAY[(SELECT oid FROM pg_statistic_ext WHERE stxname='single_mcv_ab')]);
DO $$ BEGIN ASSERT plan_rows('SELECT * FROM single_mcv WHERE a=1 AND b=1') =
  (SELECT y1 FROM repeat_result); END $$;

-- A selected kind must never silently fall back to its catalog payload.
SELECT pg_hypothetical_extstats_activate(ARRAY[(SELECT oid FROM pg_statistic_ext WHERE stxname='error_both')]);
SELECT expect_error('SELECT plan_rows(''SELECT * FROM error_stats WHERE a=1 AND b=1'')',
                    'has no registered payload for kind m');
SELECT pg_hypothetical_extstats_activate(ARRAY[(SELECT oid FROM pg_statistic_ext WHERE stxname='error_both_fd_missing')]);
SELECT expect_error('SELECT plan_rows(''SELECT * FROM error_stats WHERE a=1 AND b=1'')',
                    'has no registered payload for kind f');

-- Catalogs are unchanged throughout the hot path.
DO $$ DECLARE a text; b text; BEGIN
  SELECT md5(string_agg(x, ',' ORDER BY x)) INTO a FROM
    (SELECT oid::text || ':' || stxrelid::text || ':' || stxname x FROM pg_statistic_ext) s;
  SELECT md5(string_agg(y, ',' ORDER BY y)) INTO b FROM
    (SELECT stxoid::text || ':' || stxdinherit::text || ':' ||
            coalesce(pg_mcv_list_send(stxdmcv)::text, '') || ':' ||
            coalesce(pg_dependencies_send(stxddependencies)::text, '') y
       FROM pg_statistic_ext_data) d;
  ASSERT a = (SELECT definitions FROM catalog_fingerprint);
  ASSERT b = (SELECT payloads FROM catalog_fingerprint);
END $$;

SELECT pg_hypothetical_extstats_reset();
DO $$ BEGIN
  ASSERT pg_hypothetical_extstats_active() IS NULL;
  ASSERT plan_rows('SELECT * FROM single_mcv WHERE a=1 AND b=1') =
         (SELECT rows FROM expected WHERE name='single_physical');
END $$;

SELECT e.name, e.rows AS physical_rows, o.rows AS hypothetical_rows, o.active
FROM expected e LEFT JOIN observed o ON e.name='overlap_physical' AND o.name='overlap_ab'
WHERE e.name IN ('single_empty','single_physical','fd_physical','mixed_physical','overlap_physical')
ORDER BY e.name;
SELECT * FROM observed ORDER BY name;
