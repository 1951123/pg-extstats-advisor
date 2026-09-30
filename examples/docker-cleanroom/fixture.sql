CREATE TABLE public.fixture (a text, b text, c text);
INSERT INTO public.fixture (a, b, c) VALUES
  ('hot', 'x', 'one'), ('hot', 'x', 'one'), ('hot', 'x', 'two'),
  ('hot', 'y', 'one'), ('cold', 'x', 'one'), ('cold', 'y', 'two'),
  ('cold', 'y', 'two'), ('warm', 'z', 'three');
GRANT USAGE ON SCHEMA public TO capture;
GRANT SELECT ON public.fixture TO capture;
GRANT SELECT ON pg_catalog.pg_statistic_ext, pg_catalog.pg_statistic_ext_data TO capture;
ANALYZE public.fixture;
