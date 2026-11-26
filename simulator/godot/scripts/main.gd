extends Node3D
## Builds the isometric simulator from layout.json and the Blender models, and plays a game of Jądro with simulated people.

const NAMES := ["Iskra", "Rdza", "Mysz", "Grom", "Lis"]
const COLORS := [Color("#fcee0a"), Color("#00f0ff"), Color("#ffffff"), Color("#ff5fd2"), Color("#9dff5c")]
const NEXT_ROOM := {"winda": "pietro", "zejscie": "jadro"}
const ISO_PITCH := -35.264
const CHAT := {
	"hol": ["Ten grafik coś znaczy.", "Reklama rysuje gest.", "Sprawdź donice.", "Kto ma dziś zmianę?"],
	"pietro": ["Raport mówi, kto ich trzyma.", "To auto ma tryb serwisowy.", "Plakaty mówią, co kupi.", "Ile mamy rdzeni?"],
	"jadro": ["Ustaw lustra.", "Nie ufaj jej.", "Kod z trzech fragmentów.", "Szybko, zanim ochrona…"],
}
const REPLIES := ["Racja.", "Sprawdzę.", "Daj mi chwilę.", "Nie jestem pewien.", "Dobra, idę."]

static var started_once := false

var layout: Dictionary
var nav: Nav
var sim: Sim
var hud: Hud
var screens: Screens
var fonts := {}
var props := {}
var doors := {}
var npcs: Array[Npc] = []
var room_lights := {}
var cones: Array[MeshInstance3D] = []
var atmosphere: Atmosphere
var reached := {"hol": true}
var rng := RandomNumberGenerator.new()
var speed := 5.0
var paused := false
var follow := true
var cam: Camera3D
var cam_target := Vector3(4.5, 0, 3.5)
var zoom := 13.0
var dragging := false
var music: Array[AudioStreamPlayer] = []
var music_active := 0
var music_on := true
var track := ""
var sfx: AudioStreamPlayer
var voice: AudioStreamPlayer  # the characters' spoken lines (engine mode, when the engine has a TTS voice)
var voice_queue: Array[Dictionary] = []
var voice_http: HTTPRequest
var alarm: AudioStreamPlayer
var last_zeta := ""
var core_mat: StandardMaterial3D
var bomb: Node3D
var mats := {}
var faces := {}
var red_alert := false
var end_shown_at := -1.0
var _clock := 0.0
var link: MqttLink
var engine_def := {}
var gm_text := ""
var _pending_start := false
var _nonce := ""
var _players_at := 0.0
const ROOM_ID := "jadro"
var _next_chat := 6.0


func _ready() -> void:
	layout = JSON.parse_string(FileAccess.get_file_as_string("res://layout.json"))
	fonts = {"display": load("res://fonts/ChakraPetch-Bold.ttf"), "text": load("res://fonts/Rajdhani-SemiBold.ttf"),
		"mono": load("res://fonts/JetBrainsMono[wght].ttf")}
	rng.randomize()
	nav = Nav.new(layout)
	hud = Hud.new()
	hud.layer = 2
	add_child(hud)
	hud.build(fonts)
	sim = Sim.new()
	sim.layout = layout
	add_child(sim)
	sim.log_line.connect(_on_log)
	sim.task_changed.connect(_on_task)
	sim.door_changed.connect(_on_door)
	sim.stage_changed.connect(_on_stage)
	sim.twist.connect(_on_twist)
	sim.finished.connect(_on_finished)
	sim.new_game(rng.randi())
	for id in sim.tasks:
		if not hud._rows.has(id):  # tasks that became active during new_game already have a row
			hud.add_task(id, sim.tasks[id].def.name, sim.tasks[id].def.room)
		hud.set_task(id, sim.tasks[id].state)
	hud.set_variant(sim.variant_lines)
	hud.start_requested.connect(_start)
	hud.speed_requested.connect(func(v):
		speed = v
		hud.set_speed(v)
		if link and link.is_open:
			link.publish("escaperoom/%s/control" % ROOM_ID, JSON.stringify({"speed": v})))
	hud.pause_toggled.connect(func(): paused = not paused)
	hud.new_game_requested.connect(func(): get_tree().reload_current_scene())
	hud.music_toggled.connect(func(on): music_on = on; _fade_music())
	hud.set_speed(speed)
	_build_environment()
	_build_world()
	_build_lights()
	_build_screens()
	_build_team()
	_calm(self, {})
	_build_audio()
	_build_camera()
	_aim_scanners_and_sensors()
	# Test hook: `godot --headless -- --autostart --speed=60` plays a whole game without clicks.
	for arg in OS.get_cmdline_user_args():
		if arg == "--autostart":
			started_once = true
		elif arg.begins_with("--speed="):
			speed = float(arg.get_slice("=", 1))
		elif arg.begins_with("--zoom="):
			zoom = float(arg.get_slice("=", 1))
		elif arg.begins_with("--shot="):
			_shoot(arg.get_slice("=", 1))
	_connect_engine()
	if started_once:
		hud.hide_start()
		_start()


func _shoot(path: String) -> void:
	## Test hook: `godot -- --autostart --shot=/tmp/x.png` saves a frame (and one screen texture) after a few seconds and quits.
	await get_tree().create_timer(6.0).timeout
	get_viewport().get_texture().get_image().save_png(path)
	for c in get_children():
		if c is SubViewport:
			c.get_texture().get_image().save_png(path.replace(".png", "_screen.png"))
			break
	get_tree().quit()


func _start() -> void:
	started_once = true
	sim.running = true
	_play(sim.stage)
	if link and link.is_open:
		_engine_start()
	else:
		_pending_start = link != null


# ---------- the engine link: the simulator as virtual hardware ----------

func _connect_engine() -> void:
	var url := ""
	for arg in OS.get_cmdline_user_args():
		if arg.begins_with("--mqtt="):
			url = arg.get_slice("=", 1)
	if url == "" and OS.has_feature("web"):
		url = str(JavaScriptBridge.eval("""(() => { const q = new URLSearchParams(location.search).get('mqtt');
			if (q !== null) return q;
			return ['localhost', '127.0.0.1'].includes(location.hostname) ? 'ws://localhost:9001' : 'wss://' + location.host + '/mqtt'; })()"""))
	if url == "" or url == "off":
		hud.set_director("TRYB OFFLINE\nSymulator bez silnika: reżyser na regułach lokalnych.")
		return
	link = MqttLink.new()
	add_child(link)
	link.connected.connect(_on_engine_connected)
	link.disconnected.connect(_on_engine_lost)
	link.message.connect(_on_engine_message)
	link.open(url, PackedStringArray(["escaperoom/%s/#" % ROOM_ID, "sim/%s/screen/#" % ROOM_ID, "sim/%s/fx/#" % ROOM_ID, "sim/%s/door/#" % ROOM_ID]))
	sim.submit.connect(_on_submit)


func _on_engine_connected() -> void:
	sim.engine_mode = true
	gm_text = "TRYB: SILNIK + AI GAME MASTER\nCzekam na start gry."
	if _pending_start:
		_pending_start = false
		_engine_start()


func _on_engine_lost() -> void:
	if not sim.engine_mode:
		link = null  # never connected: stay offline
		_pending_start = false
		hud.set_director("TRYB OFFLINE\nBrak połączenia z silnikiem: reżyser na regułach lokalnych.")
	else:
		gm_text = "UTRACONO POŁĄCZENIE Z SILNIKIEM"


func _engine_start() -> void:
	_nonce = "%d-%d" % [Time.get_unix_time_from_system(), rng.randi()]
	link.publish("escaperoom/%s/control" % ROOM_ID, JSON.stringify({"command": "start", "nonce": _nonce}))
	link.publish("escaperoom/%s/control" % ROOM_ID, JSON.stringify({"speed": speed}))


func _on_engine_message(topic: String, payload: String) -> void:
	var leaf := topic.get_slice("/", topic.get_slice_count("/") - 1)
	if topic.begins_with("sim/%s/door/" % ROOM_ID):
		_on_door(leaf, payload.strip_edges() == "open")
		return
	if not payload.begins_with("{"):
		return
	var data: Variant = JSON.parse_string(payload)
	if not data is Dictionary:
		return
	var d: Dictionary = data
	if topic.begins_with("sim/%s/screen/" % ROOM_ID):
		_engine_screen(leaf, d)
		return
	match topic:
		"escaperoom/%s/state" % ROOM_ID:
			# The broker keeps the state of older games; follow only the game our own start command began.
			if sim.running and str(d.get("nonce", "")) == _nonce:
				sim.apply_engine_state(d)
		"escaperoom/%s/definition" % ROOM_ID:
			engine_def = d
		"escaperoom/%s/voice" % ROOM_ID:
			_on_log(str(d.get("speaker", "ZETA")), str(d.get("text", "")))
			if str(d.get("audio", "")) != "":
				_queue_voice(str(d.get("speaker", "")), str(d.get("audio", "")))
			var target := _npc_named(str(d.get("to", "")))
			if target:
				target.emote("listen", 5.0)
				get_tree().create_timer(1.2).timeout.connect(func(): target.bark(["Jasne.", "Słyszę.", "Dobra, sprawdzam."][rng.randi() % 3], 1.8))
		"escaperoom/%s/hint" % ROOM_ID:
			var id: String = Sim.ENGINE_TO_TASK.get(str(d.get("puzzle", "")), str(d.get("puzzle", "")))
			sim.boosts[id] = sim.t + 150.0
		"escaperoom/%s/twist" % ROOM_ID:
			var secs := float(d.get("seconds", 0))
			match str(d.get("kind", "")):
				"patrol":
					sim.freeze_until = sim.t + secs
					_on_twist("patrol", secs)
				"blackout":
					sim.dim_until = sim.t + secs
				"lockdown":
					for n in npcs:
						if n.visible and rng.randf() < 0.5:
							n.bark("Zamknęła drzwi!", 2.0)
			if not sim.twists_used.has(str(d.get("kind", ""))):
				sim.twists_used.append(str(d.get("kind", "")))
		"escaperoom/%s/gm" % ROOM_ID:
			var actions: Array = d.get("actions", [])
			var mode := "MODEL " + str(d.get("model", "")) if d.get("mode", "") == "model" else "REGUŁY (bez modelu)"
			gm_text = "%s\n%s%s" % [mode, str(d.get("thought", "")), ("\n› " + "\n› ".join(PackedStringArray(actions.map(func(a): return str(a))))) if not actions.is_empty() else ""]
		"sim/%s/fx/scanners" % ROOM_ID:
			sim.variant.skanery = d.get("active", []).map(func(x): return str(x))
			_aim_scanners_and_sensors()
			_refresh_variant()
		"sim/%s/fx/sensors" % ROOM_ID:
			sim.variant.czujniki = d.get("active", []).map(func(x): return int(x))
			if sim.tasks.has("firewall") and sim.tasks.firewall.state == "active":
				_on_task("firewall", "active")
			_refresh_variant()


func _engine_screen(kind: String, d: Dictionary) -> void:
	## Clues come from the engine, so a rerolled puzzle shows its new clue at once.
	var v := sim.variant
	match kind:
		"reklama":
			v.gest = str(d.get("gest", v.get("gest", "Z")))
		"grafik":
			v.dyzurny = str(d.get("duty", ""))
			v.shifts = d.get("shifts", {})
		"raport":
			v.minutes = d.get("minutes", {})
		"zdrive":
			v.kod = str(d.get("kod", ""))
		"kiosk":
			v.klientka = str(d.get("klientka", ""))
		"fragmenty":
			v.fragmenty = d.get("fragments", [])
			v.haslo = str(d.get("haslo", ""))
		"zeta":
			last_zeta = str(d.get("line", last_zeta))
			return
	screens.refresh(kind, sim)
	_refresh_variant()


func _refresh_variant() -> void:
	sim.rebuild_variant_lines()
	hud.set_variant(sim.variant_lines)


func _on_submit(task_id: String) -> void:
	## The team thinks it has it: send what the devices would send for the puzzle's current solution.
	var engine_id := task_id
	if task_id == "wybor":
		engine_id = "detonacja" if rng.randf() < 0.85 else "uklad"
	var puzzle: Dictionary = {}
	for p in engine_def.get("puzzles", []):
		if p.id == engine_id:
			puzzle = p
	if puzzle.is_empty() or not puzzle.has("when"):
		return
	for pair in _inputs_for(puzzle.when):
		_send_input(pair[0], pair[1])


func _send_input(input: String, value: String) -> void:
	var io: Dictionary = engine_def.get("inputs", {}).get(input, {})
	if io.has("topic"):
		link.publish(io.topic, value)


func _inputs_for(cond: Dictionary) -> Array:
	if cond.has("sequence"):
		return cond.sequence.map(func(c): return [c.input, _value(c)])
	var wanted := {}
	_collect(cond, wanted)
	var out := []
	for input in wanted:
		out.append([input, _value(wanted[input])])
	return out


func _collect(cond: Dictionary, wanted: Dictionary) -> void:
	if cond.has("all"):
		for c in cond.all:
			_collect(c, wanted)
	elif cond.has("any"):
		_collect(cond.any[0], wanted)
	elif cond.has("input"):
		var w: Dictionary = wanted.get_or_add(cond.input, {})
		for k in ["equals", "gt", "lt"]:
			if cond.has(k):
				w[k] = cond[k]


func _value(w: Dictionary) -> String:
	if w.has("equals"):
		var e: Variant = w.equals
		return ("true" if e else "false") if typeof(e) == TYPE_BOOL else str(e)
	if w.has("gt") and w.has("lt"):
		return str(snappedf((float(w.gt) + float(w.lt)) / 2.0, 0.01))
	if w.has("gt"):
		return str(float(w.gt) + 100.0)
	if w.has("lt"):
		return str(float(w.lt) - 1.0)
	return "ON"


func _wrong_attempt(n: Npc) -> void:
	## People get it wrong before they get it right; the game master sees these attempts.
	var engine_id: String = "detonacja" if n.task_id == "wybor" else n.task_id
	for p in engine_def.get("puzzles", []):
		if p.id == engine_id and p.has("when"):
			var pairs := _inputs_for(p.when)
			if pairs.is_empty():
				return
			var pick: Array = pairs[rng.randi() % pairs.size()]
			_send_input(pick[0], str(rng.randi_range(1, 9)) if pick[1].length() <= 1 else str(rng.randi_range(1000, 9999)))
			n.bark(["Nie to…", "Źle!", "Jeszcze raz."][rng.randi() % 3], 1.6)
			return


func _publish_players() -> void:
	var list := []
	for n in npcs:
		if not n.visible:
			continue
		list.append({"name": n.display_name, "room": _room_at(n.position), "x": snappedf(n.position.x, 0.1), "z": snappedf(n.position.z, 0.1),
			"activity": n.activity, "task": n.task_id, "stuckMinutes": snappedf(n.stuck / 60.0, 0.1), "carrying": n.carry})
	link.publish("escaperoom/%s/players" % ROOM_ID, JSON.stringify(list))


func _room_at(p: Vector3) -> String:
	for r in layout.rooms:
		if p.x >= r.x - 0.2 and p.x <= r.x + r.w + 0.2 and p.z >= r.z - 0.2 and p.z <= r.z + r.d + 0.2:
			return r.id
	return "outside"


func _npc_named(name: String) -> Npc:
	for n in npcs:
		if n.display_name.to_lower() == name.to_lower():
			return n
	return null


# ---------- building ----------

func _build_environment() -> void:
	var env := Environment.new()
	env.background_mode = Environment.BG_COLOR
	env.background_color = Color("#040507")
	env.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	env.ambient_light_color = Color("#173236")
	env.ambient_light_energy = 0.75
	env.tonemap_mode = Environment.TONE_MAPPER_ACES
	env.tonemap_exposure = 0.95
	env.glow_enabled = true
	env.glow_intensity = 0.35
	env.glow_strength = 1.0
	env.glow_bloom = 0.0
	env.glow_hdr_threshold = 1.4
	env.glow_blend_mode = Environment.GLOW_BLEND_MODE_ADDITIVE
	# Depth fog only on the far plane: the rooms stay crisp, the city fades into the rain.
	env.fog_enabled = false
	env.fog_mode = Environment.FOG_MODE_DEPTH
	env.fog_light_color = Color("#16324a")
	env.fog_depth_begin = 90.0
	env.fog_depth_end = 340.0
	env.adjustment_enabled = true
	env.adjustment_contrast = 1.04
	env.adjustment_saturation = 0.8
	var we := WorldEnvironment.new()
	we.environment = env
	add_child(we)
	var moon := DirectionalLight3D.new()
	moon.rotation_degrees = Vector3(-58, -32, 0)
	moon.light_color = Color("#9fb4ff")
	moon.light_energy = 0.25
	moon.shadow_enabled = true
	moon.directional_shadow_max_distance = 70.0
	add_child(moon)
	atmosphere = Atmosphere.new()
	add_child(atmosphere)
	atmosphere.build(fonts.display, env)
	_build_post()


func _calm(node: Node, seen: Dictionary) -> void:
	## Halves the glow of every emissive material from the Blender assets: neon is an accent, not the light source.
	if node is MeshInstance3D:
		var mi: MeshInstance3D = node
		var mats: Array = [mi.material_override]
		if mi.mesh:
			for i in mi.mesh.get_surface_count():
				mats.append(mi.get_surface_override_material(i))
				mats.append(mi.mesh.surface_get_material(i))
		for m in mats:
			if m is BaseMaterial3D and m.emission_enabled and not seen.has(m.get_instance_id()):
				seen[m.get_instance_id()] = true
				m.emission_energy_multiplier *= 0.5
	for c in node.get_children():
		_calm(c, seen)


func _build_post() -> void:
	## Vignette, a touch of chromatic aberration at the edges, film grain and faint scanlines, under the HUD.
	var layer := CanvasLayer.new()
	layer.layer = 1
	add_child(layer)
	var rect := ColorRect.new()
	rect.set_anchors_preset(Control.PRESET_FULL_RECT)
	rect.mouse_filter = Control.MOUSE_FILTER_IGNORE
	var sh := Shader.new()
	sh.code = """
shader_type canvas_item;
uniform sampler2D screen_texture : hint_screen_texture, filter_linear;
float rand(vec2 co) { return fract(sin(dot(co, vec2(12.9898, 78.233))) * 43758.5453); }
void fragment() {
	vec2 uv = SCREEN_UV;
	vec2 d = uv - 0.5;
	float r = length(d);
	vec2 off = d * r * 0.003;
	vec3 col = vec3(texture(screen_texture, uv + off).r, texture(screen_texture, uv).g, texture(screen_texture, uv - off).b);
	col = mix(col, col * vec3(0.82, 1.05, 1.0) + vec3(0.0, 0.012, 0.016), 0.6);  // teal grade, like the reference
	col *= 1.0 - 0.55 * smoothstep(0.3, 0.82, r);
	col += (rand(uv * 731.0 + fract(TIME)) - 0.5) * 0.015;
	col *= 1.0 - 0.015 * (0.5 + 0.5 * sin(FRAGCOORD.y * 3.14159));
	COLOR = vec4(col, 1.0);
}
"""
	var m := ShaderMaterial.new()
	m.shader = sh
	rect.material = m
	layer.add_child(rect)


func _build_world() -> void:
	for r in layout.rooms:
		add_child(load("res://assets/room_%s.glb" % r.id).instantiate())
	for p in layout.props:
		var n: Node3D = load("res://assets/%s.glb" % p.model).instantiate()
		n.position = Vector3(p.x, p.get("y", 0.0), p.z)
		n.rotation_degrees.y = p.yaw
		add_child(n)
		props[p.id] = n
	for d in layout.doors:
		var leaf: Node3D = load("res://assets/door.glb").instantiate()
		var x: float = d.x + (-0.1 if d.wall == "west" else 0.1 if d.wall == "east" else 0.0)
		leaf.position = Vector3(x, 0, d.z)
		leaf.rotation_degrees.y = 90
		leaf.set_meta("closed_z", float(d.z))
		leaf.set_meta("width", float(d.width))
		add_child(leaf)
		doors[d.id] = leaf
		if d.id == "wejscie":
			leaf.position.z += d.width
	bomb = load("res://assets/bomb.glb").instantiate()
	bomb.position = Vector3(layout.bomb.x, 0, layout.bomb.z)
	bomb.scale = Vector3.ONE * 0.01
	bomb.visible = false
	add_child(bomb)
	var core: MeshInstance3D = props.jadro_wieza.find_child("Core", true, false)
	core_mat = (core.mesh.surface_get_material(0) as StandardMaterial3D).duplicate()
	core.material_override = core_mat


func _light(room: String, l: Light3D, pos: Vector3, color: Color, energy: float, reach: float, shadow := false) -> void:
	l.position = pos
	l.light_color = color
	l.light_energy = energy
	l.shadow_enabled = shadow
	l.set_meta("base", energy)
	l.set_meta("color", color)
	if l is OmniLight3D:
		l.omni_range = reach
	elif l is SpotLight3D:
		l.spot_range = reach
		l.spot_angle = 40.0
		l.rotation_degrees.x = -90
		_cone(room, pos, color)
	add_child(l)
	room_lights.get_or_add(room, []).append(l)


func _cone(room: String, pos: Vector3, color: Color) -> void:
	## Fake volumetric light: an additive cone that fades towards the floor and at its silhouette.
	var m := MeshInstance3D.new()
	var c := CylinderMesh.new()
	c.top_radius = 0.12
	c.bottom_radius = pos.y * tan(deg_to_rad(36.0))
	c.height = pos.y
	c.radial_segments = 24
	c.cap_top = false
	c.cap_bottom = false
	m.mesh = c
	m.position = Vector3(pos.x, pos.y / 2.0, pos.z)
	var sh := Shader.new()
	sh.code = """
shader_type spatial;
render_mode unshaded, blend_add, cull_disabled, depth_draw_never, shadows_disabled;
uniform vec4 tint : source_color;
uniform float strength = 0.05;
void fragment() {
	float edge = pow(abs(dot(NORMAL, VIEW)), 2.0);
	ALBEDO = tint.rgb * strength * edge * pow(1.0 - UV.y, 1.4);
}
"""
	var mat := ShaderMaterial.new()
	mat.shader = sh
	mat.set_shader_parameter("tint", color)
	m.material_override = mat
	m.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	add_child(m)
	m.set_meta("room", room)
	cones.append(m)


func _build_lights() -> void:
	var white := Color("#eef3ff")
	for i in 3:
		var p: Vector3 = [Vector3(2.2, 2.7, 2.6), Vector3(6.6, 2.7, 2.6), Vector3(4.5, 2.7, 5.6)][i]
		_light("hol", OmniLight3D.new(), p, white, 0.75, 6.5, i < 2)
	_light("hol", OmniLight3D.new(), Vector3(1.5, 2.2, 0.9), Color("#fcee0a"), 0.7, 3.5)
	for p in [Vector3(11.7, 3.1, 1.7), Vector3(18.6, 3.3, 2.6), Vector3(14.0, 3.1, 6.6), Vector3(15.2, 3.1, 3.4)]:
		_light("pietro", SpotLight3D.new(), p, white, 3.2, 6.0, true)
	for p in [Vector3(10.6, 2.4, 7.6), Vector3(20.4, 2.4, 7.6), Vector3(15.4, 2.6, 1.3)]:
		_light("pietro", OmniLight3D.new(), p, Color("#00f0ff"), 0.9, 6.0)
	for p in [Vector3(24.0, 2.6, 6.2), Vector3(29.0, 2.6, 6.2)]:
		_light("jadro", OmniLight3D.new(), p, Color("#3a64ff"), 0.9, 7.0)
	_light("jadro", OmniLight3D.new(), Vector3(26.3, 1.7, 3.6), Color("#00f0ff"), 2.0, 6.5, true)
	for p in [Vector3(23.4, 3.0, 2.4), Vector3(29.2, 3.0, 2.4)]:
		_light("jadro", SpotLight3D.new(), p, Color("#5f8bff"), 2.2, 5.0, true)


func _build_screens() -> void:
	screens = Screens.new(fonts)
	for p in layout.props:
		if not p.has("screen"):
			continue
		var mesh: MeshInstance3D = props[p.id].find_child("Screen", true, false)
		var box := mesh.mesh.get_aabb()
		var flat := box.size.y < box.size.z
		var dims := Vector2(box.size.x, box.size.z if flat else box.size.y)
		var px := Vector2i(dims * (300.0 / dims.y)) if dims.y * 420.0 < 300.0 else Vector2i(dims * 420.0)
		var vp := screens.make(p.screen, px, sim)
		add_child(vp)
		var quad := MeshInstance3D.new()
		var pm := PlaneMesh.new()
		pm.size = dims
		pm.orientation = PlaneMesh.FACE_Y if flat else PlaneMesh.FACE_Z
		quad.mesh = pm
		quad.position = box.get_center() + (Vector3(0, box.size.y / 2 + 0.002, 0) if flat else Vector3(0, 0, box.size.z / 2 + 0.002))
		var m := StandardMaterial3D.new()
		m.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED  # a screen emits its own picture; text stays sharp
		m.albedo_texture = vp.get_texture()
		m.albedo_color = Color(0.6, 0.6, 0.6)  # screens are props in a dark room, not the brightest thing in it
		quad.material_override = m
		mesh.add_child(quad)
		mesh.material_override = _glow(Color.BLACK, 0.0)


func _build_team() -> void:
	var variants: Array[PackedScene] = []
	for name in ["npc", "npc2", "npc3"]:
		if ResourceLoader.exists("res://assets/%s.glb" % name):
			variants.append(load("res://assets/%s.glb" % name))
	for i in NAMES.size():
		var n := Npc.new()
		n.setup(variants[i % variants.size()], NAMES[i], COLORS[i], rng.randf_range(0.8, 1.25), fonts.display)
		n.position = Vector3(1.0 + (i % 2) * 0.7, 0, 3.4 + i * 0.3)
		add_child(n)
		npcs.append(n)


func _build_audio() -> void:
	for i in 2:
		var p := AudioStreamPlayer.new()
		p.volume_db = -80.0
		add_child(p)
		music.append(p)
	sfx = AudioStreamPlayer.new()
	sfx.stream = _mp3("solve", false)
	sfx.volume_db = -7.0
	add_child(sfx)
	alarm = AudioStreamPlayer.new()
	alarm.stream = _mp3("alarm", true)
	alarm.volume_db = -16.0
	add_child(alarm)
	# Kruk talks over a radio from the van: band-limited and a little overdriven.
	var radio := AudioServer.bus_count
	AudioServer.add_bus(radio)
	AudioServer.set_bus_name(radio, "Radio")
	var hp := AudioEffectHighPassFilter.new()
	hp.cutoff_hz = 380.0
	var lp := AudioEffectLowPassFilter.new()
	lp.cutoff_hz = 3200.0
	var drive := AudioEffectDistortion.new()
	drive.mode = AudioEffectDistortion.MODE_OVERDRIVE
	drive.drive = 0.3
	drive.post_gain = -3.0
	for fx in [hp, lp, drive]:
		AudioServer.add_bus_effect(radio, fx)
	voice = AudioStreamPlayer.new()
	voice.volume_db = 0.0
	voice.finished.connect(_next_voice)
	add_child(voice)
	voice_http = HTTPRequest.new()
	voice_http.request_completed.connect(_on_voice_loaded)
	add_child(voice_http)


func _queue_voice(speaker: String, url: String) -> void:
	voice_queue.append({"speaker": speaker, "url": url})
	if voice_queue.size() == 1 and not voice.playing:
		voice_http.request(url)


func _on_voice_loaded(result: int, code: int, _headers: PackedStringArray, body: PackedByteArray) -> void:
	if voice_queue.is_empty():
		return
	var line: Dictionary = voice_queue[0]
	if result != HTTPRequest.RESULT_SUCCESS or code != 200:
		push_warning("voice line %s: HTTP %d" % [line.url, code])
		_next_voice()
		return
	var stream := AudioStreamMP3.new()
	stream.data = body
	voice.stream = stream
	voice.bus = "Radio" if line.speaker == "KRUK" else "Master"
	voice.play()
	create_tween().tween_property(music[music_active], "volume_db", _music_db() - 10.0, 0.3)  # duck the music


func _next_voice() -> void:
	if not voice_queue.is_empty():
		voice_queue.pop_front()
	if not voice_queue.is_empty():
		voice_http.request(voice_queue[0].url)
	else:
		_fade_music()


func _build_camera() -> void:
	cam = Camera3D.new()
	cam.projection = Camera3D.PROJECTION_ORTHOGONAL
	cam.size = zoom
	cam.near = 0.1
	cam.far = 300.0
	cam.rotation_degrees = Vector3(ISO_PITCH, 45, 0)
	add_child(cam)
	_place_camera()


func _aim_scanners_and_sensors() -> void:
	## The variant decides which scanners and sensors count; the puzzle spots follow it.
	var spots := []
	for k in sim.variant.skanery:
		var s: Node3D = props["skaner" + k]
		spots.append([s.position.x + (0.55 if s.position.x < 15.0 else -0.55), s.position.z])
		_set_mat("skaner" + k, "Ring", _glow(Color("#00f0ff"), 0.4))
	sim.tasks.skanery.def.spots = spots
	for i in 4:
		if not str(i + 1) in sim.variant.skanery:
			_set_mat("skaner%d" % (i + 1), "Ring", _glow(Color("#1a1d22"), 0.0))


# ---------- game events ----------

func _on_log(speaker: String, text: String) -> void:
	hud.add_log(speaker, text)
	if DisplayServer.get_name() == "headless":
		print("[%s] %s> %s" % [sim.clock(), speaker, text])
	if speaker == "ZETA":
		last_zeta = text
	if speaker == "KRUK" and sim.running and not npcs.is_empty():
		for n in npcs:
			n.emote("listen", 4.0)
		if rng.randf() < 0.4:
			npcs[rng.randi() % npcs.size()].bark("Słyszeliście?", 1.8)


func _on_task(id: String, state: String) -> void:
	if not hud._rows.has(id):
		hud.add_task(id, sim.tasks[id].def.name, sim.tasks[id].def.room)
	hud.set_task(id, state)
	if state == "active":
		for n in npcs:
			if n.mode == "explore":
				n.mode = "idle"
		_point_at(id)
		if id == "skanery":
			for k in sim.variant.skanery:
				_set_mat("skaner" + k, "Ring", _glow(Color("#00f0ff"), 3.0))
		if id == "firewall":
			for s in sim.variant.czujniki:
				_set_mat("czujniki", "Sensor%d" % (s - 1), _glow(Color("#00f0ff"), 3.0))
	if state == "solved" or state == "locked":
		var workers: Array = sim.tasks[id].workers.duplicate()
		for w in workers:
			w.task_id = ""
			w.at_spot = false
			w.mode = "idle"
			w.activity = "czeka"
			w.stuck = 0.0
			if state == "solved":
				w.emote("cheer", 5.0)
		sim.tasks[id].workers.clear()
		if state == "solved":
			_solved_effects(id, workers)


func _solved_effects(id: String, workers: Array) -> void:
	if sim.running:
		sfx.play()
	var first: Npc = workers[0] if not workers.is_empty() else null
	if first:
		first.bark(sim.tasks[id].def.say.split(".")[0] + ".", 3.0)
	var frag: Array = sim.variant.fragmenty
	match id:
		"przeszukanie":
			if first:
				first.set_carry("badge")
		"telefon":
			screens.solved("zphone")
		"szafka":
			_set_mat("szafka", "KeypadLight", _glow(Color("#00f0ff"), 3.0))
		"bramka":
			for flap in ["FlapL", "FlapR"]:
				var f: Node3D = props.bramka.find_child(flap, true, false)
				create_tween().tween_property(f, "scale:x", 0.05, 0.6)
			for n in npcs:
				if n.carry == "badge":
					n.set_carry("")
		"sciana":
			screens.solved("raport", frag[0])
			screens.solved("licznik")
			for i in 6:
				_set_mat("sciana", "SlotLight%d" % i, _glow(Color("#fcee0a"), 3.0))
		"zdrive":
			screens.solved("zdrive", frag[1])
			screens.solved("licznik")
			var lights: Node3D = props.zdrive.find_child("Headlights", true, false)
			var tw := create_tween().set_loops(4)
			tw.tween_callback(func(): lights.visible = false).set_delay(0.15)
			tw.tween_callback(func(): lights.visible = true).set_delay(0.15)
		"zmart":
			screens.solved("kiosk", frag[2])
			screens.solved("licznik")
		"skanery":
			for k in sim.variant.skanery:
				_set_mat("skaner" + k, "Ring", _glow(Color("#fcee0a"), 3.0))
		"firewall":
			for s in sim.variant.czujniki:
				_set_mat("czujniki", "Sensor%d" % (s - 1), _glow(Color("#fcee0a"), 3.0))
			_beams()
			for door_name in ["DoorL", "DoorR"]:
				var dn: Node3D = props.szafa_serwisowa.find_child(door_name, true, false)
				create_tween().tween_property(dn, "scale:x", 0.05, 0.8)
			bomb.visible = true
			create_tween().tween_property(bomb, "scale", Vector3.ONE, 0.8).set_trans(Tween.TRANS_BACK).set_ease(Tween.EASE_OUT)
		"uzbrojenie":
			for i in 3:
				var ring: MeshInstance3D = bomb.find_child("SlotRing%d" % i, true, false)
				ring.material_override = _glow(Color("#00f0ff"), 4.0)
			for n in npcs:
				if n.carry == "rod":
					n.set_carry("")
	if id in ["sciana", "zdrive", "zmart"] and first:
		first.set_carry("rod")


func _on_door(id: String, open: bool) -> void:
	nav.set_door(id, open)
	var leaf: Node3D = doors[id]
	var z: float = leaf.get_meta("closed_z") + (leaf.get_meta("width") if open else 0.0)
	create_tween().tween_property(leaf, "position:z", z, 1.2).set_trans(Tween.TRANS_SINE)
	if open and NEXT_ROOM.has(id):
		reached[NEXT_ROOM[id]] = true


func _on_stage(room: String) -> void:
	if sim.running:
		_play(room)


func _on_twist(kind: String, _seconds: float) -> void:
	for n in npcs:
		if kind == "patrol":
			n.bark("Nie ruszać się!", 3.0)


func _on_finished(kind: String) -> void:
	red_alert = kind == "detonacja"
	if kind == "detonacja":
		alarm.play()
		_play("final")
	elif kind == "uklad":
		_play("hol")
	else:
		_play("jadro")
	for n in npcs:
		n.task_id = ""
		n.at_spot = false
		n.mode = "leave"
		n.activity = "ucieka" if kind == "detonacja" else "wychodzi"
		n.go(nav, Vector3(32.2, 0, 4.6 + rng.randf_range(-0.3, 0.3)), Vector3(33, 0, 4.6))
	end_shown_at = _clock + 7.0


func _summary() -> String:
	var titles := {"detonacja": "DETONACJA", "uklad": "UKŁAD Z ZETĄ", "czas": "KONIEC CZASU"}
	var twists := ", ".join(PackedStringArray(sim.twists_used)) if not sim.twists_used.is_empty() else "brak"
	return "Zakończenie: %s\nCzas gry: %02d:%02d z 60:00\nPodpowiedzi Kruka: %d\nZwroty akcji: %s\nWariant: gest %s, dyżur %s, %s" % [
		titles[sim.ending], int(sim.t) / 60, int(sim.t) % 60, sim.hints_used, twists, sim.variant.gest, sim.variant.dyzurny, sim.variant.kod]


# ---------- per frame ----------

func _process(delta: float) -> void:
	_clock += delta
	var game_dt := delta * speed if sim.running and not paused and sim.ending == "" else 0.0
	sim.tick(game_dt)
	var move_dt := game_dt if sim.ending == "" else delta * 2.0
	for n in npcs:
		n.set_frozen(sim.t < sim.freeze_until and sim.ending == "")
		if game_dt > 0.0:
			_think(n)
		n.step(delta, move_dt)
		if n.at_spot and n.task_id != "" and game_dt > 0.0:
			n.stuck += game_dt
			if n.stuck > 420.0:
				n.stuck = 0.0
				n.emote("shrug", 6.0)
				n.bark(["To nie ma sensu…", "Ktoś ma pomysł?", "Utknęliśmy."][rng.randi() % 3], 2.4)
	_chat()
	_separate(delta)
	screens.update(sim, last_zeta)
	hud.set_clock(sim.clock(), sim.stage, int(layout.plan[sim.stage]), paused)
	hud.set_team(npcs)
	hud.set_director(gm_text if sim.engine_mode else _director_text())
	if sim.engine_mode and sim.running and sim.ending == "":
		if _clock >= _players_at:
			_players_at = _clock + 2.0
			_publish_players()
		if game_dt > 0.0:
			for n in npcs:
				if n.at_spot and n.task_id != "" and sim.t >= n.get_meta("next_try", 0.0):
					if n.has_meta("next_try") and rng.randf() < 0.3:
						_wrong_attempt(n)
					n.set_meta("next_try", sim.t + rng.randf_range(40.0, 90.0))
	_update_lights(delta)
	_update_camera(delta)
	if end_shown_at > 0.0 and _clock >= end_shown_at:
		end_shown_at = -1.0
		hud.show_end(_summary())
		if DisplayServer.get_name() == "headless":
			print(_summary())


func _think(n: Npc) -> void:
	if n.emoting() and n.mode in ["idle", "explore"]:
		return  # finish the reaction before picking the next thing to do
	if n.mode == "explore" and sim.t > n.explore_until:
		n.mode = "idle"
	if n.mode != "idle":
		return
	var best := ""
	var best_score := -INF
	for id in sim.tasks:
		var tk: Dictionary = sim.tasks[id]
		if tk.state != "active" or tk.workers.size() >= int(tk.def.max):
			continue
		var spot := _spot(id, tk.workers.size())
		var score: float = 2.0 * (int(tk.def.max) - tk.workers.size()) - 0.12 * n.position.distance_to(spot) + rng.randf() * 0.8
		if int(tk.def.get("coop", 0)) > tk.workers.size() and tk.workers.size() > 0:
			score += 3.0
		if score > best_score:
			best_score = score
			best = id
	if best != "":
		var tk: Dictionary = sim.tasks[best]
		var idx: int = tk.workers.size()
		if n.go(nav, _spot(best, idx), _face(best, idx)):
			tk.workers.append(n)
			n.task_id = best
			n.mode = "task"
			n.activity = tk.def.name.to_lower()
			if rng.randf() < 0.6:
				n.bark(["Biorę to.", "Ja to zrobię.", "Mam pomysł.", "Sprawdzam."][rng.randi() % 4])
			return
	var r: Dictionary = layout.rooms.filter(func(x): return x.id == sim.stage)[0]
	var p := Vector3(rng.randf_range(r.x + 0.8, r.x + r.w - 0.8), 0, rng.randf_range(r.z + 1.0, r.z + r.d - 0.8))
	if nav.is_open(p) and n.go(nav, p, p + Vector3(rng.randf_range(-1, 1), 0, -1)):
		n.mode = "explore"
		n.task_id = ""
		n.explore_until = sim.t + rng.randf_range(20.0, 50.0)
		n.activity = "szuka wskazówek"
		if rng.randf() < 0.25:
			n.bark(["Coś tu musi być.", "Szukam wskazówek.", "Patrzcie na ekrany."][rng.randi() % 3])


func _chat() -> void:
	## Every few seconds two people looking around stop, face each other and trade a line.
	if _clock < _next_chat or not sim.running or sim.ending != "" or paused:
		return
	_next_chat = _clock + rng.randf_range(6.0, 14.0)
	var free := npcs.filter(func(n): return n.mode == "explore" and not n.emoting())
	for a in free:
		for b in free:
			if a != b and a.position.distance_to(b.position) < 3.0:
				a.emote("talk", 12.0, b.position)
				b.emote("talk", 12.0, a.position)
				var lines: Array = CHAT[sim.stage]
				a.bark(lines[rng.randi() % lines.size()], 2.2)
				get_tree().create_timer(1.4).timeout.connect(func(): b.bark(REPLIES[rng.randi() % REPLIES.size()], 1.8))
				return


func _point_at(id: String) -> void:
	## Whoever is closest to a puzzle that just opened points it out to the others.
	var target := _face(id, 0)
	var best: Npc = null
	for n in npcs:
		if n.mode in ["explore", "idle"] and (best == null or n.position.distance_to(target) < best.position.distance_to(target)):
			best = n
	if best and best.position.distance_to(target) < 7.0:
		best.emote("point", 4.0, target)
		best.bark(["Tam!", "Patrzcie na to!", "Coś się otworzyło."][rng.randi() % 3], 1.8)


func _spot(id: String, idx: int) -> Vector3:
	var spots: Array = sim.tasks[id].def.spots
	var s: Array = spots[idx % spots.size()]
	return Vector3(s[0], 0, s[1])


func _face(id: String, idx: int) -> Vector3:
	## Workers face the nearest piece of furniture to their spot.
	var spot := _spot(id, idx)
	var key := "%s/%d" % [id, idx]
	if not faces.has(key):
		var best := spot + Vector3(0, 0, -1)
		var dist := INF
		for p in props.values():
			var d := Vector2(p.position.x - spot.x, p.position.z - spot.z).length()
			if d < dist and d > 0.2:
				dist = d
				best = Vector3(p.position.x, 0, p.position.z)
		faces[key] = best
	return faces[key]


func _separate(delta: float) -> void:
	for i in npcs.size():
		for j in range(i + 1, npcs.size()):
			var a := npcs[i]
			var b := npcs[j]
			if (a.at_spot and b.at_spot) or not a.visible or not b.visible:
				continue
			var d := Vector3(b.position.x - a.position.x, 0, b.position.z - a.position.z)
			if d.length() < 0.55 and d.length() > 0.001:
				var push := d.normalized() * (0.55 - d.length()) * minf(1.0, delta * 6.0)
				if not a.at_spot:
					a.position -= push
				if not b.at_spot:
					b.position += push


func _director_text() -> String:
	var plan_end: float = layout.plan[sim.stage] * 60.0
	var gap := sim.t - plan_end
	var lines := []
	if gap > 0:
		lines.append("Spóźnienie względem planu: %d:%02d" % [int(gap) / 60, int(gap) % 60])
	else:
		lines.append("Do końca planu pokoju: %d:%02d" % [int(-gap) / 60, int(-gap) % 60])
	lines.append("Podpowiedzi Kruka: %d (poziom %d)" % [sim.hints_used, sim.hint_level])
	lines.append("Tempo drużyny: ×%.1f" % sim.rate_scale())
	lines.append("Zwroty akcji: %s" % (", ".join(PackedStringArray(sim.twists_used)) if not sim.twists_used.is_empty() else "brak"))
	return "\n".join(PackedStringArray(lines))


func _update_lights(delta: float) -> void:
	var pulse := 0.5 + 0.5 * sin(_clock * 6.0)
	for room in room_lights:
		var factor := 1.0 if reached.has(room) else 0.22
		if sim.t < sim.dim_until and room == sim.stage:
			factor *= 0.2
		for l: Light3D in room_lights[room]:
			var target: float = l.get_meta("base") * factor
			var c: Color = l.get_meta("color")
			if red_alert:
				target = (0.6 + 2.2 * pulse)
				c = Color("#ff1a1a")
			l.light_energy = lerpf(l.light_energy, target, minf(1.0, delta * 4.0))
			l.light_color = l.light_color.lerp(c, minf(1.0, delta * 4.0))
	core_mat.emission_energy_multiplier = (5.0 + 1.8 * sin(_clock * 2.2)) if reached.has("jadro") else 1.5
	for cone in cones:
		cone.visible = reached.has(cone.get_meta("room")) and not red_alert


func _beams() -> void:
	var emitter: Node3D = props.emiter
	var start := emitter.position + Vector3(0, 1.0, 0)
	var mirrors := ["lustro1", "lustro2", "lustro3"]
	var stand: Node3D = props.czujniki
	for i in 3:
		var s: int = sim.variant.czujniki[i] - 1
		var mirror: Vector3 = props[mirrors[i]].position + Vector3(0, 1.05, 0)
		var sensor := stand.to_global(Vector3(0.18, 1.4 - s * 0.25, -0.6 + s * 0.3))
		_beam(start, mirror)
		_beam(mirror, sensor)


func _beam(a: Vector3, b: Vector3) -> void:
	var holder := Node3D.new()
	add_child(holder)
	holder.look_at_from_position((a + b) / 2.0, b, Vector3.UP)
	var m := MeshInstance3D.new()
	var c := CylinderMesh.new()
	c.top_radius = 0.012
	c.bottom_radius = 0.012
	c.height = a.distance_to(b)
	c.radial_segments = 8
	m.mesh = c
	m.rotation_degrees.x = 90
	m.material_override = _glow(Color("#00f0ff"), 5.0)
	holder.add_child(m)


# ---------- camera and input ----------

func _place_camera() -> void:
	cam.global_position = cam_target + cam.global_transform.basis.z * 80.0


func _update_camera(delta: float) -> void:
	if follow:
		var center := Vector3.ZERO
		var count := 0
		for n in npcs:
			if n.visible:
				center += n.position
				count += 1
		if count > 0:
			cam_target = cam_target.lerp(center / count + Vector3(0, 0, -0.5), minf(1.0, delta * 1.2))
	cam.size = lerpf(cam.size, zoom, minf(1.0, delta * 6.0))
	_place_camera()
	atmosphere.follow(cam_target)


func _unhandled_input(e: InputEvent) -> void:
	var mb := e as InputEventMouseButton
	var mm := e as InputEventMouseMotion
	var key := e as InputEventKey
	if mb:
		if mb.button_index in [MOUSE_BUTTON_LEFT, MOUSE_BUTTON_RIGHT, MOUSE_BUTTON_MIDDLE]:
			dragging = mb.pressed
			if mb.pressed:
				follow = false
		elif mb.pressed and mb.button_index == MOUSE_BUTTON_WHEEL_UP:
			zoom = clampf(zoom * 0.9, 5.0, 32.0)
		elif mb.pressed and mb.button_index == MOUSE_BUTTON_WHEEL_DOWN:
			zoom = clampf(zoom * 1.1, 5.0, 32.0)
	elif mm and dragging:
		var px := cam.size / get_viewport().get_visible_rect().size.y
		var right := cam.global_transform.basis.x
		var up := Vector3(cam.global_transform.basis.y.x, 0, cam.global_transform.basis.y.z).normalized()
		cam_target += (-right * mm.relative.x + up * mm.relative.y / sin(deg_to_rad(-ISO_PITCH))) * px
	elif key and key.pressed and not key.echo:
		match key.keycode:
			KEY_1, KEY_2, KEY_3:
				var r: Dictionary = layout.rooms[key.keycode - KEY_1]
				cam_target = Vector3(r.x + r.w / 2.0, 0, r.z + r.d / 2.0)
				follow = false
			KEY_F:
				follow = not follow
			KEY_H:
				hud.visible = not hud.visible
			KEY_SPACE:
				paused = not paused


# ---------- helpers ----------

func _set_mat(prop: String, node: String, m: Material) -> void:
	var n := props[prop].find_child(node, true, false) as MeshInstance3D
	if n:
		n.material_override = m


func _glow(c: Color, energy: float) -> StandardMaterial3D:
	var key := "%s/%.1f" % [c.to_html(), energy]
	if not mats.has(key):
		var m := StandardMaterial3D.new()
		m.albedo_color = c
		if energy > 0.0:
			m.emission_enabled = true
			m.emission = c
			m.emission_energy_multiplier = energy
		mats[key] = m
	return mats[key]


func _mp3(name: String, loop: bool) -> AudioStreamMP3:
	var s := (load("res://audio/%s.mp3" % name) as AudioStreamMP3).duplicate() as AudioStreamMP3
	s.loop = loop
	return s


func _play(name: String) -> void:
	if name == track:
		return
	track = name
	var outgoing := music[music_active]
	music_active = 1 - music_active
	var incoming := music[music_active]
	incoming.stream = _mp3(name, true)
	incoming.volume_db = -50.0
	incoming.play()
	var tw := create_tween().set_parallel()
	tw.tween_property(incoming, "volume_db", _music_db(), 2.5)
	tw.tween_property(outgoing, "volume_db", -80.0, 2.5)


func _music_db() -> float:
	return -9.0 if music_on else -80.0


func _fade_music() -> void:
	create_tween().tween_property(music[music_active], "volume_db", _music_db(), 0.5)
