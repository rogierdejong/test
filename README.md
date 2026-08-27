# TeslaWebScrape — Nederlandse configuratie

A Tesla inventory scraper built as an [MCP](https://modelcontextprotocol.io/) server, designed to run inside [Claude Code](https://docs.anthropic.com/en/docs/claude-code).

This copy is vendored from [JumpBearCode/TeslaWebScrape](https://github.com/JumpBearCode/TeslaWebScrape) (MIT) and configured for the **Dutch** site — `tesla.com/nl_NL`, market `NL`, prices in EUR, distances in km. Installatie- en configuratiehandleiding in het Nederlands: [`INSTALLATIE.md`](INSTALLATIE.md).

## Why I Built This

I was shopping for a Tesla and got tired of refreshing Tesla's website every day — manually re-selecting the model, year, and filters each time. So I built this tool to scrape Tesla's inventory programmatically and save the results locally or to a database for tracking price changes over time.

## Why Not Playwright?

Tesla's inventory pages are protected by **Akamai Bot Manager**, which is one of the more aggressive bot detection systems out there. Here's what I tried and what happened:

| Approach | Result |
|----------|--------|
| `requests` / `httpx` | 403 Forbidden |
| `cloudscraper` | 403 Forbidden |
| `curl_cffi` alone | 429 with Akamai challenge page |
| Playwright (even with `stealth.js`) | 403 Forbidden |
| Puppeteer + stealth plugin | 403 Forbidden |

**What actually works:** [`nodriver`](https://github.com/nicegui-development/nodriver) — an undetected Chrome automation library. It launches a real Chrome instance that passes Akamai's fingerprinting checks. Once the cookies are acquired through `nodriver`, the actual API calls are made with [`curl_cffi`](https://github.com/lexiforest/curl_cffi), which impersonates Chrome's TLS fingerprint.

### The Two-Step Approach

```
1. nodriver (Chrome)          2. curl_cffi (API calls)
┌─────────────────────┐      ┌─────────────────────────┐
│ Visit tesla.com     │      │ Reuse Akamai cookies     │
│ Wait for Akamai JS  │ ───→ │ Call /inventory/api/v4   │
│ Extract cookies     │      │ Impersonate Chrome TLS   │
│ (~15 sec)           │      │ (~0.5 sec per page)      │
└─────────────────────┘      └─────────────────────────┘
```

Cookies are cached for 10 minutes, so subsequent searches within that window skip the browser step entirely.

## Architecture

This project is an MCP server that exposes tools to Claude Code. You interact with it through natural language via the `/tesla` skill.

```
You (natural language)
  │
  ▼
Claude Code ──→ /tesla skill ──→ MCP Server (tesla-inventory)
                                    ├── acquire_cookies()   ← nodriver
                                    ├── search_top_n()      ← curl_cffi + pagination
                                    ├── merge_results()     ← CSV export
                                    └── save_to_postgres()  ← PostgreSQL storage
```

### Two Storage Modes

1. **Local files** — Results are saved as JSON/CSV in the `results/` directory. Good for quick one-off searches.

2. **PostgreSQL** — Each scrape is inserted (not upserted) as a timestamped snapshot, so you can track price changes over time. Requires a PostgreSQL server. I run mine on a home server.

## MCP Tools

| Tool | Description |
|------|-------------|
| `acquire_cookies` | Launch Chrome via nodriver, bypass Akamai, cache cookies (10-min TTL) |
| `search_inventory` | Single-page API query with filters (model, condition, year, mileage, etc.) |
| `search_top_n` | Auto-paginating search with VIN deduplication — returns top N unique vehicles |
| `merge_results` | Merge multiple raw JSON files into a single CSV |
| `save_to_postgres` | Insert scraped vehicles into PostgreSQL for historical tracking |
| `region_info` | Show the active market, URLs and query payload (NL by default) |

## Supported Models

| Code | Model |
|------|-------|
| `my` | Model Y |
| `m3` | Model 3 |
| `ms` | Model S |
| `mx` | Model X |

Both **used** and **new** inventory are supported.

## Setup

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (package manager)
- Google Chrome installed (for nodriver)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (to use the MCP server)

### Install

```bash
git clone <this-repo>
cd <this-repo>
uv sync
cp .env.example .env          # optional — NL defaults work without it
uv run python -m tesla_mcp.selfcheck
```

`selfcheck` prints the active market and the exact API request that will be
sent, without touching the network. Expect `market: NL`, `language: nl`,
`super_region: europe`.

### Configure MCP Server

Add to your Claude Code MCP config (`.mcp.json`):

```json
{
  "mcpServers": {
    "tesla-inventory": {
      "command": "uv",
      "args": [
        "run",
        "--directory", ".",
        "python", "-m", "tesla_mcp.server"
      ]
    }
  }
}
```

The checked-in `.mcp.json` uses a relative directory, so it works as long as
you start Claude Code from the project root. Starting it elsewhere? Replace
`"."` with the absolute path to this directory.

### Market Configuration

The market lives in [`tesla_mcp/config.py`](tesla_mcp/config.py) as named
presets — `NL` (default), `BE`, `DE`, `FR` and `US` (the upstream default).
Every value is overridable from `.env`:

| Variable | Default (NL) | Meaning |
|----------|--------------|---------|
| `TESLA_REGION` | `NL` | Preset to load |
| `TESLA_POSTAL_CODE` | `1012AB` | Default search location |
| `TESLA_RADIUS` | `0` | Search radius in km; 0 = whole country |
| `TESLA_LAT` / `TESLA_LNG` | Amsterdam | Anchor for distance sorting |
| `TESLA_MARKET` / `TESLA_LANGUAGE` / `TESLA_SUPER_REGION` / `TESLA_API_REGION` | `NL` / `nl` / `europe` / `NL` | Raw API query fields |
| `TESLA_LOCALE` | `nl_NL` | URL segment for the localised site |
| `TESLA_API_LOCALE_PREFIX` | *(empty)* | Set to `nl_NL` if the market-neutral API endpoint stops working |

Cookies are acquired on `tesla.com/nl_NL` so Akamai issues them for the same
market the API calls use.

### PostgreSQL (Optional)

If you want to use the database storage mode:

1. Set up a PostgreSQL server
2. Put the connection details in `.env`:

```
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=postgres
POSTGRES_DB=tesla
POSTGRES_PASSWORD=your_password
```

3. The tables (`tesla_used`, `tesla_new`) are created automatically on first insert, including a `market` column so multiple countries can share one database

## Usage Examples

Through Claude Code with the `/tesla` skill:

```
> Scrape de 30 goedkoopste tweedehands Model Y's in Nederland en sla ze op als CSV

> Zoek nieuwe Model 3 binnen 100 km van 3011AA, sorteer op prijs

> Geef gebruikte Model S vanaf 2023 met minder dan 60.000 km
```

Prices come back in EUR and odometer readings in km — the API returns the
Dutch feed, no conversion happens client-side.

## Data Fields

Each vehicle record from Tesla's API contains ~123 fields. This tool extracts 34 essential fields (27 upstream + 7 that only the European feed provides):

- **Identity** — VIN, Year, Model, TrimName
- **Pricing** — TotalPrice, PriceAdjustmentUsed, TransportationFee
- **Mileage** — Odometer, ActualRange
- **Appearance** — Paint, Interior, Wheels
- **Location** — City, StateProvince, CountryCode, MetroName
- **Market** — CurrencyCode (EUR), OdometerType (km), Price, PurchasePrice, RegistrationDate
- **History** — VehicleHistory, DamageDisclosure, CPORefurbishmentStatus
- **Provenance** — AcquisitionSubType, FleetVehicle, IsDemo
- **Features** — Autopilot package, HasVehiclePhotos

See [`data dictionary.md`](data%20dictionary.md) for the full field reference.

## Tech Stack

- [`fastmcp`](https://github.com/jlowin/fastmcp) — MCP server framework
- [`nodriver`](https://github.com/nicegui-development/nodriver) — Undetected Chrome automation (bypasses Akamai)
- [`curl_cffi`](https://github.com/lexiforest/curl_cffi) — HTTP client with TLS fingerprint impersonation
- [`asyncpg`](https://github.com/MagicStack/asyncpg) — Async PostgreSQL driver

## License

MIT — see the [upstream project](https://github.com/JumpBearCode/TeslaWebScrape).
