class_name Sim
extends Node
## Game clock, puzzle graph and the AI director from docs/scenariusz-jadro.md (hint ladder, twists, variant per game).

signal log_line(speaker: String, text: String)
signal task_changed(id: String, state: String)
signal door_changed(id: String, open: bool)
signal stage_changed(room: String)
signal twist(kind: String, seconds: float)
signal finished(kind: String)
signal submit(id: String)  # engine mode: the team believes it solved this; send the inputs to the engine

const TEAM := "Nomadzi z Trasy 7"
const GAME_SECONDS := 3600.0
const HINTS := {
	"przeszukanie": "Identyfikatory leżą po kątach. Sprawdźcie donice i kanapę.",
	"telefon": "Reklama w holu rysuje gest. Powtórzcie go na telefonie.",
	"szafka": "Grafik mówi, kto ma dziś zmianę. Godziny zmiany to kod.",
	"bramka": "Tylko identyfikator dyżurnego otworzy bramkę.",
	"sciana": "Raport czasu ekranowego. Ustawcie telefony od najdłuższego.",
	"zdrive": "Kod serwisowy z ekranu auta. Instrukcja mówi, jak go czytać.",
	"zmart": "Profil klientki i plakaty. Co algorytm wcisnąłby jej najpierw?",
	"skanery": "Dwa zapalone skanery, dwie dłonie naraz.",
	"blokada": "Panel przy drzwiach. Sekwencja jest w tym, co mówi ZETA.",
	"firewall": "Lustra. Każda wiązka w zapalony czujnik.",
	"uzbrojenie": "Hasło ZETY mówi, w jakiej kolejności wpisać fragmenty.",
	"wybor": "Dwa klucze, jednocześnie. Albo jej przycisk.",
}

var taunts := 0
var layout: Dictionary
var rng := RandomNumberGenerator.new()
var t := 0.0
var running := false
var ending := ""
var tasks := {}  # id -> {def, state, progress, need, workers}
var variant := {}
var variant_lines: Array[String] = []
var stage := "hol"
var stage_started := 0.0
var done_at := {}  # room -> seconds
var hint_level := 0
var hint_factor := 1.0
var hints_used := 0
var twists_used: Array[String] = []
var freeze_until := 0.0
var dim_until := 0.0
var last_event := 0.0
var last_taunt := 0.0
var next_director := 30.0
var pending_twist := {}  # {kind, at}
var team_pace := 1.0  # how quick this team is at puzzles, drawn per game
var engine_mode := false  # true: the engine owns puzzle states and the AI game master directs; false: offline
var boosts := {}  # task id -> game time until which a hint speeds the team up
const ENGINE_TO_TASK := {"detonacja": "wybor", "uklad": "wybor"}


func new_game(seed_value: int) -> void:
	rng.seed = seed_value
	t = 0.0
	ending = ""
	tasks.clear()
	done_at.clear()
	twists_used.clear()
	hints_used = 0
	pending_twist = {}
	team_pace = rng.randf_range(0.75, 1.25)
	for d in layout.tasks:
		tasks[d.id] = {"def": d.duplicate(true), "state": "locked", "progress": 0.0,
			"need": d.work * rng.randf_range(0.8, 1.25), "workers": []}
	_make_variant()
	_enter_stage("hol")
	_refresh()
	say("KRUK", "Jesteście w środku. Macie godzinę, zanim ochrona odetnie piętra.")
	say("ZETA", "Witamy w Z. Proszę zaczekać na asystę.")


func say(speaker: String, text: String) -> void:
	log_line.emit(speaker, text)


func clock() -> String:
	var left := maxf(GAME_SECONDS - t, 0.0)
	return "%02d:%02d" % [int(left) / 60, int(left) % 60]


func rate_scale() -> float:
	if t < freeze_until:
		return 0.0
	return team_pace * hint_factor * (0.7 if t < dim_until else 1.0)


func tick(dt: float) -> void:
	if not running or ending != "":
		return
	t += dt
	if t >= GAME_SECONDS:
		_finish("czas")
		return
	for id in tasks.keys():
		var tk: Dictionary = tasks[id]
		if tk.state != "active":
			continue
		var skill := 0.0
		var present := 0
		for w in tk.workers:
			if w.at_spot:
				skill += w.skill
				present += 1
		if present == 0 or present < int(tk.def.get("coop", 0)):
			continue
		var boost := 1.6 if t < boosts.get(id, 0.0) else 1.0
		tk.progress += pow(skill, 0.8) * rate_scale() * boost * dt / 60.0
		if tk.progress >= tk.need:
			if not engine_mode:
				solve(id, "")
			elif t - tk.get("submitted_at", -999.0) > 20.0:
				tk.submitted_at = t
				submit.emit(id)
	if engine_mode:
		return
	if pending_twist and t >= pending_twist.at:
		_run_twist(pending_twist.kind)
		pending_twist = {}
	if t >= next_director:
		next_director = t + 30.0
		_direct()


func solve(id: String, by: String) -> void:
	var tk: Dictionary = tasks[id]
	if tk.state == "solved":
		return
	tk.state = "solved"
	tk.progress = tk.need
	last_event = t
	task_changed.emit(id, "solved")
	if by == "":
		say("NOMADZI", tk.def.say)
	if engine_mode:
		return  # doors, lines and the next puzzles come from the engine and the game master
	if tk.def.has("opens"):
		door_changed.emit(tk.def.opens, true)
	match id:
		"bramka":
			say("ZETA", "Dzień dobry, %s. Miłej zmiany." % variant.dyzurny)
		"skanery":
			say("KRUK", "Jesteście blisko. Schodźcie.")
		"blokada":
			say("ZETA", "Niech wam będzie.")
		"uzbrojenie":
			say("ZETA", "Wyjmijcie rdzenie, a każdy z was dostanie dożywotni abonament premium.")
		"wybor":
			_finish("detonacja" if rng.randf() < 0.85 else "uklad")
			return
	_refresh()
	var room: String = tk.def.room
	if _room_done(room) and not done_at.has(room):
		done_at[room] = t
		var ahead: float = layout.plan[room] * 60.0 - t
		var next_room: String = {"hol": "pietro", "pietro": "jadro"}.get(room, "")
		if room == "pietro" and ahead >= 300.0 and not tasks.has("blokada"):
			_run_twist("blokada")
		elif next_room != "":
			if ahead >= 300.0 and room == "hol":
				pending_twist = {"kind": ["patrol", "zaciemnienie"][rng.randi() % 2], "at": t + rng.randf_range(90, 180)}
			_enter_stage(next_room)


func _enter_stage(room: String) -> void:
	var announce := room != stage or t == 0.0
	stage = room
	stage_started = t
	hint_level = 0
	hint_factor = 1.0
	last_event = t
	stage_changed.emit(room)
	if not announce or engine_mode:
		return  # in engine mode the game master speaks
	match room:
		"pietro":
			say("ZETA", "Piętro Nawyków. Tu powstaje wasza codzienność.")
		"jadro":
			say("ZETA", "%s. Wiedziałam, że przyjdziecie." % TEAM)


func _refresh() -> void:
	for id in tasks.keys():
		var tk: Dictionary = tasks[id]
		if tk.state != "locked":
			continue
		var ready := true
		for a in tk.def.after:
			if not tasks.has(a) or tasks[a].state != "solved":
				ready = false
		if ready:
			tk.state = "active"
			task_changed.emit(id, "active")
			if id == "skanery":
				say("ZETA", "Nie macie uprawnień do poziomu minus trzy.")


func _room_done(room: String) -> bool:
	for tk in tasks.values():
		if tk.def.room == room and tk.state != "solved":
			return false
	return true


func _direct() -> void:
	## The AI director: compares the team with the plan and escalates hints, or taunts after a long silence.
	var behind: float = t - layout.plan[stage] * 60.0
	var active := tasks.keys().filter(func(id): return tasks[id].state == "active")
	if active.is_empty():
		return
	if behind > 600.0 and hint_level < 3:
		hint_level = 3
		hints_used += 1
		var best: String = active[0]
		for id in active:
			if tasks[id].progress / tasks[id].need > tasks[best].progress / tasks[best].need:
				best = id
		say("KRUK", "Wchodzę im w system… Macie %s. Ruszajcie." % tasks[best].def.name)
		solve(best, "kruk")
	elif behind > 360.0 and hint_level < 2:
		hint_level = 2
		hint_factor = 1.6
		hints_used += 1
		say("KRUK", HINTS.get(active[0], "Skupcie się."))
	elif behind > 180.0 and hint_level < 1:
		hint_level = 1
		hint_factor = 1.3
		hints_used += 1
		say("KRUK", "Za wolno. " + HINTS.get(active[rng.randi() % active.size()], ""))
	elif t - last_event > 300.0 and t - last_taunt > 300.0:
		last_taunt = t
		# In order, so the same taunt never comes twice in a row.
		say("ZETA", ["Ciekawe, ile wytrzymacie bez powiadomień.", "Statystycznie poddajecie się w siedemnastej minucie.",
			"Mogę wam pomóc. Wystarczy się zalogować.", "Wasz czas ekranowy dziś: zero. Niepokojące."][taunts % 4])
		taunts += 1


func _run_twist(kind: String) -> void:
	twists_used.append(kind)
	match kind:
		"patrol":
			freeze_until = t + 20.0
			say("ZETA", "Wykryto ruch. Patrol w drodze. Nikt się nie rusza.")
			twist.emit(kind, 20.0)
		"zaciemnienie":
			dim_until = t + 60.0
			say("ZETA", "Oszczędzanie energii. Dobranoc.")
			twist.emit(kind, 60.0)
		"blokada":
			# Live rewiring, as the AI would do over MCP: a new puzzle, and the firewall now waits for it.
			var d := {"id": "blokada", "name": "Blokada drzwi", "room": "pietro", "work": 3.0,
				"spots": [[20.9, 6.6]], "max": 1, "after": [], "say": "Sekwencja przyjęta, drzwi puściły.", "opens": "zejscie"}
			tasks["blokada"] = {"def": d, "state": "locked", "progress": 0.0, "need": 3.0, "workers": []}
			tasks["firewall"].def.after.append("blokada")
			if tasks["firewall"].state == "active":
				tasks["firewall"].state = "locked"
				task_changed.emit("firewall", "locked")
			door_changed.emit("zejscie", false)
			done_at.erase("pietro")
			_enter_stage("pietro")
			say("ZETA", "Przywracam blokadę poziomu minus trzy.")
			say("KRUK", "Znowu zamknęła. Szukajcie panelu przy drzwiach.")
			twist.emit(kind, 0.0)
			_refresh()


func _finish(kind: String) -> void:
	ending = kind
	match kind:
		"detonacja":
			say("KRUK", "Wiejcie!")
			say("SYSTEM", "Jądro offline. Strefa się budzi.")
		"uklad":
			say("ZETA", "Witamy w rodzinie Z. Wasz abonament jest aktywny.")
		"czas":
			say("ZETA", "Ochrona jest w drodze.")
			say("KRUK", "Odjeżdżam. Przykro mi.")
	door_changed.emit("wyjscie", true)
	finished.emit(kind)


func _make_variant() -> void:
	## What the AI picks before the game; the screens in the room show the matching clues.
	var gest: String = ["Z", "S", "N", "U", "L", "7"][rng.randi() % 6]
	var people := ["Marek", "Ola", "Tomek"]
	var duty: String = people[rng.randi() % 3]
	var shifts := {}
	for p in people:
		var start: int = [6, 8, 10, 14, 22][rng.randi() % 5]
		shifts[p] = [start, (start + 8) % 24]
	var apps := ["Z-Feed", "Z-Bet", "Z-Date", "Z-News", "Z-Play", "Z-Fit"]
	var minutes := {}
	var pool := range(40, 261, 7)
	for a in apps:
		var i := rng.randi() % pool.size()
		minutes[a] = pool[i]
		pool.remove_at(i)
	var order := apps.duplicate()
	order.sort_custom(func(a, b): return minutes[a] > minutes[b])
	var profiles := [
		["Anna, 34 lata, bezsenność, dwoje dzieci", "Z-Boost, Z-Kids, Z-Snack, Z-Sen"],
		["Marta, 41 lat, stres w pracy", "Z-Calm, Z-Boost, Z-Snack, Z-Sen"],
		["Piotr, 23 lata, gra nocami", "Z-Boost, Z-Snack, Z-Boost Max, Z-Sen"],
		["Halina, 67 lat, mieszka sama", "Z-Calm, Z-News Print, Z-Snack, Z-Sen"],
	]
	var profile: Array = profiles[rng.randi() % profiles.size()]
	var scanners := [["1", "2"][rng.randi() % 2], ["3", "4"][rng.randi() % 2]]
	var sensors := [1, 2, 3, 4, 5]
	sensors.shuffle()
	sensors = sensors.slice(0, 3)
	sensors.sort()
	var words := ["Telefon", "Auto", "Sklep"]
	words.shuffle()
	variant = {
		"gest": gest, "dyzurny": duty, "zmiana": shifts[duty], "shifts": shifts, "minutes": minutes, "order": order,
		"kod": "SRV-%.1f/%d/%d" % [rng.randi_range(18, 32) / 10.0, rng.randi_range(12, 22) * 5, rng.randi_range(10, 60) * 100],
		"klientka": profile[0], "zakupy": profile[1], "skanery": scanners, "czujniki": sensors,
		"haslo": ". ".join(PackedStringArray(words)) + ".", "fragmenty": [rng.randi_range(10, 99), rng.randi_range(10, 99), rng.randi_range(10, 99)],
	}
	variant_lines = [
		"Gest odblokowania: %s" % gest,
		"Dyżur: %s, %02d–%02d (kod %02d%02d)" % [duty, shifts[duty][0], shifts[duty][1], shifts[duty][0], shifts[duty][1]],
		"Raport: %s" % " > ".join(PackedStringArray(order.slice(0, 3))) + " > …",
		"Kod serwisowy: %s" % variant.kod,
		"Klientka: %s" % profile[0],
		"Skanery dłoni: %s i %s" % scanners,
		"Czujniki firewalla: %s" % ", ".join(PackedStringArray(sensors.map(func(s): return str(s)))),
		"Hasło ZETY: %s" % variant.haslo,
		"Kod ładunku: %d %d %d" % variant.fragmenty,
	]


# ---------- engine mode ----------

func apply_engine_state(state: Dictionary) -> void:
	## The engine owns the truth: puzzle states, the clock and the ending.
	t = float(state.get("clock", t))
	var room_of := {}
	for p in state.puzzles:
		var id: String = ENGINE_TO_TASK.get(p.id, p.id)
		if not tasks.has(id):
			if id != "blokada":
				continue
			var d := {"id": "blokada", "name": "Blokada drzwi", "room": "pietro", "work": 3.5, "spots": [[20.9, 6.6]],
				"max": 1, "after": [], "say": "Sekwencja przyjęta, drzwi puściły."}
			tasks["blokada"] = {"def": d, "state": "locked", "progress": 0.0, "need": 3.5, "workers": []}
		var tk: Dictionary = tasks[id]
		room_of[id] = tk.def.room
		var st: String = p.status
		if id == "wybor" and tk.state == "solved":
			continue
		if st == "solved" and tk.state != "solved":
			solve(id, "")
		elif st != "solved" and st != tk.state:
			if st == "active" and id == "wybor" and tk.state == "active":
				continue
			tk.state = st
			if st == "locked":
				tk.progress = 0.0
			task_changed.emit(id, st)
	var current := stage
	for id in tasks:
		if tasks[id].state == "active":
			current = tasks[id].def.room
			break
	if current != stage:
		_enter_stage(current)
	if state.get("ended", false) and ending == "":
		var e: String = str(state.get("ending", ""))
		_finish({"detonacja": "detonacja", "uklad": "uklad"}.get(e, "czas"))


func rebuild_variant_lines() -> void:
	var v := variant
	var lines: Array[String] = []
	if v.has("gest"):
		lines.append("Gest odblokowania: %s" % v.gest)
	if v.has("dyzurny") and v.has("shifts") and v.shifts.has(v.dyzurny):
		var sh: Array = v.shifts[v.dyzurny]
		lines.append("Dyżur: %s, %02d–%02d (kod %02d%02d)" % [v.dyzurny, int(sh[0]), int(sh[1]), int(sh[0]), int(sh[1])])
	if v.has("minutes"):
		var order: Array = v.minutes.keys()
		order.sort_custom(func(a, b): return v.minutes[a] > v.minutes[b])
		lines.append("Raport: %s > …" % " > ".join(PackedStringArray(order.slice(0, 3))))
	if v.has("kod"):
		lines.append("Kod serwisowy: %s" % v.kod)
	if v.has("klientka"):
		lines.append("Klientka: %s" % v.klientka)
	if v.has("skanery"):
		lines.append("Skanery dłoni: %s" % " i ".join(PackedStringArray(v.skanery.map(func(x): return str(x)))))
	if v.has("czujniki"):
		lines.append("Czujniki firewalla: %s" % ", ".join(PackedStringArray(v.czujniki.map(func(x): return str(int(x))))))
	if v.has("haslo"):
		lines.append("Hasło ZETY: %s" % v.haslo)
	if v.has("fragmenty"):
		lines.append("Fragmenty kodu: %s" % " ".join(PackedStringArray(v.fragmenty.map(func(x): return str(int(x))))))
	variant_lines = lines
