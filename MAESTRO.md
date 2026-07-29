# `movik.maestro` — diccionario de columnas

Una fila por carrier. **162 columnas**: las 145 del censo FMCSA más 17 que salen
del cruce con los registros UCC.

Es la tabla que hay que consultar para casi todo: ya viene cruzada, no requiere
joins y trae la prioridad calculada.

> **Sobre `days`:** no está en `movik.maestro` sino en la vista
> `movik.maestro_live`, que es esta tabla más esa columna calculada contra la
> fecha de hoy. Para cualquier cosa que involucre vencimientos, consultá la
> vista. Ver la sección [Vencimientos](#7-vencimientos-y-alertas).

**Convención de llenado:** los porcentajes son sobre los 532.167 carriers de
FL + CA + NC. Una columna al 2 % no está rota: el censo de FMCSA es un
formulario donde casi todo es opcional.

---

## 1. Identidad

| Columna | Tipo | Llenado | Qué es | Para qué sirve |
|---|---|---:|---|---|
| `dot_number` | text (PK) | 100 % | Número USDOT. **El identificador del sector** | Clave para cruzar con cualquier otra fuente. Es lo que se manda a GHL como ID externo |
| `legal_name` | text | 100 % | Razón social registrada | El nombre con el que se busca en el registro UCC |
| `dba_name` | text | 26 % | Nombre comercial ("doing business as") | El nombre con el que **realmente** se los conoce. Úsalo en el saludo de la llamada |
| `search_name` | text | 100 % | `UPPER(legal_name)` | Búsquedas sin preocuparse por mayúsculas |
| `docket1` + `docket1prefix` | text | 30 % | Número de docket y su prefijo (`MC`, `FF`, `MX`) | **El número MC.** El segundo identificador del sector; sin él no se cruza con brokers ni con bases de seguros |
| `docket1_status_code` | text | 30 % | `A` activo · `I` inactivo · `P` pendiente | Un MC inactivo es señal de que dejó de operar bajo esa autoridad |
| `docket2*`, `docket3*` | text | 1,4 % / 0,1 % | Dockets adicionales | Casos raros: empresas con varias autoridades |
| `dun_bradstreet_no` | text | 9 % | Número D&B | Enganche a historial crediticio comercial |
| `business_org_id` / `business_org_desc` | text | 1,6 % | Forma jurídica (LLC, Corp, Sole Proprietor) | Casi vacío. **Ojo:** el Excel lo exporta como "Org Type" y ahí engaña |
| `carship` | text | 100 % | Códigos separados por `;` (`F;C;I;S`) | Tipos de operación combinados. Requiere decodificar |

---

## 2. Contacto

| Columna | Tipo | Llenado | Qué es | Para qué sirve |
|---|---|---:|---|---|
| `phone` | text | 98 % | Teléfono principal | **El canal principal.** Casi universal, pero filtrá igual por `phone <> ''` |
| `cell_phone` | text | 37 % | Celular | Cuando existe, mejor tasa de contacto que el fijo |
| `fax` | text | 15 % | Fax | Sin uso comercial hoy |
| `email_address` | text | 65 % | Correo | Campañas de email; ojo que muchos son del contador, no del dueño |
| `company_officer_1` | text | 93 % | Nombre del titular | **Con quién preguntar al llamar.** Cambia "¿está el dueño?" por un nombre |
| `company_officer_2` | text | 11 % | Segundo titular | El plan B cuando el primero no atiende |

---

## 3. Ubicación

| Columna | Tipo | Llenado | Qué es | Para qué sirve |
|---|---|---:|---|---|
| `phy_street`, `phy_city`, `phy_state`, `phy_zip` | text | 100 % | Dirección física | Asignación territorial, rutas de visita |
| `phy_cnty` | text | 100 % | Código de condado | Territorios comerciales más finos que el estado |
| `phy_country` | text | 100 % | País | Casi todo `US`; hay algunos extranjeros |
| `carrier_mailing_*` (6 columnas) | text | 100 % | Dirección postal | **Distinta de la física.** Es a donde llega el correo — la buena para correo directo |
| `carrier_mailing_und_date` | text | 1,2 % | Fecha de correo no entregado | Señal de dato viejo |
| `undeliv_phy` | text | 0,9 % | `U` no entregable · `C` corregida · `N` normal | **Calidad de dato.** Un `U` es dirección muerta |
| `phy_omc_region`, `safety_inv_terr` | text | 4 % | Región administrativa FMCSA | Uso interno del regulador, poco valor comercial |
| `mail_nationality_indicator`, `phy_nationality_indicator` | text | 0,1 % | Indicador de nacionalidad | Prácticamente vacío |

---

## 4. Tamaño del negocio

Esta es la sección que más importa para calificar un lead.

| Columna | Tipo | Llenado | Qué es | Para qué sirve |
|---|---|---:|---|---|
| `units` | bigint | 100 % | `COALESCE(power_units, truck_units, 0)` | **El campo de segmentación por defecto.** Ya viene resuelto |
| `power_units` | bigint | 99,9 % | Vehículos motorizados | La medida estándar de tamaño de flota |
| `truck_units` | bigint | 100 % | Camiones | |
| `fleetsize` | bigint | 20 % | Tamaño declarado de flota | **Solo 1 de cada 5 lo declara.** Por eso `units` cae a `power_units` primero |
| `bus_units` | bigint | 98 % | Autobuses | Casi siempre 0 en transporte de carga |
| `total_cars` | bigint | 1 % | Automóviles | |
| `mcs150_mileage` | bigint | 49 % | **Millaje anual declarado** | **El mejor proxy de facturación del censo.** Un carrier de 5 camiones con 800.000 millas factura mucho más que uno de 5 con 90.000 |
| `mcs150_mileage_year` | bigint | 47 % | Año de ese millaje | Sin esto, el millaje no se puede comparar entre empresas |
| `mcs151_mileage` | bigint | 4 % | Millaje del formulario MCS-151 | |
| `total_drivers` | bigint | 95 % | Conductores | |
| `total_cdl` | bigint | 90 % | **Conductores con licencia CDL** | Más fiable que `total_drivers` para medir capacidad real |
| `total_intrastate_drivers` | bigint | 100 % | Conductores intraestatales | |
| `driver_inter_total` | bigint | 70 % | Conductores interestatales | |
| `avg_drivers_leased_per_month` | bigint | 71 % | Conductores arrendados al mes | Alto = modelo de owner-operators, otro perfil de crédito |

### Equipo propio vs. arrendado — lo más importante para prestar

| Prefijo | Significa | Columnas |
|---|---|---|
| `own*` | **Propio** | `owntruck` `owntract` `owntrail` `owncoach` `ownvan_*` `ownbus_16` `ownschool_*` `ownlimo_*` |
| `trm*` | Arrendado **a término** | mismos sufijos |
| `trp*` | Arrendado **por viaje** | mismos sufijos |

Sufijos: `truck` camión · `tract` tractocamión · `trail` remolque · `coach`
autobús · `van_1_8` / `van_9_15` furgoneta por capacidad · `school_*` escolar ·
`limo_*` limusina · `bus_16` autobús de 16+.

**Por qué importa:** un UCC grava activos, y **solo se pueden gravar los
propios**. `power_units` mezcla las tres modalidades, así que no distingue a
quien tiene 30 camiones suyos —30 activos gravables— de quien los alquila
todos y no tiene nada que ofrecer como garantía.

```sql
-- Carriers con activos propios reales y sin gravamen
SELECT dot_number, legal_name, phone,
       COALESCE(owntruck,0) + COALESCE(owntract,0) + COALESCE(owntrail,0) AS propios
  FROM movik.maestro
 WHERE priority = 'P1'
   AND COALESCE(owntruck,0) + COALESCE(owntract,0) + COALESCE(owntrail,0) >= 5
 ORDER BY propios DESC;
```

Llenado: `owntruck` 47 % · `owntract` 31 % · `owntrail` 24 % · el resto (buses,
limusinas, escolares) por debajo del 1 %, porque Movik vende a transporte de
carga.

---

## 5. Operación y carga

| Columna | Tipo | Llenado | Qué es | Para qué sirve |
|---|---|---:|---|---|
| `carrier_operation` | text | 100 % | `A` interestatal · `B` intraestatal con hazmat · `C` intraestatal sin hazmat | Un carrier interestatal es un negocio más grande y más regulado |
| `classdef` | text | 100 % | Clasificaciones separadas por `;` ("AUTHORIZED FOR HIRE", "PRIVATE PROPERTY"…) | *For hire* = transporta para terceros, es el cliente típico. *Private property* = flota propia de otra empresa |
| `hm_ind` | text | 100 % | `Y` / `N` — transporta materiales peligrosos | Hazmat implica seguros y márgenes mayores |
| `interstate_beyond_100_miles` | bigint | 83 % | Conductores interestatales a más de 160 km | **Radio de operación.** Larga distancia = otro perfil de flota y de financiamiento |
| `interstate_within_100_miles` | bigint | 81 % | Interestatales dentro de 160 km | |
| `intrastate_beyond_100_miles` | bigint | 83 % | Intraestatales a más de 160 km | |
| `intrastate_within_100_miles` | bigint | 86 % | Intraestatales dentro de 160 km | Operación local |

### Tipos de carga — las 28 columnas `crgo_*`

Son **banderas**: valen `'X'` si transporta ese tipo, `NULL` si no.

| Columna | Carga | Llenado |
|---|---|---:|
| `crgo_genfreight` | Carga general | 41 % |
| `crgo_cargoothr` + `crgo_cargoothr_desc` | Otra (con descripción libre) | 15 % |
| `crgo_construct` | Construcción | 10 % |
| `crgo_bldgmat` | Materiales de construcción | 9 % |
| `crgo_motoveh` | Vehículos | 5 % |
| `crgo_produce` / `crgo_coldfood` / `crgo_meat` / `crgo_beverages` | Alimentos y refrigerados | 5 % / 4 % / 2 % / 3 % |
| `crgo_machlrg` | Maquinaria pesada | 5 % |
| `crgo_logpole` | Madera y postes | 4 % |
| `crgo_paperprod` / `crgo_garbage` / `crgo_household` | Papel · basura · mudanzas | 3 % c/u |
| `crgo_metalsheet` / `crgo_intermodal` / `crgo_drivetow` | Metal · intermodal · grúa | 3 % / 3 % / 2 % |
| `crgo_grainfeed` / `crgo_farmsupp` / `crgo_livestock` | Grano · insumos agrícolas · ganado | 2 % / 3 % / 1 % |
| `crgo_drybulk` / `crgo_liqgas` / `crgo_chem` | Granel seco · gas licuado · químicos | 2 % / 1 % / 0,5 % |
| `crgo_utility` / `crgo_waterwell` / `crgo_oilfield` / `crgo_coalcoke` | Servicios · pozos · petróleo · carbón | 2 % / 0,3 % / 0,3 % / 0,2 % |
| `crgo_passengers` / `crgo_usmail` / `crgo_mobilehome` | Pasajeros · correo · casas móviles | 2 % / 0,7 % / 0,4 % |

```sql
-- Refrigerados de Florida sin gravamen: equipo caro, buen candidato
SELECT dot_number, legal_name, phone, units
  FROM movik.maestro
 WHERE priority = 'P1' AND phy_state = 'FL'
   AND (crgo_coldfood = 'X' OR crgo_produce = 'X' OR crgo_meat = 'X');
```

---

## 6. Historial y riesgo

| Columna | Tipo | Llenado | Qué es | Para qué sirve |
|---|---|---:|---|---|
| `add_date` | text `YYYYMMDD` | 100 % | Alta en FMCSA | **Antigüedad de la empresa.** Un carrier de 15 años no es el mismo riesgo que uno de 6 meses |
| `mcs150_date` | text | 72 % | Última actualización del MCS-150 | **Un MCS-150 de hace 3 años sugiere empresa inactiva o descuidada.** Vale como filtro de calidad |
| `status_code` | text | 100 % | Siempre `A` — solo cargamos activos | Constante en esta tabla |
| `prior_revoke_flag` | text | 27 % | `Y` / `N` — se le revocó la autoridad antes | **Señal de riesgo fuerte** |
| `prior_revoke_dot_number` | text | 0,6 % | DOT anterior revocado | Reincidencia bajo otra identidad |
| `safety_rating` | text | 0,9 % | `S` satisfactorio · `C` condicional · `U` insatisfactorio | **Casi vacío**: FMCSA audita a muy pocos. No sirve para filtrar |
| `safety_rating_date` | text | 0,9 % | Fecha de esa calificación | Una calificación de 2011 dice poco de hoy |
| `review_date` / `review_type` / `review_id` | text | ~1 % | Última auditoría | |
| `recordable_crash_rate` | bigint | 15 % | Índice de accidentes registrables | Riesgo operativo |
| `mcsipstep` / `mcsipdate` | text | 7,5 % | Etapa del programa MCSIP | Intervención del regulador en curso |
| `mcs150_update_code_id` | text | 4 % | Código de actualización | Uso interno |
| `pointnum` | text | 1,3 % | Identificador de punto | Uso interno |

---

## 7. Vencimientos y alertas

Estas 17 columnas **no vienen del censo**: las calcula `load_to_supabase.py` al
cruzar con los registros UCC.

| Columna | Tipo | Qué es | Para qué sirve |
|---|---|---|---|
| `priority` | text | `P1` `P2a` `P2b` `P2c` `P3` `P4` `Error` `Sin datos` | **La columna que define la acción comercial.** Ver [DATOS.md](DATOS.md) |
| `prio_rank` | smallint | P1=0 P2b=1 P2c=2 P2a=3 P3=4 P4=5 otros=9 | Ordenar sin `CASE`. `prio_rank < 9` = leads accionables |
| `ucc_found` | text | `Sí` · `No` · `Error` · `Sin datos (ST)` | Versión legible de lo anterior |
| `ucc_number` | text | Número del gravamen representativo | Referencia al consultar el registro |
| `date_filed` | text `MM/DD/YYYY` | Cuándo se registró | Antigüedad de la relación con el acreedor |
| `expires_date` | text `MM/DD/YYYY` | Cuándo vence | Legible; para ordenar usá la siguiente |
| `expires_date_iso` | date | La misma fecha como `date` | **Ordená y filtrá por esta.** Es la que tiene índice |
| `days_to_expiry` | integer | Días al vencimiento **congelados el día del scrape** | ⚠️ Envejece. Usá `days` de `maestro_live` |
| `secured_party` | text | Quién tiene el gravamen | **A quién le está pagando hoy.** Es la competencia |
| `secured_party_type` | text | `factor` · `trust` · `bank` · `other` | Un carrier con factor tiene otro perfil que uno con préstamo SBA |
| `filing_status` | text | `Filed` · `Lapsed` | `Lapsed` = venció |
| `state_registry` | text | `FL` · `CA` | En qué registro se encontró |
| `n_filings` | integer | Cuántos gravámenes tiene | >1 significa varios acreedores: más apalancado |
| `scraped_at` | text | Cuándo se consultó | Antigüedad del dato |
| `notes` | text | Contexto ("3 filings (1 filed, 2 lapsed)") | |
| `units` | bigint | Ver sección 4 | |
| `search_name` | text | Ver sección 1 | |

### La columna `days` está en la vista, no en la tabla

```sql
-- ✅ correcto
SELECT dot_number, legal_name, days FROM movik.maestro_live WHERE days BETWEEN 0 AND 30;

-- ❌ mal: days_to_expiry se congeló el día del scrape
SELECT dot_number, legal_name, days_to_expiry FROM movik.maestro WHERE days_to_expiry BETWEEN 0 AND 30;
```

Un carrier scrapeado hace tres meses con `days_to_expiry = 45` **ya venció**.

### El gravamen representativo

Cuando un carrier tiene varios (`n_filings > 1`), estas columnas describen
**uno solo**: el activo que vence antes; si no hay activos, el que venció más
recientemente. Para ver todos, ir a `movik.ucc_filings` por `dot_number`.

---

## 8. Recetas

**Lo que hay que llamar hoy, ordenado por urgencia:**

```sql
SELECT dot_number, legal_name, dba_name, company_officer_1, phone,
       phy_city, phy_state, units, priority, days, secured_party
  FROM movik.maestro_live
 WHERE prio_rank < 9
   AND phone IS NOT NULL AND phone <> ''
 ORDER BY prio_rank, expires_date_iso NULLS LAST, legal_name, dot_number
 LIMIT 200;
```

**El lead ideal — sin gravamen, con activos propios y facturación real:**

```sql
SELECT dot_number, legal_name, company_officer_1, phone,
       COALESCE(owntruck,0) + COALESCE(owntract,0) AS propios,
       mcs150_mileage, total_cdl, add_date
  FROM movik.maestro
 WHERE priority = 'P1'
   AND COALESCE(owntruck,0) + COALESCE(owntract,0) >= 3
   AND mcs150_mileage > 100000
   AND add_date < '20230101'              -- al menos ~3 años operando
   AND COALESCE(prior_revoke_flag,'N') <> 'Y'
   AND phone IS NOT NULL
 ORDER BY mcs150_mileage DESC;
```

**Se le vence el gravamen y ya sabemos a quién le paga:**

```sql
SELECT dot_number, legal_name, phone, secured_party, secured_party_type, days
  FROM movik.maestro_live
 WHERE priority IN ('P2b', 'P2c') AND days BETWEEN 0 AND 60
 ORDER BY days;
```

**Datos sucios que conviene excluir de cualquier campaña:**

```sql
SELECT COUNT(*) FILTER (WHERE phone IS NULL OR phone = '')      AS sin_telefono,
       COUNT(*) FILTER (WHERE undeliv_phy = 'U')                AS direccion_muerta,
       COUNT(*) FILTER (WHERE mcs150_date < '20220101')         AS mcs150_viejo,
       COUNT(*) FILTER (WHERE prior_revoke_flag = 'Y')          AS autoridad_revocada
  FROM movik.maestro
 WHERE priority = 'P1';
```

---

## 9. Notas para quien vaya a limpiar

- **Las fechas son texto `YYYYMMDD`**, no `date`: `add_date`, `mcs150_date`,
  `review_date`, `safety_rating_date`, `mcsipdate`, `carrier_mailing_und_date`.
  Se dejaron así a propósito, sin convertir, para no perder el valor original.
  Comparan bien como cadena (`add_date < '20230101'` funciona) porque el formato
  es de ancho fijo y ordenable.
- **Los identificadores son texto aunque parezcan números**: `dun_bradstreet_no`,
  `docket1`, `phy_cnty`, `business_org_id`, `prior_revoke_dot_number`.
  Convertirlos a entero borraría ceros a la izquierda.
- **Las `crgo_*` son `'X'` o `NULL`**, no `true`/`false`. Un `= 'X'` funciona;
  un `IS TRUE` no.
- **`classdef` y `carship` traen varios valores separados por `;`** en un solo
  campo. Para filtrar por uno, `LIKE '%AUTHORIZED FOR HIRE%'`.
- **Las columnas de limusinas, autobuses y transporte escolar** (`ownlimo_*`,
  `trmschool_*`, `trpbus_16`…) tienen entre 3 y 800 filas con dato en 532.167.
  Se cargaron por completitud; para transporte de carga son ruido.
- **`phy_barrio` y `mail_barrio` no están**: son campos de Puerto Rico y venían
  100 % vacías en los tres estados del proyecto.
