# EscapeRoomFramework

Silnik scenariuszy dla escape roomów. Opisujesz pokój w JSON-ie: jego wejścia (czujniki), wyjścia (zamki, światła)
i graf zagadek. Silnik pilnuje postępu gry, zapisuje każde zdarzenie w Postgresie i steruje sprzętem.

## Architektura

Każdy sprzęt rozmawia z silnikiem przez **MQTT**. To wspólny mianownik całego ekosystemu IoT:

```
Tasmota ─┐
ESPHome ─┤                      ┌─ Engine (.NET) ── Postgres
zigbee2mqtt (Zigbee) ─┼─ MQTT ──┤
Shelly / cokolwiek ───┤         └─ API HTTP (pod przyszły front React)
Kamera RTSP → QrCamera ─┘
```

Kamera RTSP nie mówi MQTT, więc `QrCamera` robi z niej urządzenie MQTT. ffmpeg pobiera klatki, ZXing czyta kody QR,
a odczytany kod trafia na `escaperoom/camera/<id>/qr` jak każdy inny czujnik. Tak samo da się później dopiąć
inne „wirtualne czujniki” (rozpoznawanie obrazu, audio itp.), bez zmian w silniku.

## Uruchomienie

```bash
docker compose up -d                      # Postgres (port 5433) + Mosquitto (1883)
dotnet run --project src/EscapeRoom       # API na http://localhost:5201
curl -X PUT localhost:5201/rooms/lab -H 'content-type: application/json' --data-binary @examples/lab.json
curl -X POST localhost:5201/rooms/lab/start
curl localhost:5201/rooms/lab/session
```

Testy: `dotnet test`.

## Definicja pokoju

Pełny przykład: [examples/lab.json](examples/lab.json). Kamera czyta QR, co otwiera pułapkę. W pułapce są czujniki
(kolby Zigbee, termometr ESPHome, przyciski Tasmota). Rozwiązanie kolb otwiera skrytkę, a kolby razem z przyciskami
odblokowują wyjście.

### Wejścia i wyjścia (`inputs`, `outputs`)

Każde ustawia dokładnie jedno z: `topic`, `tasmota`, `esphome`, `z2m`, `camera`.

| Preset | Wejście (subskrypcja) | Wyjście (publikacja) |
|---|---|---|
| `{"topic": "a/b", "path": "x.y"}` | `a/b`, pole `x.y` z JSON-a | `a/b`, z `path` → `{"path": value}` |
| `{"tasmota": "dev", "path": "Button1.Action"}` | `stat/dev/RESULT` | `cmnd/dev/<command>` (domyślnie `Power1`) |
| `{"esphome": "node", "entity": "switch/lock"}` | `node/switch/lock/state` | `node/switch/lock/command` |
| `{"z2m": "name", "path": "contact"}` | `zigbee2mqtt/name` | `zigbee2mqtt/name/set` |
| `{"camera": "cam1"}` | `escaperoom/camera/cam1/qr` | — |

Bez `path` wartością jest cały payload. Wartości porównywane są jako tekst bez rozróżniania wielkości liter
(`true`, `"ON"`, `42`).

Tasmota wysyła przyciski i przełączniki na `stat/.../RESULT` dopiero po odpięciu ich od przekaźników:
`SetOption73 1` dla przycisków, `SetOption114 1` dla przełączników.

### Zagadki (`puzzles`)

```json
{ "id": "flasks", "after": ["qr-code"], "when": { ... }, "then": [ ... ] }
```

- `after`: zagadka jest aktywna dopiero po rozwiązaniu wszystkich wymienionych. Pozwala to robić gałęzie
  równoległe i ich złączenia.
- `when`: warunek. Liczy się od chwili aktywacji, także na podstawie stanu sprzed niej: czujnik, który już jest
  w dobrej pozycji, rozwiązuje zagadkę od razu. Brak `when` oznacza zagadkę zaliczaną tylko ręcznie przez MG.
  - `{"input": "thermo", "equals": "x" | "gt": 40 | "lt": 10}`. Bez porównania pasuje dowolna wartość.
  - `{"all": [...]}`, `{"any": [...]}`.
  - `{"sequence": [...]}`: kolejność zdarzeń, np. przyciski albo kod. Liczy się ostatnie N pasujących zdarzeń,
    więc pomyłka nie blokuje gry, a zdarzenia niepasujące do żadnego kroku (puszczenie przycisku) są ignorowane.
    Musi być całym `when`.
- `then`: kroki wykonywane po rozwiązaniu: `{"output": "lock", "value": "OFF"}` albo `{"delay": 3000}`
  (np. impuls na elektrozamek).

`start` (na poziomie pokoju) to kroki wykonywane przy starcie gry, np. zamknięcie wszystkiego.

### Dla mistrza gry (`characters`, `variants`, `twists`, `hints`, brief)

Mistrz gry AI nie ma w kodzie nic o konkretnym pokoju. Wszystko, co wie, dostaje z silnika, więc prowadzi każdy pokój,
jeden naraz.

- **Brief** (`PUT /rooms/{id}/brief`, Markdown): historia, klimat, postacie, wygląd zagadek, tempo i reżyseria,
  opisane przez lokal tak jak dla człowieka. Przykład: [docs/scenariusz-jadro.md](docs/scenariusz-jadro.md).
- `characters`: głosy, którymi mówi, np. `{"name": "KRUK", "about": "...", "output": "voice", "voice": "<id głosu TTS>", "screen": "screen_zeta"}`.
  Kwestia trafia na `output` jako `{speaker, text, to, audio}`, a tekst także na `screen`.
- `variants`: dla klucza lista wariantów `{"solutions": {"zagadka": {warunek}}, "clues": [kroki], "summary": "..."}`.
  Losowanie podmienia rozwiązanie i wskazówkę razem, tylko w bieżącej grze (zapisany pokój zostaje przy domyślnym).
- `twists`: przygotowane zwroty akcji `{"about": "...", "then": [kroki], "adds": [zagadki], "blocks": [...], "after": [...]}`,
  raz na grę. `adds` dokłada zagadki, `blocks` każe im czekać na dodane, `after` mówi, co musi być już rozwiązane.
- `cues`: wskazówki bez słów wbudowane w pokój (lampka, neon, dźwięk): `{"about": "...", "puzzle": "telefon", "then": [kroki]}`.
  Mistrz gry może je odpalać wielokrotnie, zanim cokolwiek powie; z `puzzle` tylko gdy ta zagadka jest aktywna.
- `hints` w zagadce: przygotowane podpowiedzi lokalu, od najdelikatniejszej. Bez modelu mistrz gry używa tylko ich.

Wyjścia o nazwach `gm` (myśli mistrza gry) i `hint` (która zagadka dostała pomoc) są opcjonalne.

Model: `OPENROUTER_API_KEY` (przez OpenRouter) albo `ANTHROPIC_API_KEY` / `ant auth login`; bez danych logowania gra
prowadzi się na regułach. Głos: `ELEVENLABS_API_KEY` (model `Voice:Model`, domyślnie `eleven_v3`); pliki mp3 trafiają
pod `/voice/…` (adres publiczny w `Voice:PublicUrl`). Na Sprite klucze trzymają Connectory: `GameMaster:BaseUrl`
i `Voice:BaseUrl` wskazują ich bramki.

Definicja jest walidowana przy zapisie: nieznane pola (literówka `"equal"`), nieistniejące wejścia, wyjścia
i zagadki oraz cykle w `after` zwracają 400 z listą błędów.

## Kamery

W `appsettings.json` lub w zmiennych środowiskowych (`Cameras__0__Id`, `Cameras__0__Url`):

```json
"Cameras": [ { "Id": "lab-cam", "Url": "rtsp://user:pass@10.0.0.20/stream1", "Width": 1280, "Height": 720, "Fps": 2 } ]
```

Wymaga `ffmpeg` w PATH. Ten sam kod publikowany jest ponownie dopiero wtedy, gdy przez ~3 s zniknie z kadru.

## API

| | |
|---|---|
| `PUT /rooms/{id}` | zapis definicji (walidacja) |
| `GET /rooms/{id}` | definicja |
| `POST /rooms/{id}/start` | nowa gra (przerywa trwającą) |
| `POST /rooms/{id}/stop` | koniec gry |
| `POST /rooms/{id}/puzzles/{puzzle}/solve` | ręczne zaliczenie przez MG (wykonuje `then`) |
| `GET /rooms/{id}/session` | stan ostatniej gry: status zagadek i wartości wejść |
| `PUT /rooms/{id}/brief`, `GET /rooms/{id}/brief` | brief pokoju dla mistrza gry (Markdown) |
| `GET /voice/{plik}.mp3` | nagrane kwestie postaci dla głośników pokoju |
| `GET /safety`, `GET /safety/log`, `PUT /safety` | warstwa bezpieczeństwa, patrz niżej (zmiana tylko z tokenem operatora) |

Każde zdarzenie (`input`, `solved`, `output`, `gm-solve`, `room-updated`, `started`, `finished`…) trafia
do tabeli `events`. Trwająca gra jest wznawiana po restarcie procesu.

Zapis pokoju (`PUT` lub MCP) w trakcie gry od razu podmienia definicję w tej grze i zachowuje postęp.
Nowa lub przepięta zagadka, której warunek już jest spełniony, rozwiązuje się od razu i wykonuje swoje kroki.

## Serwer MCP

Silnik wystawia serwer MCP (Streamable HTTP) pod `/mcp`. Dzięki temu model AI może prowadzić grę i przepinać
zagadki na żywo. Podłączenie do Claude Code:

```bash
claude mcp add --transport http escaperoom http://localhost:5201/mcp
```

| Narzędzie | Do czego |
|---|---|
| `list_rooms`, `get_room`, `game_status` | odczyt pokoi i stanu gry |
| `list_mqtt_topics` | co jest na szynie MQTT i co wysyła, żeby znaleźć nowe urządzenia |
| `upsert_input`, `upsert_output` | dodanie lub podmiana czujnika albo wyjścia |
| `upsert_puzzle` | spięcie wejść i wyjść w zagadkę (dodanie lub podmiana po `id`) |
| `remove_from_room`, `save_room` | usunięcie elementu, zapis całego pokoju |
| `start_game`, `stop_game`, `solve_puzzle` | przebieg gry |
| `simulate_input` | wartość „jakby z czujnika”: test zagadki albo zastępstwo za zepsuty czujnik |
| `trigger_output` | sterowanie wyjściem od ręki, także bez gry |
| `get_brief`, `reroll_variant`, `fire_twist`, `fire_cue` | brief pokoju, losowanie wariantu, zwrot akcji i wskazówka świetlna w trwającej grze |

Każda zmiana przechodzi tę samą walidację co `PUT`. Błędna zmiana jest odrzucana z listą powodów i nic się nie zapisuje.

API i MCP odpowiadają tylko dla hostów z `AllowedHosts` (domyślnie localhost), co chroni przed DNS rebindingiem
ze stron WWW. Żeby sięgnąć do serwera z innej maszyny, dopisz jej nazwę lub IP serwera w `appsettings.json`.

## Bezpieczeństwo

Funkcje bezpieczeństwa (przycisk awaryjny, czujnik dymu, odcięcie zasilania zamków, oświetlenie awaryjne) to osobna
kategoria dla całego obiektu, niezależna od pokoi i gier. Przykład: [examples/safety.json](examples/safety.json).

- **Reguły działają zawsze**, także bez trwającej gry. Liczą się w pamięci, w pętli MQTT, i nie czekają na bazę.
  Dopóki warunek jest spełniony, każda nowa wartość czujnika ponownie wysyła wyjścia (np. gdy sterownik zamka
  się zrestartował).
- **Gra i MCP nie mogą ich dotknąć.** Wyjście pokoju, które publikowałoby do urządzenia bezpieczeństwa, jest
  odrzucane przy zapisie pokoju. Dodatkowo tuż przed każdą publikacją gry sprawdzana jest ostatnia blokada, zapisywana
  w audycie jako `blocked`. Do urządzenia „należy” każda komenda do niego, podszywanie się pod jego odczyty,
  broadcast Tasmoty `cmnd/tasmotas/` i `zigbee2mqtt/bridge/`. MCP nie ma żadnego narzędzia do bezpieczeństwa,
  a `list_mqtt_topics` oznacza tematy bezpieczeństwa jako `safety: true`.
- **Zmiana konfiguracji wymaga tokenu operatora** (`PUT /safety`, nagłówek `X-Safety-Token`, w treści `reason`
  i `config`). W konfiguracji serwera trzymany jest tylko SHA-256 tokenu, więc jej odczyt nie pozwala się podszyć:

  ```bash
  TOKEN=$(openssl rand -hex 32); echo "token dla operatora: $TOKEN"
  printf %s "$TOKEN" | shasum -a 256   # wynik do Safety:Operators:<imię> (np. zmienna Safety__Operators__jan)
  ```

- **Audyt:** tabela `safety_log` tylko do dopisywania. Trigger w Postgresie odrzuca UPDATE, DELETE i TRUNCATE.
  Trafia tam każda próba zmiany (`denied`, `rejected`, `config` z operatorem, IP i powodem), każde zadziałanie
  reguły (`fired`, `output`, `failed`) i każda zablokowana publikacja gry (`blocked`). Zmiana jest zapisywana
  w audycie, zanim zacznie obowiązywać. Odczyt: `GET /safety`, `GET /safety/log`.

**To warstwa dodatkowa, nie zastępuje sprzętowego bezpieczeństwa.** Elektrozamki muszą puszczać po zaniku zasilania,
a przycisk awaryjny musi fizycznie odcinać ich zasilanie, niezależnie od tego serwera, sieci i brokera. Dalsze
uszczelnienie należy do wdrożenia:
- ACL w brokerze, żeby inne klienty MQTT nie mogły publikować do tematów bezpieczeństwa;
- urządzenia bezpieczeństwa poza grupami zigbee2mqtt;
- aplikacja łącząca się z Postgresem rolą, która nie jest właścicielem `safety_log` i nie może zdjąć triggera.

Przywrócenie zasilania zamków po alarmie jest celowo poza oprogramowaniem i robi je człowiek.

## Czego jeszcze nie ma

Front, uwierzytelnianie API, MCP i brokera, limit czasu gry i podpowiedzi, `simulate_input` i `trigger_output`
w HTTP API (na razie tylko w MCP), ponawianie komend przy zerwanym MQTT, migracje bazy (schemat to `create if not exists`).
