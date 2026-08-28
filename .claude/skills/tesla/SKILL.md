---
name: tesla
description: Scrape de Tesla-voorraad op tesla.com/nl_NL (Model Y & Model 3, occasion + nieuw), top 30 per model, en sla het resultaat op als CSV en/of in PostgreSQL.
---

## Uitgangspunten

- Markt: **Nederland** (`tesla.com/nl_NL`, market `NL`, taal `nl`, prijzen in **EUR**).
- Afstanden en kilometerstanden zijn in **km**, niet in mijlen.
- Standaard zoeklocatie komt uit `.env` (`TESLA_POSTAL_CODE`, standaard `1012AB`
  Amsterdam) met `radius=0` = heel Nederland.

Twijfel je of de juiste markt actief is? Roep eerst `region_info()` aan; die
laat de markt, de URL's en de exacte querystring zien.

## Uitvoering

### 0. Bepaal de opslagmethode

Vraag de gebruiker waar de data heen moet:

- **local** — alleen CSV in `results/`
- **postgres** — alleen wegschrijven naar PostgreSQL
- **both** — CSV én PostgreSQL

Standaard **local** (PostgreSQL is optioneel en vereist een draaiende server).
Wordt de skill non-interactief gestart via `./start.sh <mode>`, gebruik dan die
modus en vraag niets.

### 1. Cookies ophalen

```
acquire_cookies(model="my", condition="used")
```

Dit opent Chrome op `tesla.com/nl_NL` om de Akamai-beveiliging te passeren.
Lukt het niet, meld dat en laat de gebruiker het later opnieuw proberen.

### 2. Occasions zoeken (één voor één, niet parallel)

Eerst Model Y, pas daarna Model 3 — dat beperkt de kans op blokkades:

```
search_top_n(model="my", condition="used", radius=0, sort="Price", sort_order="asc", top_n=30, year_min=2023, odometer_max=60000)
```

Daarna:

```
search_top_n(model="m3", condition="used", radius=0, sort="Price", sort_order="asc", top_n=30, year_min=2023, odometer_max=60000)
```

`odometer_max` is in **km**. Laat `postal_code` weg om de waarde uit `.env` te
gebruiken, of geef een eigen postcode mee (bv. `postal_code="3011AA"`).

### 3. Nieuwe auto's zoeken (zonder bouwjaar-/km-filter)

```
search_top_n(model="my", condition="new", radius=0, sort="Price", sort_order="asc", top_n=30)
```

Daarna:

```
search_top_n(model="m3", condition="new", radius=0, sort="Price", sort_order="asc", top_n=30)
```

### 4. Foutafhandeling

Geeft een `search_top_n` een error terug (403/429), roep dan één keer
`acquire_cookies` aan om te verversen en probeer die zoekopdracht opnieuw.

`search_top_n` bladert zelf door de pagina's en ontdubbelt op VIN, dus één
aanroep per model is genoeg. De return bevat alleen
`{market, currency, distance_unit, total, returned, raw_file, slim_file}` —
geen voertuigdata.

### 5. Resultaten opslaan

#### local of both → samenvoegen tot CSV

```
merge_results(raw_files=[my_used_raw, m3_used_raw], filename="tesla_nl_occasions.csv")
merge_results(raw_files=[my_new_raw, m3_new_raw], filename="tesla_nl_nieuw.csv")
```

#### postgres of both → wegschrijven naar PostgreSQL

```
save_to_postgres(raw_files=[my_used_raw, m3_used_raw], condition="used")
save_to_postgres(raw_files=[my_new_raw, m3_new_raw], condition="new")
```

Elke run is een nieuwe momentopname (INSERT, geen UPSERT), met kolom `market`
op `NL`, zodat prijsontwikkeling over tijd te volgen is.

### 6. Tussenbestanden opruimen

Verwijder de JSON-tussenbestanden van deze run en houd alleen de CSV's over
(bij `postgres` staat de data al in de database):

```
rm results/topn_*.json
```

### 7. Afronden

Rapporteer aan de gebruiker:

- local/both: de paden van beide CSV-bestanden en het aantal regels
- postgres/both: het aantal ingevoegde rijen per tabel
- de goedkoopste vondst per model, met prijs in euro's en kilometerstand
