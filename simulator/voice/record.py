#!/usr/bin/env python3
"""Makes the recordings of the room's fixed lines: speaks each line of lines.py through a speech gateway.

Run where the gateway can be reached (on the Sprite it is the voice Connector, no key needed):
    python3 lines.py > lines.tsv
    VOICE_GATEWAY=https://.../gateway/openrouter/<id> python3 record.py lines.tsv out/
then copy out/*.mp3 to simulator/godot/audio/lines/ and let Godot import them. Lines already in out/ are kept.
"""
import json
import os
import sys
import urllib.request

rows, out = sys.argv[1], sys.argv[2]
gateway = os.environ["VOICE_GATEWAY"].rstrip("/")
os.makedirs(out, exist_ok=True)
failed = 0
for row in open(rows, encoding="utf-8"):
    name, voice, _, spoken = row.rstrip("\n").split("\t")
    file = os.path.join(out, name + ".mp3")
    if os.path.exists(file):
        continue
    body = json.dumps({"model": os.environ.get("VOICE_MODEL", "elevenlabs/eleven-v3"), "voice": voice, "input": spoken,
                       "response_format": "mp3"}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(gateway + "/v1/audio/speech", body,
                                                           {"Content-Type": "application/json"}), timeout=60) as r:
            data = r.read()
            if not r.headers.get("Content-Type", "").startswith("audio/"):
                raise RuntimeError("the answer is " + r.headers.get("Content-Type", "nothing"))
        open(file, "wb").write(data)
    except Exception as e:  # one bad line must not lose the rest
        failed += 1
        print("failed:", name, e, file=sys.stderr)
print(len(os.listdir(out)), "recordings,", failed, "failed")
