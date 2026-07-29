# Movik — la data y las alertas

Qué hay en la base, de dónde sale cada número y cómo leer las prioridades.

---

## 1. Qué hace este pipeline

Movik cruza dos fuentes públicas para encontrar transportistas a los que
venderles financiamiento:

1. **El censo de la FMCSA** — todas las empresas de transporte de EE. UU. con
   número DOT: nombre, dirección, teléfono, tamaño de flota, estado operativo.
2. **Los registros UCC estatales** — quién tiene un gravamen (*lien*) sobre los
   activos de cada empresa, desde cuándo y hasta cuándo.

El cruce contesta una pregunta comercial concreta: **¿este transportista tiene
ya un acreedor, y le está por vencer?** Un carrier sin gravamen registrado no le
debe nada a nadie y puede firmar mañana. Uno cuyo gravamen vence en tres
semanas está por quedar libre. Uno con un gravamen vigente a cinco años es
tiempo perdido.

Esa clasificación es lo que llamamos **prioridad**, y es la columna `priority`.

---

## 2. Las prioridades

| Prioridad | Qué significa | Acción comercial |
|---|---|---|
| **P1** | Se consultó el registro y **no hay ningún UCC** a su nombre | Llamar ya. Nadie lo tiene. |
| **P2a** | Tiene UCC pero está **`Lapsed`** (venció, sin fecha exacta) | Llamar ya. El acreedor perdió la garantía. |
| **P2b** | UCC activo que **vence en ≤ 30 días** | Llamar esta semana. |
| **P2c** | UCC activo que **vence en 31–60 días** | Contactar pronto. |
| **P3** | UCC activo que **vence en 61–365 días** | Seguimiento; agendar. |
| **P4** | UCC activo a **más de 365 días** o sin fecha | Competencia vigente. Baja prioridad. |
| **Error** | La consulta al registro falló | No es un lead. Reintentar. |
| **Sin datos** | **Nunca se consultó** el registro | No es un lead *todavía*. |

`prio_rank` es la misma escala en número, para ordenar sin `CASE`:

```
P1=0   P2b=1   P2c=2   P2a=3   P3=4   P4=5   Error=9   Sin datos=9
```

Nótese que **P2b y P2c van antes que P2a**. Un gravamen que vence en 20 días es
una oportunidad con fecha; uno que ya venció (`Lapsed`) lleva ahí un tiempo
indeterminado y está más frío.

### La regla que no se puede romper

> **Ausencia de dato ≠ ausencia de UCC.**

Un carrier solo se marca **P1** si de verdad se consultó el registro y no hubo
resultado. Si nunca se consultó, es **"Sin datos"**.

Esto no es un tecnicismo. Hoy hay 418.110 carriers sin consultar. Si se
marcaran como P1, el equipo comercial tendría 484.181 "leads calientes" de los
cuales solo 66.071 lo son de verdad — y no habría forma de distinguirlos. La
lista entera perdería su valor.

La fuente de verdad de "se consultó" es la tabla `scrape_log`, no la presencia
o ausencia de filas en `ucc_filings`.

---

## 3. Qué hay hoy en la base

### Cobertura

| Estado | Carriers | Consultados | Avance |
|---|---:|---:|---:|
| Florida | 153.860 | 105.615 | 68,6 % |
| California | 325.022 | 8.442 | 2,6 % |
| North Carolina | 53.285 | 0 | 0 % |
| **Total** | **532.167** | **114.057** | **21,4 %** |

Solo se incluyen carriers **activos** (`status_code = 'A'`) con nombre legal.
Texas quedó fuera del proyecto.

North Carolina está en 0 % porque **no tiene scraper todavía**. Los carriers
están cargados, pero nadie consultó el registro de NC.

### Prioridades

| Prioridad | Carriers | % de los consultados |
|---|---:|---:|
| P1 — sin UCC | 66.071 | 57,9 % |
| P2a — lapsed | 8.265 | 7,2 % |
| P2b — ≤ 30 días | 412 | 0,4 % |
| P2c — 31–60 días | 332 | 0,3 % |
| P3 — 61–365 días | 3.307 | 2,9 % |
| P4 — > 365 días | 35.612 | 31,2 % |
| Error | 58 | 0,1 % |
| Sin datos | 418.110 | — |

**Casi seis de cada diez carriers consultados no tienen gravamen.** Ese es el
hallazgo comercial del proyecto.

Los P2b y P2c son pocos (744 en total) porque dependen de que el registro
publique la fecha de vencimiento, y **el API de Florida hoy no la da en la
mayoría de los casos** — por eso hay tantos P2a (`Lapsed` sin fecha) en
comparación. Con datos en bruto del registro esa proporción cambiaría.

### Gravámenes

`ucc_filings` tiene **258.883** registros: 231.516 del registro de Florida y
27.367 del de California.

Por tipo de acreedor (carriers distintos; uno puede tener varios):

| Tipo | Carriers | Ejemplos |
|---|---:|---|
| Bancos | 14.372 | U.S. Small Business Administration, Wells Fargo, BofA |
| Trusts / fondos | 3.813 | Valere, Salveo |
| Factoring | 1.164 | RTS, OTR, Apex, eCapital, Triumph |
| Otros | 29.845 | Agentes, personas físicas |

El acreedor más frecuente es la **U.S. Small Business Administration** con
3.784 carriers — préstamos COVID (EIDL) que quedaron registrados.

Cuidado al leer ese ranking: el segundo y el tercero (*First Corporate
Solutions* con 2.676 y *Corporation Service Company* con 2.623, ambos "AS
REPRESENTATIVE") **no son acreedores**, son agentes que presentan el filing en
nombre de otro. El acreedor real no aparece en ese campo. Para análisis de
competencia hay que excluirlos.

La clasificación por tipo se hace por palabras clave sobre el nombre. El orden
importa porque las listas se solapan: *factoring* se evalúa antes que *banco*
porque "Triumph Business Capital" es un factor, no un banco.

---

## 4. Las tablas

### `movik.carriers` — 532.167 filas × 145 columnas

Espejo del censo FMCSA, filtrado a los tres estados y a activos.

Se guardan **145 de las 147 columnas** del censo. Las dos que faltan son
`phy_barrio` y `mail_barrio`: campos de Puerto Rico, 0 filas con dato en los
tres estados del proyecto.

El detalle de qué significa cada una está en **[MAESTRO.md](MAESTRO.md)** —
`maestro` incluye estas mismas columnas.

La lista y los tipos viven en `census_schema.py`, que es de donde los leen
`ucc_scraper.py`, `05_load_carriers_db.py` y `load_to_supabase.py`. No hay que
escribirla en ningún otro lado.

### `movik.ucc_filings` — 258.883 filas

Un registro por gravamen. Un carrier puede tener varios.

`id` (PK) · `dot_number` · `legal_name_searched` · `match_found` ·
`alert_priority` · `ucc_number` · `date_filed` · `expires_date` ·
`days_to_expiry` · `secured_party` · `secured_party_addr` ·
`secured_party_type` · `filing_type` · `filing_status` · `state_registry` ·
`scraped_at`

`match_found = 0` significa que se buscó y no se encontró nada — la fila existe
como constancia de la búsqueda.

### `movik.maestro` — 532.167 filas × 162 columnas ⭐

**Una fila por carrier, ya cruzada.** Es la tabla que hay que consultar para
casi todo: no requiere joins y trae la prioridad calculada.

📖 **El diccionario completo, columna por columna, está en
[MAESTRO.md](MAESTRO.md)**: qué significa cada una, cuán llena está y para qué
sirve comercialmente.

Contiene las 145 columnas del censo, más estas 17 del cruce:

| Columna | Qué es |
|---|---|
| `priority` | P1 / P2a / P2b / P2c / P3 / P4 / Error / Sin datos |
| `prio_rank` | La misma escala en número, para ordenar |
| `units` | `COALESCE(power_units, truck_units, 0)` — tamaño de flota |
| `ucc_found` | Sí / No / Error / Sin datos (ST) |
| `ucc_number`, `date_filed`, `expires_date` | Del gravamen representativo |
| `expires_date_iso` | La misma fecha como `date`, para comparar y ordenar |
| `days_to_expiry` | Días al vencimiento **congelados al momento del scrape** |
| `secured_party`, `secured_party_type` | Quién tiene el gravamen |
| `filing_status`, `state_registry` | Estado del filing y registro de origen |
| `n_filings` | Cuántos gravámenes tiene en total |
| `scraped_at` | Cuándo se consultó |
| `notes` | Contexto (p. ej. "3 filings (1 filed, 2 lapsed)") |
| `search_name` | `UPPER(legal_name)`, para búsquedas |

#### El gravamen representativo

Cuando un carrier tiene varios, `maestro` muestra **el activo que vence
antes**. Si no hay ninguno activo, el que venció más recientemente.

Es importante que sea ese y no "el primero que aparezca": si se toma otro, la
fila queda incoherente — un P2b mostrando "563 días" porque la prioridad la
determinó un gravamen y los días otro.

### `movik.maestro_live` — vista

`maestro` más una columna calculada:

```sql
days = COALESCE(expires_date_iso - CURRENT_DATE, days_to_expiry)
```

**Usá `days` de esta vista, no `days_to_expiry` de la tabla.** El segundo se
congeló el día del scrape y envejece: un carrier scrapeado hace tres meses con
`days_to_expiry = 45` en realidad ya venció.

### Vistas de agregados

Materializadas, refrescadas por el loader al terminar cada carga:

| Vista | Contenido |
|---|---|
| `mv_priority_counts` | Carriers por prioridad |
| `mv_state_counts` | Carriers por estado |
| `mv_top_parties` | Acreedores ordenados por carriers distintos |
| `mv_secured_parties` | Cada acreedor con su tipo y su conteo |
| `mv_scraper_status` | Avance del scraper por estado |

Son materializadas porque recalcularlas costaba hasta 3 s por carga de página.

### Puente a `public`

PostgREST (la API REST de Supabase) solo sirve los schemas marcados como
expuestos, y `movik` no se puede exponer en este proyecto. Por eso hay vistas
en `public` que apuntan a las de `movik`:

```
public.movik_maestro          → movik.maestro_live
public.movik_ucc_filings      → movik.ucc_filings
public.movik_carriers         → movik.carriers
public.movik_priority_counts  → movik.mv_priority_counts
public.movik_state_counts     → movik.mv_state_counts
public.movik_top_parties      → movik.mv_top_parties
public.movik_secured_parties  → movik.mv_secured_parties
public.movik_scraper_status   → movik.mv_scraper_status
```

Los datos siguen viviendo en `movik`; `public` solo tiene punteros de solo
lectura, con `security_invoker = on` para que las políticas RLS sigan
aplicando. **Desde la API REST o el cliente de JS hay que usar los nombres
`public.movik_*`.** Desde SQL directo, cualquiera de los dos.

---

## 5. El pipeline

```
01_census_incremental.py   descarga el censo FMCSA      → census_file_full.csv (1,7 GB)
02_transform_export.py     limpia y exporta             → movik_carriers.xlsx
ucc_scraper.py             consulta el registro de FL   → movik.db
ca_ucc_scraper.py          consulta el registro de CA   → movik.db
03_build_master.py         cruza todo                   → movik_maestro.xlsx
load_to_supabase.py        sube las 3 tablas            → Supabase, schema movik
```

`movik.db` (SQLite, ~168 MB) es el estado real del proyecto: acumula lo
scrapeado. Los `.xlsx` son entregables para revisar a mano.

### Los scrapers

`ucc_scraper.py` pega contra el API público del registro de Florida en dos
pasos: búsqueda por nombre, y detalle de cada filing encontrado. Corre con 3
workers, techo de 3 req/s, y en la práctica rinde **~1 carrier/segundo**.

Guarda checkpoint cada 200 carriers, así que un run cortado se retoma donde
quedó. Lo ya hecho sale de `scrape_log` unido al checkpoint JSON — no solo del
JSON, que puede perderse.

`ca_ucc_scraper.py` hace lo propio contra `bizfileonline.sos.ca.gov`, que
resuelve en una sola llamada. Es **CA-only por diseño**: no acepta `--state`.
Correr el scraper de Florida contra carriers de California daría datos sin
sentido, y por eso `ucc_scraper.py` rechaza explícitamente cualquier estado que
no sea FL.

### Automatización

`.github/workflows/scraper_fl.yml` y `scraper_ca.yml` corren los scrapers a
mano (`workflow_dispatch`). `movik.db` viaja entre runs como artifact; si no hay
run previo, se baja del release `db-seed`. Al terminar, cargan a Supabase.

---

## 6. Consultas útiles

**Leads calientes, los más urgentes primero:**

```sql
SELECT dot_number, legal_name, phy_state, phy_city, phone, units,
       priority, days, secured_party
  FROM movik.maestro_live
 WHERE prio_rank < 9                       -- excluye Error y Sin datos
 ORDER BY prio_rank, expires_date_iso NULLS LAST, legal_name, dot_number;
```

Ordená por `expires_date_iso` y no por `days`: dan exactamente el mismo orden
—`days` es esa fecha menos hoy— pero el primero usa índice y el segundo obliga
a ordenar toda la tabla.

**P1 de Florida con flota de 5 a 25 unidades y teléfono:**

```sql
SELECT dot_number, legal_name, phy_city, phone, units
  FROM movik.maestro
 WHERE priority = 'P1'
   AND phy_state = 'FL'
   AND units BETWEEN 5 AND 25
   AND phone IS NOT NULL AND phone <> ''
 ORDER BY units DESC;
```

**Lo que vence en los próximos 30 días:**

```sql
SELECT dot_number, legal_name, phone, secured_party, days
  FROM movik.maestro_live
 WHERE priority IN ('P2b', 'P2c')
   AND days BETWEEN 0 AND 30
 ORDER BY days;
```

**Avance del scraper:**

```sql
SELECT * FROM movik.mv_scraper_status;
```

---

## 7. Integrar con GHL (u otro CRM)

La tabla a leer es `movik.maestro`, filtrando por `priority`.

### ⚠️ Antes de poner un trigger

`load_to_supabase.py` reescribe **las 532.167 filas** en cada carga, con
`INSERT ... ON CONFLICT DO UPDATE`. Un trigger `AFTER INSERT OR UPDATE` sin
condición **se dispararía medio millón de veces por run**, aunque no haya
cambiado nada.

El trigger tiene que reaccionar al **cambio de prioridad**, no a la escritura:

```sql
CREATE OR REPLACE FUNCTION movik.notificar_lead() RETURNS trigger AS $$
BEGIN
  -- Solo lo que vale una llamada.
  IF NEW.priority IN ('P1', 'P2a', 'P2b', 'P2c') THEN
    INSERT INTO movik.leads_salientes (dot_number, priority, enviado_at)
    VALUES (NEW.dot_number, NEW.priority, NULL)
    ON CONFLICT (dot_number) DO UPDATE
      SET priority = EXCLUDED.priority, enviado_at = NULL;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_lead_prioridad
  AFTER INSERT OR UPDATE ON movik.maestro
  FOR EACH ROW
  WHEN (OLD.priority IS DISTINCT FROM NEW.priority)   -- ← la clave
  EXECUTE FUNCTION movik.notificar_lead();
```

Dos decisiones de diseño ahí:

- **`WHEN (OLD.priority IS DISTINCT FROM NEW.priority)`** hace que el trigger
  solo corra cuando la prioridad realmente cambió. En un `INSERT` `OLD` es
  `NULL`, así que también entra el primer alta.
- **Escribe a una tabla intermedia (`leads_salientes`) en vez de llamar a GHL
  directo.** Llamar a una API HTTP desde un trigger mantiene abierta la
  transacción de la carga mientras espera respuesta: si GHL tarda o falla, se
  frena —o se revierte— la carga entera. Con una tabla de salida, un proceso
  aparte lee lo pendiente y lo envía a su ritmo, con reintentos.

Si igual se prefiere llamar directo, `pg_net` hace peticiones asíncronas sin
bloquear la transacción; `http` (síncrono) no sirve para esto.

### Transiciones que importan

Con el trigger por cambio, estos son los eventos que vale la pena enviar:

| Transición | Qué pasó |
|---|---|
| `Sin datos` → `P1` | Se consultó por primera vez y está limpio. **Lead nuevo.** |
| `P4`/`P3` → `P2b`/`P2c` | Se le acerca el vencimiento. **Momento de llamar.** |
| cualquiera → `P2a` | Su gravamen venció. **Quedó libre.** |
| `P1` → `P4` | Alguien más le prestó. **Se perdió.** |

Esa última conviene registrarla aunque no se envíe: mide cuántos leads se
pierden por demora.

---

## 8. Limitaciones conocidas

- **NC no tiene scraper.** Sus 53.285 carriers están en "Sin datos" y ahí
  seguirán hasta que se implemente.
- **CA está al 2,6 %.** El backfill apenas empezó.
- **El API de Florida no da fecha de vencimiento en la mayoría de los filings.**
  Por eso los P2b/P2c son tan pocos frente a los P2a. Resolverlo requiere los
  datos en bruto del registro, no el API de búsqueda.
- **`days_to_expiry` envejece.** Usar `days` de `maestro_live`.
- **Un carrier puede tener gravámenes en varios estados.** `maestro` muestra
  uno solo; para verlos todos hay que ir a `ucc_filings` por `dot_number`.
- **El match es por nombre legal**, no por identificador. Un carrier que cambió
  de razón social puede tener gravámenes que no se le atribuyen.
