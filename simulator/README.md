# Symulator Jądra

Izometryczny symulator pokoju „Jądro” (scenariusz: [docs/scenariusz-jadro.md](../docs/scenariusz-jadro.md)) w Godot 4.7,
uruchamiany w przeglądarce. Pięcioro symulowanych Nomadów przechodzi grę, a reżyser AI losuje wariant, podpowiada
i dokłada zwroty akcji tak, jak opisuje scenariusz.

## Z czego się składa

| Ścieżka | Co to jest |
| --- | --- |
| `godot/layout.json` | plan pokoi: wymiary, drzwi, rekwizyty, zagadki i miejsca pracy; czytają go Blender i Godot |
| `blender/build_assets.py` | buduje modele pokoi, rekwizytów i postaci → `godot/assets/*.glb` |
| `music/compose.py` | syntezuje muzykę i efekty z kodu → `godot/audio/*.mp3` |
| `godot/scripts/sim.gd` | zegar, graf zagadek, wariant gry, reżyser AI (podpowiedzi, zwroty akcji) |
| `godot/scripts/npc.gd` | symulowani gracze: chodzenie po siatce A*, praca, animacja |
| `godot/scripts/main.gd` | składa scenę, światła, ekrany, kamerę i muzykę |
| `godot/scripts/atmosphere.gd` | miasto w tle (osobny świat 3D z kamerą perspektywiczną), platforma, deszcz |
| `godot/scripts/mqtt.gd` | klient MQTT 3.1.1 po WebSocket: symulator udaje sprzęt pokoju na tym samym brokerze co prawdziwe urządzenia |

## Przebudowa i uruchomienie

```bash
/Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup --python simulator/blender/build_assets.py
```

```bash
python3 simulator/music/compose.py
```

```bash
cd simulator/godot && godot --headless --path . --import && godot --headless --path . --export-release "Web" ../web/index.html
```

```bash
python3 -m http.server 8060 --bind 127.0.0.1 --directory simulator/web
```

Potem `http://localhost:8060`. Sterowanie: LPM/PPM przesuwa, kółko przybliża, 1/2/3 pokoje, F śledzi drużynę,
H ukrywa panele, spacja pauzuje.

## Z silnikiem i mistrzem gry AI (wirtualny sprzęt)

Gdy broker MQTT jest osiągalny, symulator przestaje sam rozstrzygać zagadki: wysyła wejścia (`sim/jadro/...`) jak
czujniki w pokoju, a o wszystkim decyduje silnik i agent mistrza gry. Ekrany, drzwi, kwestie KRUKA, podpowiedzi
i zwroty akcji przychodzą z silnika. Bez brokera działa lokalny reżyser offline.

```bash
docker compose up -d
```

```bash
ASPNETCORE_ENVIRONMENT=Development dotnet run --project src/EscapeRoom
```

```bash
curl -X PUT localhost:5201/rooms/jadro --data-binary @examples/jadro.json -H 'Content-Type: application/json'
```

Adres brokera: `?mqtt=ws://host:9001` w URL strony (`?mqtt=off` wyłącza silnik); domyślnie `ws://localhost:9001`
na localhost i `wss://<host>/mqtt` gdzie indziej. Mistrz gry używa modelu Claude przez OpenRouter, gdy jest ustawione
`OPENROUTER_API_KEY`, a w przeciwnym razie bezpośrednio przez Anthropic (`ANTHROPIC_API_KEY` albo profil `ant auth login`).
Bez danych logowania gra prowadzi się na regułach; `GameMaster:RulesOnly=true` wymusza reguły. Jako briefu mistrz gry
używa [scenariusza pokoju](../docs/scenariusz-jadro.md), więc zmiany w scenariuszu od razu zmieniają jego wiedzę o pokoju.

Test bez okna (cała gra w ~70 s, dziennik na wyjściu):

```bash
cd simulator/godot && godot --headless --path . --fixed-fps 60 --quit-after 4300 -- --autostart --speed=60
```

Zrzut klatki z prawdziwym renderem: `godot --path . -- --autostart --zoom=22 --shot=/ścieżka/kadr.png`.
