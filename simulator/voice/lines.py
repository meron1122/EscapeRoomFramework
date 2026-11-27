#!/usr/bin/env python3
"""Lists the room's fixed announcements: the lines the simulator itself says as RAVEN and ZETA (sim.gd), which
are recordings in a real room, unlike the lines the game master writes live.

Run: python3 simulator/voice/lines.py   -> one line per recording: <name>\t<voice>\t<SPEAKER|text>\t<what is spoken>
The recordings are godot/audio/lines/<name>.mp3, where <name> is the first 16 hex digits of sha256("SPEAKER|text");
the game looks a line up by that name and stays silent when there is none. record.py makes them from this list.
"""
import hashlib
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SIM = (ROOT / "godot" / "scripts" / "sim.gd").read_text()
ROOM = json.loads((ROOT.parent / "examples" / "the-core.json").read_text())
VOICES = {c["name"]: c["voice"] for c in ROOM["characters"]}
# How each character delivers a recorded line (a tag the speech model acts on and does not read aloud).
DELIVERY = {"RAVEN": "[low, urgent]", "ZETA": "[calm, cold]"}
FILL = {"variant.duty": ["Marek", "Ola", "Tomek"], "TEAM": [re.search(r'const TEAM := "([^"]+)"', SIM).group(1)]}


def lines():
    out = []
    # say("ZETA", "…") and say("ZETA", "… %s …" % value); the director's hints for a named puzzle are left out:
    # in a room with the game master he writes those himself.
    for who, text, value in re.findall(r'say\("(ZETA|RAVEN)", "((?:[^"\\]|\\.)*)"(?: % ([\w.]+))?\)', SIM):
        for v in FILL.get(value, [None]) if value else [None]:
            if value and v is None:
                continue
            out.append((who, text % v if value else text))
    # say("ZETA", ["…", "…"][n]): a list of lines said in turn
    for who, block in re.findall(r'say\("(ZETA|RAVEN)", \[(.*?)\]\[', SIM, re.S):
        out += [(who, t) for t in re.findall(r'"((?:[^"\\]|\\.)*)"', block)]
    # the prepared hints, spoken by RAVEN
    hints = re.search(r"const HINTS := \{(.*?)\n\}", SIM, re.S).group(1)
    out += [("RAVEN", t) for t in re.findall(r': "((?:[^"\\]|\\.)*)"', hints)]
    return list(dict.fromkeys(out))


if __name__ == "__main__":
    for who, text in lines():
        print(hashlib.sha256(f"{who}|{text}".encode()).hexdigest()[:16], VOICES[who], f"{who}|{text}",
              f"{DELIVERY[who]} {text}", sep="\t")
