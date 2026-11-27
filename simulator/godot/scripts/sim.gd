class_name Sim
extends Node
## Game clock, puzzle graph and the AI director from docs/scenario-the-core.md (hint ladder, twists, variant per game).

signal log_line(speaker: String, text: String)
signal task_changed(id: String, state: String)
signal door_changed(id: String, open: bool)
signal stage_changed(room: String)
signal twist(kind: String, seconds: float)
signal finished(kind: String)
signal submit(id: String)  # engine mode: the team believes it solved this; send the inputs to the engine

const TEAM := "Nomads of Route 7"
const GAME_SECONDS := 3600.0
const HINTS := {
	"search": "Badges turn up in corners. Check the planters and the sofa.",
	"phone": "The ad in the lobby draws a gesture. Repeat it on the phone.",
	"locker": "The roster says who's on shift today. The shift hours are the code.",
	"gate": "Only the duty guard's badge opens the gate.",
	"wall": "The screen time report. Line the phones up from the longest.",
	"zdrive": "The service code on the car's screen. The manual says how to read it.",
	"zmart": "The customer's profile and the posters. What would the algorithm push first?",
	"scanners": "Two lit scanners, two palms at once.",
	"doorlock": "The panel by the door. The sequence is in what ZETA says.",
	"firewall": "Mirrors. Every beam into a lit sensor.",
	"arming": "ZETA's slogan gives the order to enter the fragments.",
	"choice": "Two keys, at the same time. Or her button.",
}

var taunts := 0
var meddle := 1.0  # a visitor's hand on the team: 0 stalls them, 4 makes them fly
var meddle_until := 0.0
var force_detonation := false  # recordings need the loud ending every time
var layout: Dictionary
var rng := RandomNumberGenerator.new()
var t := 0.0
var running := false
var ending := ""
var tasks := {}  # id -> {def, state, progress, need, workers}
var variant := {}
var variant_lines: Array[String] = []
var stage := "lobby"
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
const WORK := 0.82  # people walk at a human pace on screen, which costs game minutes: the puzzles give them back
const ENGINE_TO_TASK := {"detonation": "choice", "deal": "choice"}


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
			"need": d.work * WORK * rng.randf_range(0.8, 1.25), "workers": []}
	_make_variant()
	_enter_stage("lobby")
	_refresh()
	say("RAVEN", "You're in. You've got an hour before security seals the floors.")
	say("ZETA", "Welcome to Z. Please wait for assistance.")


func say(speaker: String, text: String) -> void:
	log_line.emit(speaker, text)


func clock() -> String:
	var left := maxf(GAME_SECONDS - t, 0.0)
	return "%02d:%02d" % [int(left) / 60, int(left) % 60]


func rate_scale() -> float:
	if t < freeze_until:
		return 0.0
	return team_pace * hint_factor * (0.7 if t < dim_until else 1.0) * (meddle if t < meddle_until else 1.0)


func tick(dt: float) -> void:
	if not running or ending != "":
		return
	t += dt
	if t >= GAME_SECONDS:
		_finish("time")
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
		say("NOMADS", tk.def.say)
	if engine_mode:
		return  # doors, lines and the next puzzles come from the engine and the game master
	if tk.def.has("opens"):
		door_changed.emit(tk.def.opens, true)
	match id:
		"gate":
			say("ZETA", "Good morning, %s. Have a nice shift." % variant.duty)
		"scanners":
			say("RAVEN", "You're close. Head down.")
		"doorlock":
			say("ZETA", "Have it your way.")
		"arming":
			say("ZETA", "Take the rods out, and each of you gets a lifetime premium subscription.")
		"choice":
			_finish("detonation" if force_detonation or rng.randf() < 0.85 else "deal")
			return
	_refresh()
	var room: String = tk.def.room
	if _room_done(room) and not done_at.has(room):
		done_at[room] = t
		var ahead: float = layout.plan[room] * 60.0 - t
		var next_room: String = {"lobby": "habit", "habit": "core"}.get(room, "")
		if room == "habit" and ahead >= 300.0 and not tasks.has("doorlock"):
			_run_twist("lockdown")
		elif next_room != "":
			if ahead >= 300.0 and room == "lobby":
				pending_twist = {"kind": ["patrol", "blackout"][rng.randi() % 2], "at": t + rng.randf_range(90, 180)}
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
		"habit":
			say("ZETA", "The Habit Floor. This is where your everyday life is made.")
		"core":
			say("ZETA", "%s. I knew you would come." % TEAM)


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
			if id == "scanners":
				say("ZETA", "You are not authorised for level minus three.")


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
		say("RAVEN", "I'm getting into their system… You've got %s. Move." % tasks[best].def.name)
		solve(best, "raven")
	elif behind > 360.0 and hint_level < 2:
		hint_level = 2
		hint_factor = 1.6
		hints_used += 1
		say("RAVEN", HINTS.get(active[0], "Focus."))
	elif behind > 180.0 and hint_level < 1:
		hint_level = 1
		hint_factor = 1.3
		hints_used += 1
		say("RAVEN", "Too slow. " + HINTS.get(active[rng.randi() % active.size()], ""))
	elif t - last_event > 300.0 and t - last_taunt > 300.0:
		last_taunt = t
		# In order, so the same taunt never comes twice in a row.
		say("ZETA", ["I wonder how long you will last without notifications.", "Statistically, you give up in the seventeenth minute.",
			"I can help you. Just log in.", "Your screen time today: zero. Worrying."][taunts % 4])
		taunts += 1


func _run_twist(kind: String) -> void:
	twists_used.append(kind)
	match kind:
		"patrol":
			freeze_until = t + 20.0
			say("ZETA", "Movement detected. A patrol is on its way. Nobody moves.")
			twist.emit(kind, 20.0)
		"blackout":
			dim_until = t + 60.0
			say("ZETA", "Saving energy. Good night.")
			twist.emit(kind, 60.0)
		"lockdown":
			# Live rewiring, as the AI would do over MCP: a new puzzle, and the firewall now waits for it.
			var d := {"id": "doorlock", "name": "Door lockdown", "room": "habit", "work": 3.0,
				"spots": [[20.9, 6.6]], "max": 1, "after": [], "say": "Sequence accepted, the door released.", "opens": "descent"}
			tasks["doorlock"] = {"def": d, "state": "locked", "progress": 0.0, "need": 3.0, "workers": []}
			tasks["firewall"].def.after.append("doorlock")
			if tasks["firewall"].state == "active":
				tasks["firewall"].state = "locked"
				task_changed.emit("firewall", "locked")
			door_changed.emit("descent", false)
			done_at.erase("habit")
			_enter_stage("habit")
			say("ZETA", "Restoring the lockdown on level minus three.")
			say("RAVEN", "She's locked it again. Look for the panel by the door.")
			twist.emit(kind, 0.0)
			_refresh()


func _finish(kind: String) -> void:
	ending = kind
	match kind:
		"detonation":
			say("RAVEN", "It's lit! Run, you've got twelve seconds!")
		"deal":
			say("ZETA", "Welcome to the Z family. Your subscription is active.")
		"time":
			say("ZETA", "Security is on its way.")
			say("RAVEN", "I'm driving off. Sorry.")
	door_changed.emit("exit", true)
	finished.emit(kind)


func _make_variant() -> void:
	## What the AI picks before the game; the screens in the room show the matching clues.
	var gesture: String = ["Z", "S", "N", "U", "L", "7"][rng.randi() % 6]
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
		["Anna, 34, insomnia, two kids", "Z-Boost, Z-Kids, Z-Snack, Z-Sleep"],
		["Marta, 41, stress at work", "Z-Calm, Z-Boost, Z-Snack, Z-Sleep"],
		["Piotr, 23, games at night", "Z-Boost, Z-Snack, Z-Boost Max, Z-Sleep"],
		["Halina, 67, lives alone", "Z-Calm, Z-News Print, Z-Snack, Z-Sleep"],
	]
	var profile: Array = profiles[rng.randi() % profiles.size()]
	var scanners := [["1", "2"][rng.randi() % 2], ["3", "4"][rng.randi() % 2]]
	var sensors := [1, 2, 3, 4, 5]
	sensors.shuffle()
	sensors = sensors.slice(0, 3)
	sensors.sort()
	var words := ["Phone", "Car", "Store"]
	words.shuffle()
	variant = {
		"gesture": gesture, "duty": duty, "shift": shifts[duty], "shifts": shifts, "minutes": minutes, "order": order,
		"code": "SRV-%.1f/%d/%d" % [rng.randi_range(18, 32) / 10.0, rng.randi_range(12, 22) * 5, rng.randi_range(10, 60) * 100],
		"customer": profile[0], "shopping": profile[1], "scanners": scanners, "sensors": sensors,
		"slogan": ". ".join(PackedStringArray(words)) + ".", "fragments": [rng.randi_range(10, 99), rng.randi_range(10, 99), rng.randi_range(10, 99)],
	}
	variant_lines = [
		"Unlock gesture: %s" % gesture,
		"On duty: %s, %02d–%02d (code %02d%02d)" % [duty, shifts[duty][0], shifts[duty][1], shifts[duty][0], shifts[duty][1]],
		"Report: %s" % " > ".join(PackedStringArray(order.slice(0, 3))) + " > …",
		"Service code: %s" % variant.code,
		"Customer: %s" % profile[0],
		"Palm scanners: %s and %s" % scanners,
		"Firewall sensors: %s" % ", ".join(PackedStringArray(sensors.map(func(s): return str(s)))),
		"ZETA's slogan: %s" % variant.slogan,
		"Bomb code: %d %d %d" % variant.fragments,
	]


# ---------- engine mode ----------

func apply_engine_state(state: Dictionary) -> void:
	## The engine owns the truth: puzzle states, the clock and the ending.
	t = float(state.get("clock", t))
	var room_of := {}
	for p in state.puzzles:
		var id: String = ENGINE_TO_TASK.get(p.id, p.id)
		if not tasks.has(id):
			if id != "doorlock":
				continue
			var d := {"id": "doorlock", "name": "Door lockdown", "room": "habit", "work": 3.5, "spots": [[20.9, 6.6]],
				"max": 1, "after": [], "say": "Sequence accepted, the door released."}
			tasks["doorlock"] = {"def": d, "state": "locked", "progress": 0.0, "need": 3.5, "workers": []}
		var tk: Dictionary = tasks[id]
		room_of[id] = tk.def.room
		var st: String = p.status
		if id == "choice" and tk.state == "solved":
			continue
		if st == "solved" and tk.state != "solved":
			solve(id, "")
		elif st != "solved" and st != tk.state:
			if st == "active" and id == "choice" and tk.state == "active":
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
		_finish({"detonation": "detonation", "deal": "deal"}.get(e, "time"))


func rebuild_variant_lines() -> void:
	var v := variant
	var lines: Array[String] = []
	if v.has("gesture"):
		lines.append("Unlock gesture: %s" % v.gesture)
	if v.has("duty") and v.has("shifts") and v.shifts.has(v.duty):
		var sh: Array = v.shifts[v.duty]
		lines.append("On duty: %s, %02d–%02d (code %02d%02d)" % [v.duty, int(sh[0]), int(sh[1]), int(sh[0]), int(sh[1])])
	if v.has("minutes"):
		var order: Array = v.minutes.keys()
		order.sort_custom(func(a, b): return v.minutes[a] > v.minutes[b])
		lines.append("Report: %s > …" % " > ".join(PackedStringArray(order.slice(0, 3))))
	if v.has("code"):
		lines.append("Service code: %s" % v.code)
	if v.has("customer"):
		lines.append("Customer: %s" % v.customer)
	if v.has("scanners"):
		lines.append("Palm scanners: %s" % " and ".join(PackedStringArray(v.scanners.map(func(x): return str(x)))))
	if v.has("sensors"):
		lines.append("Firewall sensors: %s" % ", ".join(PackedStringArray(v.sensors.map(func(x): return str(int(x))))))
	if v.has("slogan"):
		lines.append("ZETA's slogan: %s" % v.slogan)
	if v.has("fragments"):
		lines.append("Code fragments: %s" % " ".join(PackedStringArray(v.fragments.map(func(x): return str(int(x))))))
	variant_lines = lines
