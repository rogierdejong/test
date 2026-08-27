"""PostgreSQL integration for Tesla inventory data.

Stores scraped vehicles in tesla_used / tesla_new tables,
with INSERT (not UPSERT) so repeated scrapes track price changes over time.

Connection settings come from the environment (.env in the project root):
POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER, POSTGRES_DB, POSTGRES_PASSWORD.
Every row also records the market it was scraped from (NL by default), so one
database can hold multiple countries.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

from tesla_mcp.config import REGION

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")
_HOST = os.getenv("POSTGRES_HOST", "localhost")
_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
_USER = os.getenv("POSTGRES_USER", "postgres")
_DB = os.getenv("POSTGRES_DB", "tesla")


def _dsn(db: str | None = None) -> str:
    return f"postgresql://{_USER}:{_PASSWORD}@{_HOST}:{_PORT}/{db or _DB}"


# Map API camelCase field names → DB snake_case columns
_FIELD_TO_COLUMN = {
    "VIN": "vin",
    "Year": "year",
    "Model": "model",
    "TrimName": "trim_name",
    "TotalPrice": "total_price",
    "Odometer": "odometer",
    "ActualRange": "actual_range",
    "PAINT": "paint",
    "INTERIOR": "interior",
    "WHEELS": "wheels",
    "City": "city",
    "StateProvince": "state_province",
    "CountryCode": "country_code",
    "MetroName": "metro_name",
    "CurrencyCode": "currency_code",
    "OdometerType": "odometer_type",
    "Price": "price",
    "PurchasePrice": "purchase_price",
    "RegistrationDate": "registration_date",
    "FactoryGatedDate": "factory_gated_date",
    "FirstRegistrationDate": "first_registration_date",
    "VehicleHistory": "vehicle_history",
    "PriceAdjustmentUsed": "price_adjustment_used",
    "DamageDisclosure": "damage_disclosure",
    "DamageDisclosureStatus": "damage_disclosure_status",
    "CPORefurbishmentStatus": "cpo_refurbishment_status",
    "AcquisitionSubType": "acquisition_sub_type",
    "FleetVehicle": "fleet_vehicle",
    "IsDemo": "is_demo",
    "VehicleSubType": "vehicle_sub_type",
    "TitleSubtype": "title_subtype",
    "AUTOPILOT": "autopilot",
    "TransportationFee": "transportation_fee",
    "HasVehiclePhotos": "has_vehicle_photos",
}

_COLUMNS = list(_FIELD_TO_COLUMN.values())

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS {table} (
    id                          SERIAL PRIMARY KEY,
    vin                         VARCHAR(50) NOT NULL,
    year                        SMALLINT,
    model                       VARCHAR(10),
    trim_name                   VARCHAR(100),
    total_price                 INTEGER,
    odometer                    INTEGER,
    actual_range                INTEGER,
    paint                       VARCHAR(50),
    interior                    VARCHAR(50),
    wheels                      VARCHAR(50),
    city                        VARCHAR(100),
    state_province              VARCHAR(10),
    country_code                VARCHAR(5),
    metro_name                  VARCHAR(100),
    currency_code               VARCHAR(5),
    odometer_type               VARCHAR(10),
    price                       INTEGER,
    purchase_price              INTEGER,
    registration_date           TIMESTAMPTZ,
    factory_gated_date          TIMESTAMPTZ,
    first_registration_date     TIMESTAMPTZ,
    vehicle_history             VARCHAR(50),
    price_adjustment_used       INTEGER,
    damage_disclosure           BOOLEAN,
    damage_disclosure_status    VARCHAR(50),
    cpo_refurbishment_status    VARCHAR(50),
    acquisition_sub_type        VARCHAR(50),
    fleet_vehicle               BOOLEAN,
    is_demo                     BOOLEAN,
    vehicle_sub_type            VARCHAR(100),
    title_subtype               VARCHAR(50),
    autopilot                   VARCHAR(100),
    transportation_fee          INTEGER,
    has_vehicle_photos          BOOLEAN,
    market                      VARCHAR(5),
    scraped_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_CREATE_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_{table}_vin ON {table} (vin);
CREATE INDEX IF NOT EXISTS idx_{table}_scraped_at ON {table} (scraped_at);
CREATE INDEX IF NOT EXISTS idx_{table}_total_price ON {table} (total_price);
CREATE INDEX IF NOT EXISTS idx_{table}_market ON {table} (market);
"""


async def ensure_database() -> None:
    """Create the 'tesla' database if it doesn't exist."""
    conn = await asyncpg.connect(_dsn("postgres"))
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", _DB
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{_DB}"')
    finally:
        await conn.close()


# Columns added for the European markets — applied to pre-existing tables too.
_EU_COLUMNS = {
    "country_code": "VARCHAR(5)",
    "metro_name": "VARCHAR(100)",
    "currency_code": "VARCHAR(5)",
    "odometer_type": "VARCHAR(10)",
    "price": "INTEGER",
    "purchase_price": "INTEGER",
    "registration_date": "TIMESTAMPTZ",
    "market": "VARCHAR(5)",
}


async def ensure_tables(conn: asyncpg.Connection) -> None:
    """Create tesla_used and tesla_new tables if they don't exist."""
    for table in ("tesla_used", "tesla_new"):
        await conn.execute(_CREATE_TABLE_SQL.format(table=table))
        await conn.execute(_CREATE_INDEXES_SQL.format(table=table))
        # Tables from an earlier (US-only) version lack the EU columns.
        for column, coltype in _EU_COLUMNS.items():
            await conn.execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {coltype}"
            )


def _parse_timestamp(val: str | None) -> datetime | None:
    """Parse ISO-ish timestamp strings from Tesla API."""
    if not val:
        return None
    try:
        # Tesla uses formats like "2024-06-15T00:00:00.000Z"
        cleaned = val.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned)
    except (ValueError, TypeError):
        return None


def _convert_vehicle(v: dict) -> dict:
    """Convert a slim vehicle dict to DB-ready values."""
    row: dict = {}
    for api_field, col in _FIELD_TO_COLUMN.items():
        val = v.get(api_field)

        # Flatten lists to comma-joined strings
        if isinstance(val, list):
            val = ", ".join(str(x) for x in val)

        # Type coercion by column
        if col in ("factory_gated_date", "first_registration_date",
                   "registration_date"):
            val = _parse_timestamp(val)
        elif col in ("year", "total_price", "odometer", "actual_range",
                      "price_adjustment_used", "transportation_fee",
                      "price", "purchase_price"):
            try:
                val = int(val) if val is not None else None
            except (ValueError, TypeError):
                val = None
        elif col in ("damage_disclosure", "fleet_vehicle", "is_demo",
                      "has_vehicle_photos"):
            if isinstance(val, str):
                val = val.lower() in ("true", "1", "yes")
            elif not isinstance(val, bool):
                val = None

        row[col] = val
    return row


async def insert_vehicles(
    vehicles: list[dict],
    condition: str,
    scraped_at: datetime | None = None,
    market: str | None = None,
) -> int:
    """Insert vehicles into the appropriate table. Returns row count."""
    if not vehicles:
        return 0

    table = "tesla_used" if condition == "used" else "tesla_new"
    if scraped_at is None:
        scraped_at = datetime.now(timezone.utc)
    if market is None:
        market = REGION.market

    await ensure_database()

    conn = await asyncpg.connect(_dsn())
    try:
        await ensure_tables(conn)

        cols_with_ts = _COLUMNS + ["market", "scraped_at"]
        placeholders = ", ".join(f"${i+1}" for i in range(len(cols_with_ts)))
        col_names = ", ".join(cols_with_ts)
        sql = f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})"

        rows = []
        for v in vehicles:
            converted = _convert_vehicle(v)
            row = [converted.get(c) for c in _COLUMNS] + [market, scraped_at]
            rows.append(row)

        await conn.executemany(sql, rows)
        return len(rows)
    finally:
        await conn.close()
