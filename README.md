# zaptecfaktura

Henter månedlig ladeforbruk per Zaptec-installasjon, lager et PDF-grunnlag og
oppretter fakturautkast i Fiken. Brukeren kontrollerer og sender fakturaene
fra Fiken.

## Slik brukes den

1. Logg inn med Zaptec-kontoen.
2. Første gang: trykk «Koble til Fiken», logg inn i Fiken og gi appen tilgang.
   Har du flere foretak, velger du hvilket utkastene skal lages i. Deretter
   velger du inntektskonto, mva og forfall for utkastene. Tilkoblingen huskes,
   så dette gjøres bare én gang. Innstillingene kan endres under
   Fiken → Fakturainnstillinger.
3. Oversikten viser forrige måned. Velg en annen måned øverst ved behov.
4. Første gang, eller når en kunde endres: trykk «Velg kunde» og koble
   installasjonen til en kunde i Fiken.
5. Fyll inn strømregningen (sum og kWh) og påslag, eller skriv pris per kWh
   direkte.
6. Trykk «Opprett utkast i Fiken». Utkastene lages i bakgrunnen med
   PDF-grunnlaget som vedlegg, og statusen vises i tabellen.

Hvert utkast får en fast UUID per installasjon og måned. Å trykke på nytt lager
ingen duplikater: et utkast som finnes blir ikke overskrevet, og en installasjon
som allerede er fakturert hoppes over. Slett utkastet i Fiken for å lage det på
nytt med ny pris.

## Zaptec

Tjenesten bruker endepunktene som erstatter `GET /api/chargehistory`
(fases ut 1. januar 2027). Zaptec-brukeren må være eier av installasjonene.

- `GET /api/installation?Roles=2`: installasjonene brukeren eier.
- `POST /api/chargehistory/installationreport`: totalt forbruk og antall
  ladeøkter per installasjon, til oversikten.
- `GET /api/sessions/archived`: ladeøktene, til PDF-grunnlaget og utkastene.

En økt hører til måneden den ble avsluttet i (UTC), som før. Utkastet bruker
summen av øktene, så fakturalinjen alltid stemmer med vedlegget.

For å gjøre oversikten rask:

- Installasjonene huskes i 15 minutter per bruker.
- Totaler for måneder som ble avsluttet for mer enn 5 dager siden lagres i
  databasen (bare tall). Inneværende måned huskes i 5 minutter.
- Forbruk hentes for opptil fire installasjoner samtidig, over én gjenbrukt
  forbindelse.
- «Oppdater fra Zaptec» over tabellen henter alt på nytt.

## Personvern

Tjenesten lagrer ikke kundenavn eller andre kundeopplysninger, bare Fikens
kunde-id per foretak og Zaptec-installasjon. Navn hentes fra Fiken når siden
vises og huskes bare i minnet i noen minutter. Navn og e-post til de som har
ladet (fra Zaptec) leses ikke inn og lagres aldri. Sesjoner lagrer Zaptec-tokenet og brukerens e-post til sesjonen utløper.

Fiken-tilgangen er OAuth per bruker. Access- og refresh-token lagres kryptert
med `SECRET_KEY`. «Koble fra Fiken» trekker tilbake tilgangen hos Fiken og
sletter tokenene. Brukeren kan også fjerne appen i Fiken under Rediger konto →
Sikkerhet → Apper du har gitt tilgang til.

## Fiken-appen

Opprett appen i Fiken under Rediger konto → API → Ny app:

- **Hvem skal bruke appen:** «Meg selv/mine foretak».
- **Redirect URIs:** `https://<domene>/fiken/callback`, og
  `http://localhost:8000/fiken/callback` for lokal utvikling.
- **Godkjente brukere:** Fiken-brukerne som skal kunne koble til.
- **Kun lesetilgang:** av (appen lager utkast).
- **Uten client secret (PKCE):** av. Appen kjører på en server og bruker
  client secret, og sender PKCE i tillegg.

Foretaket må ha API-modulen i Fiken.

## Oppsett

Kopier `.env.example` til `.env` og fyll inn:

| Variabel | Beskrivelse |
|---|---|
| `ALLOWED_USERS` | Valgfritt. Zaptec-e-poster som får logge inn, kommaseparert. |
| `SECRET_KEY` | Krypterer lagrede Fiken-token. Bytter du den, må alle koble til Fiken på nytt. |
| `FIKEN_CLIENT_ID` / `FIKEN_CLIENT_SECRET` | Fra Fiken-appen. |
| `FIKEN_REDIRECT_URI` | Må være registrert på appen, f.eks. `https://<domene>/fiken/callback`. |
| `FIKEN_VAT_TYPE` / `FIKEN_INCOME_ACCOUNT` / `FIKEN_DAYS_UNTIL_DUE` | Standardverdier til foretaket har lagret egne fakturainnstillinger. Standard `HIGH` / `3000` / 14. |
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
