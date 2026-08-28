# Tesla Inventory Data Dictionary

Slim fields extracted from Tesla v4 inventory API (~123 raw fields → 34 slim fields).

**Nederlandse markt (`market=NL`):** prijzen zijn in **EUR**, `Odometer` en
`ActualRange` in **km**. `StateProvince`, `VehicleHistory`, `TitleSubtype` en
`CPORefurbishmentStatus` zijn Amerikaanse velden en blijven in de NL-feed
meestal leeg; de EU-velden hieronder zijn juist alleen in Europa gevuld.
Ontbrekende velden worden `null`, nooit een fout.

## Vehicle Identity

| Field | Type | Description |
|-------|------|-------------|
| `VIN` | string | 车辆识别码 |
| `Year` | int | 年份 |
| `Model` | string | 车型代码 (my=Model Y, m3=Model 3) |
| `TrimName` | string | 具体配置名称 |

## Pricing

| Field | Type | Description |
|-------|------|-------------|
| `TotalPrice` | int | 总价 — totaalprijs (EUR in NL, $ in de VS) |
| `PriceAdjustmentUsed` | int | 价格调整/降价金额 — prijsverlaging |
| `TransportationFee` | int | 运输费 — transportkosten |
| `Price` | int | (EU) vraagprijs zoals getoond op de site |
| `PurchasePrice` | int | (EU) aankoopprijs incl. BTW |
| `CurrencyCode` | string | (EU) valuta van de bedragen, `EUR` in NL |

## Mileage & Battery

| Field | Type | Description |
|-------|------|-------------|
| `Odometer` | int | 里程 — kilometerstand (km in NL, mi in de VS) |
| `OdometerType` | string | (EU) eenheid van `Odometer`, `km` in NL |
| `ActualRange` | int | 当前实际续航 — actuele actieradius (km in NL) — batterijconditie |

## Appearance

| Field | Type | Description |
|-------|------|-------------|
| `PAINT` | list[str] | 车身颜色 |
| `INTERIOR` | list[str] | 内饰颜色 |
| `WHEELS` | list[str] | 轮毂尺寸 |

## Location

| Field | Type | Description |
|-------|------|-------------|
| `City` | string | 所在城市 — stad |
| `StateProvince` | string | 所在州 — staat (VS; leeg in NL) |
| `CountryCode` | string | (EU) landcode, `NL` |
| `MetroName` | string | (EU) regio / afleverlocatie |

## Timeline

| Field | Type | Description |
|-------|------|-------------|
| `FactoryGatedDate` | datetime | 出厂日期 — productiedatum |
| `FirstRegistrationDate` | datetime | 首次注册日期 — eerste registratie |
| `RegistrationDate` | datetime | (EU) registratiedatum zoals getoond in de NL-feed |

## Vehicle History & Condition

| Field | Type | Description |
|-------|------|-------------|
| `VehicleHistory` | string | 车辆历史 (CLEAN / PREVIOUS ACCIDENT(S)) |
| `DamageDisclosure` | bool | 损伤披露 |
| `DamageDisclosureStatus` | string | 损伤披露状态 |
| `CPORefurbishmentStatus` | string | 官方认证翻新状态 |

## Provenance

| Field | Type | Description |
|-------|------|-------------|
| `AcquisitionSubType` | string | 来源 (NONE=个人, PARTNER_LEASING=租赁回收) |
| `FleetVehicle` | bool | 是否车队车 |
| `IsDemo` | bool | 是否展示车 |
| `VehicleSubType` | string | 车辆子类型 (On-Site Ready / Used Test-drive) |
| `TitleSubtype` | string | 产权类型 (FIRSTREG=一手车) |

## Features & Media

| Field | Type | Description |
|-------|------|-------------|
| `AUTOPILOT` | list[str] | 自动驾驶配置 |
| `HasVehiclePhotos` | bool | 是否有实车照片 |
