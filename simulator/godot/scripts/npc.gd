class_name Npc
extends Node3D
## One simulated player: walks the nav grid, works at puzzle spots, carries items, says things.

const WALK_SPEED := 1.5  # metres a second on screen, at any game speed: bodies never fast-forward
const STRIDE := 1.5  # metres per walk cycle (two steps), so the feet do not slide

var display_name := ""
var color := Color.WHITE
var skill := 1.0
var task_id := ""
var face_point := Vector3.ZERO
var at_spot := false
var mode := "idle"  # idle | walk | explore | leave | gone
var explore_until := 0.0
var activity := "waiting"
var stuck := 0.0  # game seconds at the same spot without a solve
var carry := ""

var _path := PackedVector3Array()
var _path_i := 0
var _anim := randf() * 10.0  # idle clock; everyone starts somewhere else in it, so nobody moves in step
var _phase := randf() * TAU  # walk cycle, advanced by the distance walked
var _speed := 0.0
var _real := 0.0
var _bark: Label3D
var _bark_until := 0.0
var _legs: Array[Node3D] = []
var _arms: Array[Node3D] = []
var _head: Node3D
var _item: MeshInstance3D
var _ring: StandardMaterial3D  # the mark on the floor in the player's colour; red while a patrol holds them still
var _frozen := false
var _model: Node3D
var _emote := ""  # cheer | listen | shrug | talk | point
var _emote_until := 0.0
var _look_at := Vector3.ZERO


func setup(model_scene: PackedScene, who: String, tint: Color, skill_: float, font: Font, build := 1.0) -> void:
	display_name = who
	color = tint
	skill = skill_
	var model: Node3D = model_scene.instantiate()
	_model = model
	model.scale = Vector3.ONE * build  # people are not all the same size
	add_child(model)
	for n in ["LegL", "LegR"]:
		_legs.append(model.find_child(n, true, false))
	for n in ["ArmL", "ArmR"]:
		_arms.append(model.find_child(n, true, false))
	_head = model.find_child("Head", true, false)
	# The rooms are dark and half the crew wears black: a little of every colour shows by itself, so nobody is lost.
	for mi in model.find_children("*", "MeshInstance3D", true, false):
		for i in mi.mesh.get_surface_count():
			var m := mi.mesh.surface_get_material(i) as StandardMaterial3D
			if m and not m.emission_enabled:
				m.emission_enabled = true
				m.emission = m.albedo_color.lightened(0.08)
				m.emission_texture = m.albedo_texture
				m.emission_energy_multiplier = 0.32
	var ring := MeshInstance3D.new()
	var torus := TorusMesh.new()
	torus.inner_radius = 0.34
	torus.outer_radius = 0.4
	torus.rings = 32
	ring.mesh = torus
	ring.scale = Vector3(1, 0.05, 1)
	ring.position.y = 0.02
	var rm := StandardMaterial3D.new()
	rm.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	rm.albedo_color = tint
	ring.material_override = rm
	_ring = rm
	add_child(ring)
	_bark = Label3D.new()
	_bark.font = font
	_bark.font_size = 44
	_bark.pixel_size = 0.0045
	_bark.outline_size = 10
	_bark.outline_modulate = Color(0, 0, 0, 0.9)
	_bark.modulate = tint.lightened(0.35)
	_bark.billboard = BaseMaterial3D.BILLBOARD_ENABLED
	_bark.no_depth_test = true
	_bark.position.y = 2.25
	_bark.visible = false
	add_child(_bark)
	_item = MeshInstance3D.new()
	var cyl := CylinderMesh.new()
	cyl.top_radius = 0.045
	cyl.bottom_radius = 0.045
	cyl.height = 0.3
	_item.mesh = cyl
	var im := StandardMaterial3D.new()
	im.emission_enabled = true
	im.emission = Color("#00f0ff")
	im.emission_energy_multiplier = 3.0
	im.albedo_color = Color("#00f0ff")
	_item.material_override = im
	_item.position = Vector3(0, -0.66, 0.08)
	_item.visible = false
	_arms[1].add_child(_item)


func bark(text: String, seconds := 2.6) -> void:
	_bark.text = text
	_bark.visible = true
	_bark_until = Time.get_ticks_msec() / 1000.0 + seconds


func emote(kind: String, seconds: float, look := Vector3.ZERO) -> void:
	## A short body-language moment. It lasts real seconds at any game speed (a gesture cut to a fraction of a
	## second reads as a twitch), half the game seconds asked for, between 1.6 and 4.
	_emote = kind
	_emote_until = _real + clampf(seconds * 0.5, 1.6, 4.0)
	_look_at = look


func emoting() -> bool:
	return _emote != "" and _real < _emote_until


func set_carry(what: String) -> void:
	carry = what
	_item.visible = what != ""
	if what == "badge":
		_item.scale = Vector3(1.4, 0.25, 0.2)
		(_item.material_override as StandardMaterial3D).emission = Color("#fcee0a")
	elif what == "rod":
		_item.scale = Vector3.ONE
		(_item.material_override as StandardMaterial3D).emission = Color("#00f0ff")


func go(nav: Nav, target: Vector3, face: Vector3) -> bool:
	_path = nav.path(position, target)
	_path_i = 0
	face_point = face
	at_spot = false
	return not _path.is_empty()


func set_frozen(on: bool) -> void:
	_frozen = on
	_ring.albedo_color = Color("#ff0000") if on else color


func step(dt: float, walk_dt: float) -> void:
	## dt drives the animation; walk_dt moves the body: real time while the game runs (more in the finale's run
	## for the exit), zero while it is paused. A faster game makes puzzles go faster, not legs.
	_real += dt
	if _bark.visible and Time.get_ticks_msec() / 1000.0 > _bark_until:
		_bark.visible = false
	if mode == "gone":
		return
	var moving := false
	var talking := emoting() and _emote in ["talk", "point", "listen"]  # with somewhere to look, they stop and turn to it
	if talking and _look_at != Vector3.ZERO:
		var lf := _look_at - position
		rotation.y = lerp_angle(rotation.y, atan2(lf.x, lf.z), minf(1.0, dt * 6.0))
	elif not _frozen and _path_i < _path.size() and walk_dt > 0.0:
		var target := _path[_path_i]
		var to := target - position
		to.y = 0.0
		var last := _path_i == _path.size() - 1
		var heading := atan2(to.x, to.z)
		# Ease out into the last point, and slow down to turn instead of sliding sideways.
		var want := WALK_SPEED * (walk_dt / dt) * (clampf(to.length() / 0.7, 0.3, 1.0) if last else 1.0)
		want *= clampf(1.25 - absf(angle_difference(rotation.y, heading)) / 1.6, 0.25, 1.0)
		_speed = move_toward(_speed, want, 5.0 * dt)
		rotation.y = rotate_toward(rotation.y, heading, 7.0 * dt)
		var reach := _speed * dt
		if to.length() <= maxf(reach, 0.03) or (not last and to.length() < 0.2):  # corners are rounded, the end is exact
			if last:
				position = Vector3(target.x, 0, target.z)
			_path_i += 1
			if _path_i >= _path.size():
				_arrive()
		else:
			position += to.normalized() * reach
			_phase += reach / STRIDE * TAU
			moving = true
	elif at_spot and not _frozen:
		var f := face_point - position
		if f.length() > 0.05:
			rotation.y = lerp_angle(rotation.y, atan2(f.x, f.z), minf(1.0, dt * 6.0))
	if not moving:
		_speed = move_toward(_speed, 0.0, 8.0 * dt)
	_animate(dt)


func _arrive() -> void:
	if mode == "leave":
		mode = "gone"
		visible = false
	elif task_id != "":
		at_spot = true


func _animate(dt: float) -> void:
	## Every joint eases towards the pose of the moment, so walking, working and gestures flow into each other.
	_anim += dt * 3.0
	var gait := clampf(_speed / WALK_SPEED, 0.0, 2.0)  # 0 standing, 1 walking, 2 running for the exit
	var swing := sin(_phase)
	# [left arm x, right arm x, arms out, head x, head y, hop]
	var pose := [-swing * 0.3 * gait, swing * 0.3 * gait - (0.35 if carry != "" else 0.0), 0.05, 0.0, 0.0, 0.0]
	if gait < 0.15:
		pose[4] = sin(_anim * 0.23) * 0.4  # standing people look around
		if emoting():
			match _emote:
				"cheer":
					pose = [-2.7 + sin(_anim * 2.5) * 0.15, -2.7 - sin(_anim * 2.5) * 0.15, 0.25, -0.25, 0.0, absf(sin(_anim * 2.0)) * 0.14]
				"listen":
					pose = [-0.1, -0.3, 0.05, -0.3, 0.3, 0.0]
				"shrug":
					pose = [-0.3, -0.3, 0.45, 0.15 + sin(_anim * 1.5) * 0.06, sin(_anim * 0.9) * 0.3, 0.0]
				"talk":
					pose = [-0.5 + sin(_anim * 1.6) * 0.14, -0.15, 0.1, sin(_anim * 1.1) * 0.06, 0.0, 0.0]
				"point":
					pose = [-0.05, -1.45, 0.05, -0.1, 0.0, 0.0]
		elif at_spot and task_id != "" and not _frozen:
			pose = [-0.95 + sin(_anim * 1.3) * 0.18, -0.7 + sin(_anim * 0.9 + 1.0) * 0.22, 0.05, 0.28, sin(_anim * 0.4) * 0.12, 0.0]
	var k := 1.0 - exp(-dt * 10.0)
	_arms[0].rotation.x = lerpf(_arms[0].rotation.x, pose[0], k)
	_arms[1].rotation.x = lerpf(_arms[1].rotation.x, pose[1], k)
	_arms[0].rotation.z = lerpf(_arms[0].rotation.z, -pose[2], k)
	_arms[1].rotation.z = lerpf(_arms[1].rotation.z, pose[2], k)
	_head.rotation.x = lerpf(_head.rotation.x, pose[3], k)
	_head.rotation.y = lerpf(_head.rotation.y, pose[4], k)
	# Legs follow the ground directly; the body bobs once a step, leans into the walk and breathes when it stands.
	_legs[0].rotation.x = swing * 0.5 * minf(gait, 1.3)
	_legs[1].rotation.x = -_legs[0].rotation.x
	_model.rotation.x = lerpf(_model.rotation.x, 0.06 * gait, k)
	_model.position.y = lerpf(_model.position.y, pose[5] + absf(swing) * 0.035 * minf(gait, 1.3) + sin(_anim * 0.5) * 0.008, k)
