"""
movik/census_schema.py
======================
Fuente unica de verdad de las columnas del censo FMCSA que guardamos.

El censo trae 147 columnas. Guardamos 145: todas menos phy_barrio y
mail_barrio, que estan 100% vacias (son campos de Puerto Rico, 0 filas con dato
en los tres estados del proyecto).

Antes esta lista estaba duplicada en ucc_scraper.py, 05_load_carriers_db.py y
load_to_supabase.py. Con 145 nombres eso era una fuga de errores garantizada:
basta que una copia quede desalineada para que el INSERT meta valores en la
columna equivocada. Ahora los tres importan de aqui.

TIPOS
-----
INT_COLS son cantidades: millaje, conteos de equipo, de conductores, radio de
operacion. Sobre esas se hacen sumas y comparaciones.

Todo lo demas es texto, incluso lo que "parece" numero. dun_bradstreet_no,
docket1, phy_cnty, business_org_id y las fechas YYYYMMDD parsean como entero,
pero son identificadores, codigos y fechas: convertirlos borraria ceros a la
izquierda y el formato original. El equipo los va a revisar antes de decidir
como limpiarlos, asi que se guardan tal cual vienen.
"""

# Las 20 originales, en el orden del esquema historico de la tabla carriers.
ORIGINALES = [
    "dot_number", "legal_name", "dba_name", "phy_state", "phy_city", "phy_street",
    "phy_zip", "phone", "cell_phone", "email_address", "company_officer_1", "power_units",
    "truck_units", "fleetsize", "total_drivers", "classdef", "carrier_operation",
    "safety_rating", "mcs150_date", "status_code",
]

# Las 125 que se agregaron despues, en el orden del CSV del censo.
NUEVAS = [
    "add_date", "dun_bradstreet_no", "phy_omc_region", "safety_inv_terr",
    "business_org_id", "mcs150_mileage", "mcs150_mileage_year", "mcs151_mileage",
    "total_cars", "mcs150_update_code_id", "prior_revoke_flag", "prior_revoke_dot_number",
    "fax", "company_officer_2", "business_org_desc", "bus_units", "review_id",
    "recordable_crash_rate", "mail_nationality_indicator", "phy_nationality_indicator",
    "carship", "docket1prefix", "docket1", "docket2prefix", "docket2", "docket3prefix",
    "docket3", "pointnum", "total_intrastate_drivers", "mcsipstep", "mcsipdate", "hm_ind",
    "interstate_beyond_100_miles", "interstate_within_100_miles",
    "intrastate_beyond_100_miles", "intrastate_within_100_miles", "total_cdl",
    "avg_drivers_leased_per_month", "phy_country", "phy_cnty", "carrier_mailing_street",
    "carrier_mailing_state", "carrier_mailing_city", "carrier_mailing_country",
    "carrier_mailing_zip", "carrier_mailing_cnty", "carrier_mailing_und_date",
    "driver_inter_total", "review_type", "review_date", "safety_rating_date",
    "undeliv_phy", "crgo_genfreight", "crgo_household", "crgo_metalsheet", "crgo_motoveh",
    "crgo_drivetow", "crgo_logpole", "crgo_bldgmat", "crgo_mobilehome", "crgo_machlrg",
    "crgo_produce", "crgo_liqgas", "crgo_intermodal", "crgo_passengers", "crgo_oilfield",
    "crgo_livestock", "crgo_grainfeed", "crgo_coalcoke", "crgo_meat", "crgo_garbage",
    "crgo_usmail", "crgo_chem", "crgo_drybulk", "crgo_coldfood", "crgo_beverages",
    "crgo_paperprod", "crgo_utility", "crgo_farmsupp", "crgo_construct", "crgo_waterwell",
    "crgo_cargoothr", "crgo_cargoothr_desc", "owntruck", "owntract", "owntrail",
    "owncoach", "ownschool_1_8", "ownschool_9_15", "ownschool_16", "ownbus_16",
    "ownvan_1_8", "ownvan_9_15", "ownlimo_1_8", "ownlimo_9_15", "ownlimo_16", "trmtruck",
    "trmtract", "trmtrail", "trmcoach", "trmschool_1_8", "trmschool_9_15", "trmschool_16",
    "trmbus_16", "trmvan_1_8", "trmvan_9_15", "trmlimo_1_8", "trmlimo_9_15", "trmlimo_16",
    "trptruck", "trptract", "trptrail", "trpcoach", "trpschool_1_8", "trpschool_9_15",
    "trpschool_16", "trpbus_16", "trpvan_1_8", "trpvan_9_15", "trplimo_1_8",
    "trplimo_9_15", "trplimo_16", "docket1_status_code", "docket2_status_code",
    "docket3_status_code",
]

# Orden definitivo de la tabla carriers.
CARRIER_COLS = ORIGINALES + NUEVAS

# Cantidades. El resto va como texto.
INT_COLS = {
    "avg_drivers_leased_per_month", "bus_units", "driver_inter_total", "fleetsize",
    "interstate_beyond_100_miles", "interstate_within_100_miles",
    "intrastate_beyond_100_miles", "intrastate_within_100_miles", "mcs150_mileage",
    "mcs150_mileage_year", "mcs151_mileage", "ownbus_16", "owncoach", "ownlimo_16",
    "ownlimo_1_8", "ownlimo_9_15", "ownschool_16", "ownschool_1_8", "ownschool_9_15",
    "owntract", "owntrail", "owntruck", "ownvan_1_8", "ownvan_9_15", "power_units",
    "recordable_crash_rate", "total_cars", "total_cdl", "total_drivers",
    "total_intrastate_drivers", "trmbus_16", "trmcoach", "trmlimo_16", "trmlimo_1_8",
    "trmlimo_9_15", "trmschool_16", "trmschool_1_8", "trmschool_9_15", "trmtract",
    "trmtrail", "trmtruck", "trmvan_1_8", "trmvan_9_15", "trpbus_16", "trpcoach",
    "trplimo_16", "trplimo_1_8", "trplimo_9_15", "trpschool_16", "trpschool_1_8",
    "trpschool_9_15", "trptract", "trptrail", "trptruck", "trpvan_1_8", "trpvan_9_15",
    "truck_units",
}


def sql_type(col: str) -> str:
    """Tipo SQL de una columna (sirve para SQLite y para Postgres).

    BIGINT y no INTEGER: el censo trae basura de captura que desborda los 32
    bits de Postgres. mcs150_mileage tiene dos filas con 2.500 y 3.500 millones
    de millas anuales — imposibles, pero el dato se guarda crudo para que el
    equipo decida cómo limpiarlo. Con INTEGER, esas dos filas tumbaban la carga
    entera de 532.167.
    """
    return "BIGINT" if col in INT_COLS else "TEXT"


assert len(CARRIER_COLS) == len(set(CARRIER_COLS)), "columnas duplicadas"
assert INT_COLS <= set(CARRIER_COLS), "INT_COLS tiene columnas que no estan en CARRIER_COLS"
