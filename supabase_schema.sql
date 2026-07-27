-- ============================================================================
-- movik/supabase_schema.sql
-- ============================================================================
-- Todas las tablas del proyecto viven en el schema "movik".
--
-- Se aplica solo:   python load_to_supabase.py --init-schema
-- o a mano:         pégalo en el SQL Editor de Supabase y ejecútalo.
--
-- Es idempotente (DROP ... IF EXISTS + CREATE), así que se puede volver a
-- correr; ojo que eso VACÍA las tablas y hay que recargar.
--
-- SOBRE "Exposed schemas"
-- -----------------------
-- PostgREST — la API REST que usa supabase-js con la anon key — solo sirve los
-- schemas que estén en Settings → API → Exposed schemas. Ese ajuste no siempre
-- está disponible, así que NO dependemos de él: al final de este archivo se
-- crean vistas en "public" (que PostgREST expone siempre) apuntando a las
-- tablas de "movik". Los datos siguen viviendo en movik; public solo tiene
-- punteros de solo lectura.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS movik;

-- ── carriers ────────────────────────────────────────────────────────────────
-- Espejo 1:1 de la tabla carriers de movik.db (FL + CA + NC).
DROP TABLE IF EXISTS movik.carriers CASCADE;
CREATE TABLE movik.carriers (
    dot_number        text PRIMARY KEY,
    legal_name        text,
    dba_name          text,
    phy_state         text,
    phy_city          text,
    phy_street        text,
    phy_zip           text,
    phone             text,
    cell_phone        text,
    email_address     text,
    company_officer_1 text,
    power_units       integer,
    truck_units       integer,
    fleetsize         integer,
    total_drivers     integer,
    classdef          text,
    carrier_operation text,
    safety_rating     text,
    mcs150_date       text,
    status_code       text
);
CREATE INDEX idx_carriers_state ON movik.carriers (phy_state);

-- ── ucc_filings ─────────────────────────────────────────────────────────────
-- Espejo 1:1 de ucc_filings de movik.db. `id` se conserva tal cual (no es
-- serial aquí) para que el filing representativo siga siendo identificable.
DROP TABLE IF EXISTS movik.ucc_filings CASCADE;
CREATE TABLE movik.ucc_filings (
    id                  bigint PRIMARY KEY,
    dot_number          text NOT NULL,
    legal_name_searched text,
    match_found         smallint NOT NULL DEFAULT 0,
    alert_priority      text,
    ucc_number          text,
    date_filed          text,
    expires_date        text,
    days_to_expiry      integer,
    secured_party       text,
    secured_party_addr  text,
    secured_party_type  text,
    filing_type         text,
    filing_status       text,
    state_registry      text,
    scraped_at          text
);
CREATE INDEX idx_ucc_dot     ON movik.ucc_filings (dot_number);
CREATE INDEX idx_ucc_match   ON movik.ucc_filings (match_found);
CREATE INDEX idx_ucc_name    ON movik.ucc_filings (upper(legal_name_searched));
CREATE INDEX idx_ucc_party   ON movik.ucc_filings (secured_party);

-- ── maestro ─────────────────────────────────────────────────────────────────
-- El join final carriers × scrape_log × filing representativo de ucc_filings.
-- Una fila por carrier: mismas 27 columnas que produce 03_build_master.py más
-- las que Movikapp necesita para reproducir su vista mv_alerts sin hacer joins
-- en el cliente (units, n_filings, secured_party_type, search_name, prio_rank).
DROP TABLE IF EXISTS movik.maestro CASCADE;
CREATE TABLE movik.maestro (
    dot_number        text PRIMARY KEY,
    legal_name        text,
    dba_name          text,
    phy_state         text,
    phy_city          text,
    phy_street        text,
    phy_zip           text,
    phone             text,
    cell_phone        text,
    email_address     text,
    company_officer_1 text,
    power_units       integer,
    truck_units       integer,
    fleetsize         integer,
    total_drivers     integer,
    units             integer,          -- COALESCE(power_units, truck_units, 0)
    classdef          text,
    carrier_operation text,
    safety_rating     text,
    mcs150_date       text,
    -- Resultado del cruce con UCC
    priority          text,             -- P1 / P2a / P2b / P2c / P3 / P4 / Error / Sin datos
    prio_rank         smallint,         -- orden de urgencia; ver nota abajo
    ucc_found         text,             -- Sí / No / Error / Sin datos (ST)
    ucc_number        text,
    date_filed        text,
    expires_date      text,             -- MM/DD/YYYY, como lo guarda el scraper
    expires_date_iso  date,             -- misma fecha parseada, para calcular días
    days_to_expiry    integer,          -- congelado al momento del scrape
    secured_party     text,
    secured_party_type text,
    filing_status     text,
    state_registry    text,
    n_filings         integer DEFAULT 0,
    scraped_at        text,
    notes             text,
    search_name       text              -- UPPER(legal_name)
);

-- prio_rank sigue el orden de Movikapp (queries.ts PRIORITY_RANK):
--   P1=0  P2b=1  P2c=2  P2a=3  P3=4  P4=5  otros=9
-- NO el de 03_build_master.py (que pone P2a antes que P2b). Esta tabla es la
-- que ordena la bandeja de alertas de la app, así que manda el orden de la app.

CREATE INDEX idx_maestro_prio    ON movik.maestro (prio_rank, phy_state);
CREATE INDEX idx_maestro_state   ON movik.maestro (phy_state);
CREATE INDEX idx_maestro_units   ON movik.maestro (units);
CREATE INDEX idx_maestro_pri     ON movik.maestro (priority);
CREATE INDEX idx_maestro_name    ON movik.maestro (legal_name);

-- ── maestro_live ────────────────────────────────────────────────────────────
-- `days` no puede ser una columna: envejecería un día por cada día que pasa sin
-- recargar. Se calcula al leer, igual que hace db.ts en local con julianday().
CREATE OR REPLACE VIEW movik.maestro_live AS
SELECT m.*,
       COALESCE((m.expires_date_iso - CURRENT_DATE), m.days_to_expiry) AS days
  FROM movik.maestro m;

-- ── Vistas de agregados ─────────────────────────────────────────────────────
-- PostgREST no sabe hacer GROUP BY, así que los conteos que la app calcula con
-- SQL en local salen de estas vistas.

CREATE OR REPLACE VIEW movik.v_priority_counts AS
SELECT priority, COUNT(*)::bigint AS n
  FROM movik.maestro GROUP BY priority;

CREATE OR REPLACE VIEW movik.v_state_counts AS
SELECT phy_state AS code, COUNT(*)::bigint AS n
  FROM movik.maestro GROUP BY phy_state;

-- Carriers distintos por secured party (top 6 del reporte).
CREATE OR REPLACE VIEW movik.v_top_parties AS
SELECT secured_party AS label, COUNT(DISTINCT dot_number)::bigint AS value
  FROM movik.ucc_filings
 WHERE match_found = 1 AND secured_party IS NOT NULL AND secured_party <> ''
 GROUP BY secured_party
 ORDER BY value DESC;

-- Una fila por (secured party, tipo). La app reclasifica por nombre los que
-- vienen con secured_party_type NULL (FL nunca lo llena), con las mismas
-- reglas que en local.
CREATE OR REPLACE VIEW movik.v_secured_parties AS
SELECT secured_party, secured_party_type, COUNT(DISTINCT dot_number)::bigint AS n
  FROM movik.ucc_filings
 WHERE match_found = 1 AND secured_party IS NOT NULL AND secured_party <> ''
 GROUP BY secured_party, secured_party_type;

-- Avance del scraper por estado. Se deriva de maestro.priority en vez de
-- scrape_log: 'Sin datos' = nunca consultado, 'P1' = consultado sin match,
-- 'Error' = la consulta falló, el resto = consultado con filings.
CREATE OR REPLACE VIEW movik.v_scraper_status AS
SELECT phy_state AS code,
       COUNT(*)::bigint                                                   AS total,
       COUNT(*) FILTER (WHERE priority <> 'Sin datos')::bigint            AS scraped,
       COUNT(*) FILTER (WHERE priority NOT IN
             ('Sin datos', 'Error', 'P1'))::bigint                        AS found,
       COUNT(*) FILTER (WHERE priority = 'P1')::bigint                    AS not_found,
       COUNT(*) FILTER (WHERE priority = 'Error')::bigint                 AS errors,
       MAX(scraped_at)                                                    AS last_scraped_at
  FROM movik.maestro
 GROUP BY phy_state;

-- ── Permisos ────────────────────────────────────────────────────────────────
-- La app lee con la anon key. RLS activado + una política de solo lectura:
-- sin esto la anon key no ve nada, o lo ve todo sin control.
ALTER TABLE movik.carriers    ENABLE ROW LEVEL SECURITY;
ALTER TABLE movik.ucc_filings ENABLE ROW LEVEL SECURITY;
ALTER TABLE movik.maestro     ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS anon_read ON movik.carriers;
DROP POLICY IF EXISTS anon_read ON movik.ucc_filings;
DROP POLICY IF EXISTS anon_read ON movik.maestro;
CREATE POLICY anon_read ON movik.carriers    FOR SELECT TO anon, authenticated USING (true);
CREATE POLICY anon_read ON movik.ucc_filings FOR SELECT TO anon, authenticated USING (true);
CREATE POLICY anon_read ON movik.maestro     FOR SELECT TO anon, authenticated USING (true);

GRANT USAGE ON SCHEMA movik TO anon, authenticated, service_role;
GRANT SELECT ON ALL TABLES IN SCHEMA movik TO anon, authenticated;
GRANT ALL    ON ALL TABLES IN SCHEMA movik TO service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA movik GRANT SELECT ON TABLES TO anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA movik GRANT ALL    ON TABLES TO service_role;

-- Las vistas heredan los permisos del creador (postgres) y no soportan RLS
-- propia; los datos que exponen son los mismos que ya son legibles arriba.
GRANT SELECT ON movik.maestro_live, movik.v_priority_counts, movik.v_state_counts,
                movik.v_top_parties, movik.v_secured_parties, movik.v_scraper_status
   TO anon, authenticated, service_role;

-- ── Puente a public ─────────────────────────────────────────────────────────
-- Movikapp lee por PostgREST con la anon key, y PostgREST solo sirve los
-- schemas expuestos. Como "movik" no se puede exponer desde la consola, estas
-- vistas en public son la puerta de entrada. Prefijo movik_ para que se vea de
-- dónde salen y no choquen con nada más del proyecto.
--
-- security_invoker = on: la vista se ejecuta con los permisos de quien
-- consulta, no con los de postgres. Así siguen aplicando las políticas RLS de
-- arriba en vez de saltárselas. Requiere Postgres 15+ (Supabase lo es).

DROP VIEW IF EXISTS public.movik_maestro         CASCADE;
DROP VIEW IF EXISTS public.movik_ucc_filings     CASCADE;
DROP VIEW IF EXISTS public.movik_carriers        CASCADE;
DROP VIEW IF EXISTS public.movik_priority_counts CASCADE;
DROP VIEW IF EXISTS public.movik_state_counts    CASCADE;
DROP VIEW IF EXISTS public.movik_top_parties     CASCADE;
DROP VIEW IF EXISTS public.movik_secured_parties CASCADE;
DROP VIEW IF EXISTS public.movik_scraper_status  CASCADE;

-- maestro_live y no maestro: la app necesita `days` calculado al día de hoy.
CREATE VIEW public.movik_maestro WITH (security_invoker = on) AS
  SELECT * FROM movik.maestro_live;
CREATE VIEW public.movik_ucc_filings WITH (security_invoker = on) AS
  SELECT * FROM movik.ucc_filings;
CREATE VIEW public.movik_carriers WITH (security_invoker = on) AS
  SELECT * FROM movik.carriers;
CREATE VIEW public.movik_priority_counts WITH (security_invoker = on) AS
  SELECT * FROM movik.v_priority_counts;
CREATE VIEW public.movik_state_counts WITH (security_invoker = on) AS
  SELECT * FROM movik.v_state_counts;
CREATE VIEW public.movik_top_parties WITH (security_invoker = on) AS
  SELECT * FROM movik.v_top_parties;
CREATE VIEW public.movik_secured_parties WITH (security_invoker = on) AS
  SELECT * FROM movik.v_secured_parties;
CREATE VIEW public.movik_scraper_status WITH (security_invoker = on) AS
  SELECT * FROM movik.v_scraper_status;

GRANT SELECT ON public.movik_maestro, public.movik_ucc_filings,
                public.movik_carriers, public.movik_priority_counts,
                public.movik_state_counts, public.movik_top_parties,
                public.movik_secured_parties, public.movik_scraper_status
   TO anon, authenticated, service_role;

NOTIFY pgrst, 'reload schema';
