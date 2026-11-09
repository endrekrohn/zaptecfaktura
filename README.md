# zaptecfaktura

Henter månedlig ladeforbruk per Zaptec-installasjon, lager et PDF-grunnlag og
oppretter fakturautkast i Fiken. Brukeren kontrollerer og sender fakturaene
fra Fiken.

## Slik brukes den

1. Logg inn med Zaptec-kontoen (bare brukere i `ALLOWED_USERS`).
2. Oversikten viser forrige måned. Velg en annen måned øverst ved behov.
3. Første gang, eller når en kunde endres: trykk «Velg kunde» og koble
   installasjonen til en kunde i Fiken.
4. Fyll inn strømregningen (sum og kWh) og påslag, eller skriv pris per kWh
   direkte.
5. Trykk «Opprett utkast i Fiken». Utkastene lages i bakgrunnen med
   PDF-grunnlaget som vedlegg, og statusen vises i tabellen.

Hvert utkast får en fast UUID per installasjon og måned. Å trykke på nytt lager
ingen duplikater: et utkast som finnes blir ikke overskrevet, og en installasjon
som allerede er fakturert hoppes over. Slett utkastet i Fiken for å lage det på
nytt med ny pris.

## Personvern

Tjenesten lagrer ikke kundenavn eller andre kundeopplysninger, bare Fikens
kunde-id per Zaptec-installasjon. Navn hentes fra Fiken når siden vises.
Sesjoner lagrer Zaptec-tokenet og brukerens e-post til sesjonen utløper.

## Oppsett

Kopier `.env.example` til `.env` og fyll inn:

| Variabel | Beskrivelse |
|---|---|
| `ALLOWED_USERS` | Zaptec-e-poster som får logge inn, kommaseparert. Påkrevd når Fiken er satt opp. |
| `FIKEN_API_TOKEN` | Personlig API-nøkkel (Rediger konto → API). Krever API-modulen i Fiken. |
| `FIKEN_COMPANY_SLUG` | Foretakets slug fra Fiken-URL-en. |
| `FIKEN_PRODUCT_ID` | Valgfritt. Fiken-produkt for fakturalinjen (gir mva-type og inntektskonto). |
| `FIKEN_VAT_TYPE` / `FIKEN_INCOME_ACCOUNT` | Brukes når produkt ikke er satt. Standard `HIGH` / `3000`. |
| `FIKEN_DAYS_UNTIL_DUE` | Forfall i dager. Standard 14. |
| `DATABASE_PATH` | SQLite-fil. Standard `data/zaptecfaktura.db`, i Docker `/data/zaptecfaktura.db`. |

Uten Fiken-variablene fungerer tjenesten som før: grunnlaget kan lastes ned som
PDF eller zip.

## Kjøre lokalt

```sh
uv run fastapi dev src/main.py
uv run pytest
```

## Docker

```sh
docker compose up --build
```

Databasen ligger i volumet `zaptecfaktura-data`, så kundekoblingene overlever
ny deploy.
