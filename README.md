# Coinmate Controller

Male REST API pro cteni CZK/BTC zustatku a maker limitni nakup nebo market prodej BTC. Sluzba je
urcena pouze pro komunikaci mezi kontejnery. Docker Compose zamerne nepublikuje
port na hostitele.

## Konfigurace

V produkci vytvorte `.env` podle `.env.example`. Coinmate API klic by mel mit
jen opravneni ke cteni a obchodovani, nikdy k vyberum. Soubor `.env` je ignorovan
Gitem a nekopiruje se do Docker image.

```sh
cp .env.example .env
openssl rand -hex 32
docker compose up --build -d
```

Do `CONTROLLER_API_TOKEN` vlozte vygenerovany token. `MAX_MARKET_BUY_CZK` a
`MAX_MARKET_SELL_BTC` omezuji maximalni velikost jednoho prikazu nezavisle na
volajicim.

## REST API

Kontrola procesu nevyzaduje autentizaci:

```http
GET /health
```

Aktualni zustatek vraci primo jedno JSON cislo. Povolene meny jsou `czk` a `btc`:

```http
GET /current_balance/btc
Authorization: Bearer <CONTROLLER_API_TOKEN>
```

Priklad odpovedi kompatibilni s typem `decimal` a `numeric(20,8)` ve FINSTRAT2.0:

```json
0.01234567
```

Serverovy maker nakup BTC:

```http
POST /buy_bitcoin
Authorization: Bearer <CONTROLLER_API_TOKEN>
Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000
Content-Type: application/json

{
  "amount": 1000.00
}
```

`amount` je maximalni rozpocet v CZK vcetne poplatku. Odpoved obsahuje priznak uplneho
vyplneni objednavky a skutecne nakoupene mnozstvi BTC z detailu Coinmate prikazu:

```json
{"success":true,"btc_bought":0.00041234,"status":"filled","pending":false}
```

Nakup bezi na serveru i bez dalsich HTTP pozadavku. Prikazy pouzivaji
`buyLimit`, `postOnly=1` a cenu o jeden tick pod nejlepsi prodejni nabidkou.
Po `PURCHASE_REPRICE_SECONDS` (30 s) se zbytek zrusi, overi posledni plneni a
vystavi nova maker objednavka ze zbyvajiciho rozpoctu. Cena muze rust i klesat.
Sazba maker musi byt nejvyse 0,4 %; zadny market fallback neexistuje.
Velikost objednavky rezervuje vyssi z aktualnich sazeb maker/taker a jeden
haler na zaokrouhleni. To nemeni post-only rezim ani skutecny poplatek plneni.
Objednavka se vejde do zbyvajiciho rozpoctu i dostupneho CZK zustatku.
Pokud dostupny zustatek nestaci na minimum, nakup zustane cekat s chybou;
nedostatek zustatku se nepovazuje za uspesne dokonceni.
`PURCHASE_POLL_SECONDS` je vychozi 5 s. Vsechny Coinmate HTTP pozadavky jsou
serializovany s rozestupem nejmene 0,7 s (limit burzy je 100/min na API klic).

`GET /funding_balance/czk` poskytuje interni normalizovany zustatek pro
serverove sledovani vkladu: skutecny CZK zustatek plus skutecna utrata maker
nakupu. Neni urcen pro zobrazeni disponibilnich penez. Pri nejistych
objednavkach nebo nestabilnim snapshotu vraci chybu a kontrola se opakuje.

`GET /buy_bitcoin/requirements` vraci `min_amount_czk`, `min_amount_btc` a
`max_amount_czk`. Minimum i cenovy krok pochazeji z `tradingPairs`.
Vysledek nakupu dale obsahuje `spent_czk` (soucet cen a poplatku plneni),
`limit_price`, `detail` a `completed_at` (Unix sekundy posledniho plneni).
Nevyplnene ulohy se obnovuji ze SQLite po restartu. Nejisty submit se dohledava
podle ulozeneho `clientOrderId` a nikdy se slepe neopakuje.

Pri castecne vyplnene objednavce je `success` rovno `false`, ale `btc_bought`
obsahuje jiz skutecne nakoupene BTC. Pri timeoutu nebo chybe Coinmate vraci API
ne-2xx odpoved; nelze bezpecne tvrdit, ze nakup neprobehl.

`Idempotency-Key` je povinne UUID a musi byt pro zamysleny nakup stabilni. Stejny
klic a castka vzdy vrati stejny nakup; pouziti klice s jinou castkou vrati HTTP
409. Stav se uklada do SQLite databaze na volume `coinmate-data`, takze prezije
restart kontejneru.

Pokud se spojeni prerusi po moznem prijeti objednavky, zaznam dostane stav
`unknown`. Opakovani se stejnym klicem neposle dalsi nakup, ale pokusi se puvodni
objednavku dohledat podle Coinmate `clientOrderId`. Dokud vysledek neni znamy,
API vraci HTTP 202:

```json
{"success":false,"btc_bought":0,"status":"unknown","pending":true}
```

Stav lze bez noveho nakupu overit take samostatne:

```http
GET /buy_bitcoin/550e8400-e29b-41d4-a716-446655440000
Authorization: Bearer <CONTROLLER_API_TOKEN>
```

### Zruseni maker nakupu

`POST /buy_bitcoin/{idempotency_key}/cancel` se stejnou Bearer autentizaci
trvale zakaze nove pokusy. Vraci HTTP 202, dokud worker neoveri konec
existujici objednavky, potom stav `cancelled` a `pending=false`. Castecna
plneni zustavaji v `btc_bought`, `spent_czk` a `completed_at`. Opakovany
POST nakupu se stejnym klicem jej znovu nespusti. Neznamy vysledek odeslani
zustava cekajici i po zruseni: prazdny seznam aktivnich objednavek nedokazuje,
ze objednavka nebyla vyplnena. Worker dohledava puvodni clientOrderId.

Pro zruseni pred spustenim nove verze pouzijte nasledujici postup (nahradte
UUID klicem nakupu). Offline prikaz pouzivejte pouze pri zastavene sluzbe:

```sh
docker compose stop coinmate-controller
docker compose build coinmate-controller
docker compose run --rm --no-deps coinmate-controller python -m app.cancel_purchase 550e8400-e29b-41d4-a716-446655440000
docker compose up -d coinmate-controller
```

Pokracujte spustenim sluzby pouze pokud prikaz zruseni uspel. Prikaz pouze
ulozi pozadavek do existujici databaze; sam neodesila zadne objednavky.
Zruseni a overeni na burze dokonci worker po startu. FINSTRAT2.0 prevezme
konecny vysledek pri dalsim dotazu a zauctuje pripadna skutecna plneni.

Market prodej BTC funguje stejne idempotentne. `amount` je mnozstvi BTC k prodeji
a smi mit nejvyse osm desetinnych mist:

```http
POST /sell_bitcoin
Authorization: Bearer <CONTROLLER_API_TOKEN>
Idempotency-Key: 7a8b9c00-e29b-41d4-a716-446655440001
Content-Type: application/json

{
  "amount": 0.00100000
}
```

```json
{"success":true,"btc_sold":0.001,"status":"filled","pending":false}
```

Stav prodeje lze bez odeslani noveho prikazu overit pres
`GET /sell_bitcoin/{idempotency_key}`.

Tabulky `purchases`, `purchase_events`, `sales` a `sale_events` tvori trvaly audit
zadani, zmen stavu, Coinmate order ID a skutecne zobchodovaneho BTC. Databazovy
volume musi byt soucasti zaloh produkcniho serveru.

## Sledovani zmeny zustatku

Nejprve klient vytvori watcher. Tim se ulozi vychozi Coinmate zustatek:

```http
POST /balance_watch/btc
Authorization: Bearer <CONTROLLER_API_TOKEN>
```

```json
{
  "watch_id": "550e8400-e29b-41d4-a716-446655440000",
  "currency": "btc",
  "initial_balance": 0.01234567,
  "expires_in_seconds": 30
}
```

Klient spusti cekajici pozadavek:

```http
GET /balance_watch/550e8400-e29b-41d4-a716-446655440000
Authorization: Bearer <CONTROLLER_API_TOKEN>
```

Soubezne kazdych 10 sekund obnovi watcher:

```http
POST /balance_watch/550e8400-e29b-41d4-a716-446655440000/ping
Authorization: Bearer <CONTROLLER_API_TOKEN>
```

Pri zmene zustatku cekajici pozadavek skonci:

```json
{"changed":true,"currency":"btc","balance":0.01274567}
```

Pokud heartbeat 30 sekund neprijde, sledovani se zastavi a cekajici pozadavek
vrati `changed: false`. Vysledek watcher spotrebuje; pro dalsi sledovani je treba
vytvorit novy. Aktivni jsou soucasne nejvyse ctyri watchery. Intervaly lze
nastavit pres `BALANCE_WATCH_TIMEOUT_SECONDS` a `BALANCE_WATCH_POLL_SECONDS`.

Kontejner volajiciho pripojte k siti `coinmate-controller` a pouzijte adresu
`http://coinmate-controller:8080`. Sit umoznuje sluzbe odchozi HTTPS spojeni na
Coinmate, ale bez `ports` neni REST API dostupne z hostitele, LAN ani WAN.

## Vyvoj

```sh
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

Implementace vychazi z oficialni dokumentace:
https://docs.coinmate.io/api/docs/
