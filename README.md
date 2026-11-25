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

Każde zdarzenie (`input`, `solved`, `output`, `gm-solve`, `started`, `finished`…) trafia do tabeli `events`.
Trwająca gra jest wznawiana po restarcie procesu.

## Czego jeszcze nie ma

Front, uwierzytelnianie API i brokera, limit czasu gry i podpowiedzi, ręczne sterowanie wyjściami z API,
ponawianie komend przy zerwanym MQTT, migracje bazy (schemat to `create if not exists`).
