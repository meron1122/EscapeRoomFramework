class_name Npc
extends Node3D
## One simulated player: walks the nav grid, works at puzzle spots, carries items, says things.

const WALK_SPEED := 1.35

var display_name := ""
var color := Color.WHITE
var skill := 1.0
var task_id := ""
var face_point := Vector3.ZERO
var at_spot := false
var mode := "idle"  # idle | walk | explore | leave | gone
var explore_until := 0.0
var activity := "czeka"
var stuck := 0.0  # game seconds at the same spot without a solve
var carry := ""

var _path := PackedVector3Array()
var _path_i := 0
var _anim := 0.0
var _bark: Label3D
var _bark_until := 0.0
var _legs: Array[Node3D] = []
var _arms: Array[Node3D] = []
var _head: Node3D
var _item: MeshInstance3D
var _visor: StandardMaterial3D
var _frozen := false
var _model: Node3D
var _emote := ""  # cheer | listen | shrug | talk | point
var _emote_until := 0.0
var _look_at := Vector3.ZERO
var _game_time := 0.0


func setup(model_scene: PackedScene, who: String, tint: Color, skill_: float, font: Font) -> void:
	display_name = who
	color = tint
	skill = skill_
	var model: Node3D = model_scene.instantiate()
	_model = model
	add_child(model)
	for n in ["LegL", "LegR"]:
		_legs.append(model.find_child(n, true, false))
	for n in ["ArmL", "ArmR"]:
		_arms.append(model.find_child(n, true, false))
	_head = model.find_child("Head", true, false)
	for mi in model.find_children("*", "MeshInstance3D", true, false):
		for s in mi.mesh.get_surface_count():
			var m: Material = mi.mesh.surface_get_material(s)
			if m == null:
				continue
			if m.resource_name == "Jacket":
				var c := (m as StandardMaterial3D).duplicate() as StandardMaterial3D
				c.albedo_color = tint.darkened(0.35)
				mi.set_surface_override_material(s, c)
			elif m.resource_name == "Visor":
				_visor = (m as StandardMaterial3D).duplicate() as StandardMaterial3D
				mi.set_surface_override_material(s, _visor)
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
	## A short body-language moment, lasting game seconds like a real person's would.
	_emote = kind
	_emote_until = _game_time + seconds
	_look_at = look


func emoting() -> bool:
	return _emote != "" and _game_time < _emote_until


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
	if _visor:
		_visor.emission = Color("#ff0000") if on else Color("#00f0ff")
		_visor.albedo_color = _visor.emission


func step(dt: float, game_dt: float) -> void:
	## dt drives animation in real time; game_dt moves the body in game time, so speed-ups stay consistent.
	_game_time += game_dt
	if _bark.visible and Time.get_ticks_msec() / 1000.0 > _bark_until:
		_bark.visible = false
	if mode == "gone":
		return
	var moving := false
	var talking := emoting() and _emote in ["talk", "point"]
	if talking and _look_at != Vector3.ZERO:
		var lf := _look_at - position
		rotation.y = lerp_angle(rotation.y, atan2(lf.x, lf.z), minf(1.0, dt * 8.0))
	elif not _frozen and _path_i < _path.size():
		var target := _path[_path_i]
		var to := target - position
		to.y = 0.0
		var reach := WALK_SPEED * game_dt
		if to.length() <= reach:
			position = Vector3(target.x, 0, target.z)
			_path_i += 1
			if _path_i >= _path.size():
				_arrive()
		else:
			position += to.normalized() * reach
			moving = true
			rotation.y = lerp_angle(rotation.y, atan2(to.x, to.z), minf(1.0, dt * 12.0))
	elif at_spot and not _frozen:
		var f := face_point - position
		if f.length() > 0.05:
			rotation.y = lerp_angle(rotation.y, atan2(f.x, f.z), minf(1.0, dt * 8.0))
	_animate(dt, moving)


func _arrive() -> void:
	if mode == "leave":
		mode = "gone"
		visible = false
	elif task_id != "":
		at_spot = true


func _animate(dt: float, moving: bool) -> void:
	_anim += dt * (9.0 if moving else 3.0)
	var swing := sin(_anim) * (0.55 if moving else 0.0)
	_legs[0].rotation.x = swing
	_legs[1].rotation.x = -swing
	if emoting() and not moving:
		_arms[0].rotation.z = 0.0
		_arms[1].rotation.z = 0.0
		match _emote:
			"cheer":
				_arms[0].rotation.x = -2.9 + sin(_anim * 4.0) * 0.2
				_arms[1].rotation.x = -2.9 - sin(_anim * 4.0) * 0.2
				_head.rotation.x = -0.25
				_model.position.y = absf(sin(_anim * 3.0)) * 0.18
				return
			"listen":
				_arms[0].rotation.x = 0.0
				_arms[1].rotation.x = -1.6
				_head.rotation.x = -0.35
			"shrug":
				_arms[0].rotation.x = -0.5
				_arms[1].rotation.x = -0.5
				_arms[0].rotation.z = -0.6
				_arms[1].rotation.z = 0.6
				_head.rotation.x = 0.2 + sin(_anim * 3.0) * 0.1
			"talk":
				_arms[0].rotation.x = -0.6 + sin(_anim * 2.5) * 0.35
				_arms[1].rotation.x = -0.3
				_head.rotation.x = sin(_anim * 1.7) * 0.08
			"point":
				_arms[0].rotation.x = 0.0
				_arms[1].rotation.x = -1.5
				_head.rotation.x = -0.1
		_model.position.y = 0.0
		return
	_arms[0].rotation.z = 0.0
	_arms[1].rotation.z = 0.0
	if at_spot and task_id != "" and not _frozen:
		_arms[0].rotation.x = -1.15 + sin(_anim * 2.0) * 0.12
		_arms[1].rotation.x = -1.05 - sin(_anim * 2.0 + 1.0) * 0.12
		_head.rotation.x = 0.3
	else:
		_arms[0].rotation.x = -swing * 0.8
		_arms[1].rotation.x = swing * 0.8 - (0.4 if carry != "" else 0.0)
		_head.rotation.x = 0.0
	_model.position.y = absf(sin(_anim)) * 0.04 if moving else sin(_anim * 0.5) * 0.008
