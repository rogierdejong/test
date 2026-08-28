# Installatie & configuratie — Tesla voorraad-scraper (Nederland)

Deze kopie van [TeslaWebScrape](https://github.com/JumpBearCode/TeslaWebScrape)
is ingesteld op de **Nederlandse** Tesla-site: `tesla.com/nl_NL`, markt `NL`,
taal `nl`, prijzen in **euro's** en kilometerstanden in **km**.

## 1. Wat je nodig hebt

| Onderdeel | Waarom |
|-----------|--------|
| Python 3.11 of nieuwer | de MCP-server |
| [uv](https://docs.astral.sh/uv/) | pakketbeheer (`curl -LsSf https://astral.sh/uv/install.sh \| sh`) |
| Google Chrome | `nodriver` start een echte Chrome om Akamai te passeren |
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | hier praat je in gewone taal met de scraper |
| PostgreSQL | **optioneel**, alleen als je prijzen over tijd wilt bijhouden |

Belangrijk: de browser start **niet** headless. Draai dit dus op een machine
met een beeldscherm (je eigen laptop), niet op een kale server.

## 2. Installeren

Snelste weg — controleren, installeren en meteen scrapen:

```bash
cd <deze-repo>
./run.sh --check     # vereisten + installatie + configuratiecheck
./run.sh             # de scrape zelf, resultaat als CSV in results/
```

`./run.sh both` schrijft ook naar PostgreSQL. Liever met de hand:

```bash
uv sync
```

`uv sync` maakt een `.venv` aan en installeert `fastmcp`, `nodriver`,
`curl_cffi`, `asyncpg` en `python-dotenv`.

## 3. Configureren

De Nederlandse instellingen zijn de standaard — zonder `.env` werkt alles al.
Wil je een eigen postcode of zoekstraal:

```bash
cp .env.example .env
```

en pas aan:

```
TESLA_REGION=NL
TESLA_POSTAL_CODE=3011AA     # jouw postcode
TESLA_RADIUS=100             # km rond die postcode; 0 = heel Nederland
TESLA_LAT=51.9225            # coördinaten waar Tesla afstand vanaf rekent
TESLA_LNG=4.4792
```

Zet je `TESLA_LAT`/`TESLA_LNG` niet, dan rekent Tesla vanaf Amsterdam. De
postcode bepaalt het zoekgebied, de coördinaten de afstandssortering — zet ze
allebei op je eigen plaats voor een kloppende volgorde.

Andere markten zijn presets: `TESLA_REGION=BE`, `DE`, `FR` of `US`.

## 4. Controleren zonder te scrapen

```bash
uv run python -m tesla_mcp.selfcheck
```

Dit bouwt exact de API-aanroep die de scraper zou doen — zonder netwerkverkeer.
Je hoort te zien:

```
Market / language  : NL / nl
Super region       : europe
Currency / unit    : EUR / km
Site               : https://www.tesla.com/nl_NL
Inventory page     : https://www.tesla.com/nl_NL/inventory/used/my
OK — configuration is consistent.
```

## 5. Koppelen aan Claude Code

`.mcp.json` staat al in de repo en verwijst met een relatief pad naar dit
project. Start Claude Code vanuit deze map:

```bash
claude
```

Claude vraagt eenmalig om de MCP-server `tesla-inventory` goed te keuren.
Controleer daarna in Claude Code met `/mcp` of hij verbonden is.

Start je Claude Code vanuit een andere map, vervang dan in `.mcp.json` de `"."`
door het absolute pad naar deze map.

## 6. Gebruiken

Via de meegeleverde skill:

```
> /tesla
```

Of gewoon in je eigen woorden:

```
> Zoek de 30 goedkoopste tweedehands Model Y's in Nederland en zet ze in een CSV
> Nieuwe Model 3 binnen 100 km van 3011AA, gesorteerd op prijs
> Gebruikte Model S vanaf 2023 met minder dan 60.000 km
```

Wat er onder water gebeurt:

1. `region_info` — laat zien welke markt actief is (NL)
2. `acquire_cookies` — start Chrome op `tesla.com/nl_NL`, wacht op Akamai
   (~15 sec), bewaart de cookies 10 minuten
3. `search_top_n` — haalt via `curl_cffi` de voorraad op, bladert door en
   ontdubbelt op VIN
4. `merge_results` / `save_to_postgres` — CSV in `results/` of een
   momentopname in de database

Automatisch draaien kan met `./start.sh local` (of `postgres` / `both`).

De voortgang van de scraper (cookies, pagina's, fouten) gaat naar
`results/scrape.log`. Volg het live in een tweede terminal:

```bash
tail -f results/scrape.log
```

## 7. PostgreSQL (optioneel)

Zet in `.env`:

```
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=postgres
POSTGRES_DB=tesla
POSTGRES_PASSWORD=jouw_wachtwoord
```

De tabellen `tesla_used` en `tesla_new` worden bij de eerste insert automatisch
aangemaakt. Elke run is een nieuwe momentopname (INSERT, geen UPSERT), met een
kolom `market` (`NL`), zodat je prijsdalingen over tijd kunt volgen en meerdere
landen in één database kunt bewaren.

Voorbeeld — prijsverloop van één auto:

```sql
SELECT scraped_at, total_price
FROM   tesla_used
WHERE  vin = '<VIN>' AND market = 'NL'
ORDER  BY scraped_at;
```

## 8. Als er iets misgaat

Eerste stap bij twijfel:

```bash
uv run python -m tesla_mcp.diagnose
```

Dit controleert de omgeving (Chrome-pad, draait Chrome al, is er een
desktopsessie), test of `tesla.com/nl_NL` bereikbaar is, en loopt daarna de
cookiestappen één voor één af met de melding welke stap faalt.

| Symptoom | Oorzaak / oplossing |
|----------|--------------------|
| `Access Denied` / `Toegang geweigerd` bij het ophalen van cookies | Akamai blokkeert; verhoog de `asyncio.sleep(10)` in `tesla_mcp/scraper.py` of probeer het later opnieuw |
| API geeft 403 of 429 | Cookies verlopen — de server gooit ze zelf weg; roep `acquire_cookies` opnieuw aan en probeer één keer opnieuw |
| Nul resultaten, terwijl de site ze wel toont | Controleer met `region_info()` of markt `NL` actief is en of de postcode klopt |
| API geeft ineens 404 | Zet `TESLA_API_LOCALE_PREFIX=nl_NL` in `.env`; de aanroep gaat dan via `tesla.com/nl_NL/inventory/api/v4/...` |
| `KeyError: 'sameParty'` of hangen bij het uitlezen van cookies | Te oude `nodriver`; vanaf 0.50.3 is dit opgelost. Draai `uv sync` na een `git pull` |
| Blijft hangen op `acquire_cookies` | Draai `uv run python -m tesla_mcp.diagnose` — die loopt dezelfde stappen los af en zegt welke vastloopt. Meestal: Chrome draaide al, sluit hem volledig af met Cmd-Q. Elke stap heeft nu een timeout van 60 seconden, dus een hang eindigt met een duidelijke melding in plaats van eeuwig wachten |
| Chrome start niet / `could not find a valid chrome browser binary` | Zet `TESLA_CHROME_PATH` in `.env` naar je Chrome-binary. Er moet ook een echte desktopsessie zijn — de browser draait bewust niet headless, want headless wordt door Akamai geblokkeerd |
| `Failed to connect to browser` | Je draait als root of zonder beeldscherm (bv. in een container of over SSH); draai dit op je eigen desktop |
| Prijzen lijken in dollars | Dan draait preset `US`; zet `TESLA_REGION=NL` in `.env` |

Let op: dit haalt alleen publieke voorraadpagina's op. Houd het bij een
bescheiden aantal zoekopdrachten per dag — de ingebouwde vertraging van 1,5
seconde tussen pagina's staat er niet voor niets.
