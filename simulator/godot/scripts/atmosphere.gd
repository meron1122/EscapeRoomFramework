class_name Atmosphere
extends Node3D
## Everything outside the rooms.
## - A night city in its own 3D world, seen through a perspective camera and drawn as the background
##   (an orthographic iso camera would show distant towers at room scale). It pans a little with the iso view.
## - The rooms stand on a floating platform with railings, a landing pad and Kruk's hovering van, in the rain.

const CITY_SHADER := """
shader_type spatial;
uniform vec3 base_color : source_color = vec3(0.03, 0.035, 0.045);
uniform vec3 warm : source_color = vec3(1.0, 0.72, 0.42);
uniform vec3 cool : source_color = vec3(0.5, 0.85, 1.0);
uniform float seed = 0.0;
uniform float density = 0.5;
uniform vec2 cell_size = vec2(3.0, 4.0);
varying vec3 wpos;
varying vec3 wnrm;
float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7)) + seed) * 43758.5453); }
void vertex() {
	wpos = (MODEL_MATRIX * vec4(VERTEX, 1.0)).xyz;
	wnrm = normalize((MODEL_MATRIX * vec4(NORMAL, 0.0)).xyz);
}
void fragment() {
	ALBEDO = base_color;
	ROUGHNESS = 0.4;
	METALLIC = 0.3;
	if (abs(wnrm.y) < 0.5) {
		vec2 uv = abs(wnrm.x) > 0.5 ? wpos.zy : wpos.xy;
		vec2 cell = uv / cell_size;
		vec2 id = floor(cell);
		vec2 f = fract(cell);
		float win = step(0.15, f.x) * step(f.x, 0.85) * step(0.3, f.y) * step(f.y, 0.8);
		float floor_on = step(0.25, hash(vec2(id.y, 3.0)));
		float lit = step(1.0 - density, hash(id)) * floor_on;
		vec3 c = mix(warm, cool, step(0.6, hash(id + 3.1)));
		float tv = step(0.99, hash(id + 7.3)) * (0.5 + 0.5 * sin(TIME * 4.0 + hash(id) * 50.0));
		float band = step(0.97, fract(wpos.y / 37.0 + seed * 0.1));
		EMISSION = c * win * (lit * (0.4 + 0.7 * hash(id + 1.7)) + tv) * 1.1 + vec3(0.1, 0.94, 0.78) * band * 0.8;
	}
}
"""

const SIGN_SHADER := """
shader_type spatial;
render_mode unshaded;
uniform vec3 a : source_color = vec3(1.0, 0.24, 0.65);
uniform vec3 b : source_color = vec3(0.0, 0.94, 1.0);
uniform float seed = 0.0;
uniform float energy = 0.6;
float hash(float x) { return fract(sin(x * 91.7 + seed) * 43758.5453); }
void fragment() {
	// An animated ad: a slow gradient, a scrolling bar, a shape in the middle and scanlines; never any text.
	vec2 uv = UV;
	float t = TIME * 0.25 + seed;
	vec3 col = mix(a, b, 0.5 + 0.5 * sin(uv.y * 3.0 + t * 2.0));
	float shape = smoothstep(0.32, 0.3, length((uv - vec2(0.5, 0.45)) * vec2(1.0, 0.5)));
	float bar = step(0.92, fract(uv.y * 2.0 - t));
	float glitch = step(0.985, hash(floor(TIME * 8.0) + floor(uv.y * 30.0)));
	col = mix(col * 0.35, vec3(1.0), shape * 0.55) + bar * 0.6 + glitch * 0.8;
	col *= 0.85 + 0.15 * sin(uv.y * 400.0);
	float frame = step(uv.x, 0.03) + step(0.97, uv.x) + step(uv.y, 0.015) + step(0.985, uv.y);
	ALBEDO = (col + frame * b) * energy;
}
"""

const BEAM_SHADER := """
shader_type spatial;
render_mode unshaded, blend_add, cull_disabled, depth_draw_never;
uniform vec3 tint : source_color = vec3(0.6, 0.85, 1.0);
uniform float strength = 0.4;
void fragment() {
	float edge = pow(abs(dot(NORMAL, VIEW)), 3.0);
	ALBEDO = tint * strength * edge * (1.0 - UV.y);
}
"""

const ADS := ["Z", "Z-PHONE", "Z-DRIVE", "Z-MART", "ZALOGUJ SIĘ", "ABONAMENT NA ŻYCIE", "STREFA Z", "Z-ID"]
const PINK := Color("#ff3ca6")
const CYAN := Color("#00f0ff")
const TEAL := Color("#19f0c8")
const YELLOW := Color("#fcee0a")

var city_camera: Camera3D
var _city: SubViewport
var _cars: Array[Node3D] = []
var _beams: Array[Node3D] = []
var _blinkers: Array[MeshInstance3D] = []
var _van: Node3D
var _rng := RandomNumberGenerator.new()
var _font: Font
var _cam_base := Vector3(0, 120, 0)


func build(font: Font, main_env: Environment) -> void:
	_font = font
	_rng.seed = 2077
	_backdrop(main_env)
	_platform()
	# Rain only outside the building: the rooms have no ceiling in the cutaway.
	# Light rain far from the rooms only, so it reads as weather, not as noise over the play area.
	_rain(self, AABB(Vector3(-10, 0, -20), Vector3(55, 1, 19)), 250)
	_van_pad()


func follow(iso_target: Vector3) -> void:
	## Parallax: the city camera drifts a little as the iso view pans.
	if city_camera:
		city_camera.position = _cam_base + Vector3(iso_target.x - 15.0, 0, iso_target.z - 4.0) * 1.6


func _process(delta: float) -> void:
	for car in _cars:
		car.position += car.get_meta("dir") * car.get_meta("speed") * delta
		if car.position.length() > 1400.0:
			car.position = -car.get_meta("dir") * 1300.0 + Vector3(0, car.position.y, 0) + car.get_meta("offset")
	for i in _beams.size():
		_beams[i].rotation.z = sin(Time.get_ticks_msec() / 1000.0 * 0.3 + i * 1.7) * 0.35
	var blink := fmod(Time.get_ticks_msec() / 1000.0, 1.6) < 0.2
	for b in _blinkers:
		b.visible = blink
	if _van:
		_van.position.y = 0.55 + sin(Time.get_ticks_msec() / 1000.0 * 1.3) * 0.06


# ---------- the city, in its own world ----------

func _backdrop(main_env: Environment) -> void:
	_city = SubViewport.new()
	_city.own_world_3d = true
	_city.size = Vector2i(960, 540)
	_city.render_target_update_mode = SubViewport.UPDATE_ALWAYS
	add_child(_city)
	var env := Environment.new()
	env.background_mode = Environment.BG_COLOR
	env.background_color = Color("#06121a")
	env.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	env.ambient_light_color = Color("#1b3a4a")
	env.ambient_light_energy = 0.6
	env.tonemap_mode = Environment.TONE_MAPPER_ACES
	env.glow_enabled = true
	env.glow_intensity = 0.5
	env.glow_bloom = 0.03
	env.glow_hdr_threshold = 0.9
	env.glow_blend_mode = Environment.GLOW_BLEND_MODE_ADDITIVE
	env.fog_enabled = true
	env.fog_light_color = Color("#0f3340")
	env.fog_density = 0.005
	env.fog_sky_affect = 1.0
	var we := WorldEnvironment.new()
	we.environment = env
	_city.add_child(we)
	city_camera = Camera3D.new()
	city_camera.fov = 46.0
	city_camera.far = 3000.0
	city_camera.position = _cam_base
	city_camera.rotation_degrees = Vector3(-9, 45, 0)
	_city.add_child(city_camera)
	_skyline()
	# Show the city behind the iso scene: the main environment draws canvas layers up to -1 as its background.
	main_env.background_mode = Environment.BG_CANVAS
	main_env.background_canvas_max_layer = -1
	var layer := CanvasLayer.new()
	layer.layer = -1
	add_child(layer)
	var rect := TextureRect.new()
	rect.set_anchors_preset(Control.PRESET_FULL_RECT)
	rect.expand_mode = TextureRect.EXPAND_IGNORE_SIZE
	rect.stretch_mode = TextureRect.STRETCH_KEEP_ASPECT_COVERED
	rect.texture = _city.get_texture()
	rect.modulate = Color(0.36, 0.4, 0.44)  # the city stays behind the rooms, quieter than them
	rect.mouse_filter = Control.MOUSE_FILTER_IGNORE
	layer.add_child(rect)


func _skyline() -> void:
	var sh := Shader.new()
	sh.code = CITY_SHADER
	var sign_sh := Shader.new()
	sign_sh.code = SIGN_SHADER
	var forward := Vector3(-1, 0, -1).normalized()
	var side := Vector3(1, 0, -1).normalized()
	var ad := 0
	for i in 230:
		var dist := _rng.randf_range(430.0, 1700.0)
		var lateral := _rng.randf_range(-1.0, 1.0) * dist * 0.75
		var p := forward * dist + side * lateral
		var w := _rng.randf_range(28, 70)
		var d := _rng.randf_range(28, 70)
		var h := _rng.randf_range(70, 280) * (1.0 + dist / 1700.0)
		var b := MeshInstance3D.new()
		var box := BoxMesh.new()
		box.size = Vector3(w, h, d)
		b.mesh = box
		b.position = p + Vector3(0, h / 2.0 - 40.0, 0)
		var mat := ShaderMaterial.new()
		mat.shader = sh
		mat.set_shader_parameter("seed", _rng.randf() * 100.0)
		mat.set_shader_parameter("density", _rng.randf_range(0.12, 0.38))
		mat.set_shader_parameter("base_color", Color("#06080b").lerp(Color("#101820"), _rng.randf()))
		b.material_override = mat
		_city.add_child(b)
		if _rng.randf() < 0.35:  # a stepped top
			var top := MeshInstance3D.new()
			var tb := BoxMesh.new()
			tb.size = Vector3(w * 0.6, h * 0.25, d * 0.6)
			top.mesh = tb
			top.position = p + Vector3(0, h - 40.0 + h * 0.125, 0)
			top.material_override = mat
			_city.add_child(top)
			h *= 1.25
		var neon: Color = [CYAN, PINK, TEAL, YELLOW, CYAN][_rng.randi() % 5]
		if _rng.randf() < 0.18:
			_box(_city, Vector3(1.5, h * 0.85, 1.5), p + Vector3(w / 2.0, h * 0.42 - 40.0, d / 2.0), neon, 1.5)
		_box(_city, Vector3(w + 1, 1.2, d + 1), p + Vector3(0, h - 40.0, 0), neon, 1.0)
		if h > 200.0:
			var beacon := _box(_city, Vector3(2.5, 2.5, 2.5), p + Vector3(0, h - 36.0, 0), Color("#ff2a2a"), 5.0)
			_blinkers.append(beacon)
		# Tall vertical billboards on the faces the camera sees.
		if dist < 1200.0 and _rng.randf() < 0.6:
			var on_east := _rng.randf() < 0.5
			var bw := minf(w, d) * _rng.randf_range(0.5, 0.8)
			var bh := minf(h * 0.55, bw * _rng.randf_range(2.0, 3.6))
			var at := p + (Vector3(w / 2.0 + 0.6, 0, 0) if on_east else Vector3(0, 0, d / 2.0 + 0.6))
			at.y = h * _rng.randf_range(0.3, 0.6) - 40.0
			_sign(sign_sh, at, 90.0 if on_east else 0.0, Vector2(bw, bh), ADS[ad % ADS.size()])
			ad += 1
	for i in 4:  # searchlights sweeping the haze
		var origin := forward * _rng.randf_range(350, 800) + side * _rng.randf_range(-300, 300)
		var holder := Node3D.new()
		holder.position = origin + Vector3(0, 120, 0)
		_city.add_child(holder)
		var beam := MeshInstance3D.new()
		var c := CylinderMesh.new()
		c.top_radius = 26.0
		c.bottom_radius = 1.5
		c.height = 700.0
		c.cap_top = false
		c.cap_bottom = false
		beam.mesh = c
		beam.position.y = 350.0
		var bm := ShaderMaterial.new()
		var bsh := Shader.new()
		bsh.code = BEAM_SHADER
		bm.shader = bsh
		beam.material_override = bm
		holder.add_child(beam)
		holder.rotation.x = deg_to_rad(_rng.randf_range(-20, 20))
		_beams.append(holder)
	for i in 70:  # flying traffic in lanes
		var car := Node3D.new()
		var dir := side if _rng.randf() < 0.5 else -side
		if _rng.randf() < 0.3:
			dir = forward
		var offset := forward * _rng.randf_range(150, 900) + Vector3(0, _rng.randf_range(40, 240), 0)
		car.position = offset + dir * _rng.randf_range(-900, 900)
		car.set_meta("dir", dir)
		car.set_meta("speed", _rng.randf_range(25, 70))
		car.set_meta("offset", offset)
		car.look_at_from_position(car.position, car.position + dir, Vector3.UP)
		_box_into(car, Vector3(1.8, 0.8, 4.5), Vector3.ZERO, Color("#0b0e12"), 0.0)
		_box_into(car, Vector3(1.6, 0.25, 0.2), Vector3(0, 0, -2.3), Color("#e8fbff"), 6.0)
		_box_into(car, Vector3(1.6, 0.25, 0.2), Vector3(0, 0, 2.3), PINK if _rng.randf() < 0.5 else Color("#ff3030"), 4.0)
		_city.add_child(car)
		_cars.append(car)
	# The street level far below glows warm, like the haze over traffic.
	var glow := MeshInstance3D.new()
	var plane := PlaneMesh.new()
	plane.size = Vector2(4000, 4000)
	glow.mesh = plane
	glow.position = Vector3(0, -40, 0)
	var gm := StandardMaterial3D.new()
	gm.albedo_color = Color("#05070a")
	gm.emission_enabled = true
	gm.emission = Color("#3a1d10")
	gm.emission_energy_multiplier = 0.6
	glow.material_override = gm
	_city.add_child(glow)


func _sign(sh: Shader, at: Vector3, yaw: float, size: Vector2, text: String) -> void:
	var holder := Node3D.new()
	holder.position = at
	holder.rotation_degrees.y = yaw
	_city.add_child(holder)
	var q := MeshInstance3D.new()
	var qm := QuadMesh.new()
	qm.size = size
	q.mesh = qm
	var m := ShaderMaterial.new()
	m.shader = sh
	var pair: Array = [[PINK, CYAN], [CYAN, TEAL], [YELLOW, PINK], [PINK, YELLOW]][_rng.randi() % 4]
	m.set_shader_parameter("a", pair[0])
	m.set_shader_parameter("b", pair[1])
	m.set_shader_parameter("seed", _rng.randf() * 10.0)
	q.material_override = m
	holder.add_child(q)
	var label := Label3D.new()
	label.text = text
	label.font = _font
	label.font_size = 128
	label.pixel_size = size.x / 1100.0
	label.position = Vector3(0, -size.y * 0.36, 0.3)
	label.modulate = Color(1.6, 1.6, 1.6)
	label.shaded = false
	holder.add_child(label)


# ---------- the platform the rooms stand on ----------

func _platform() -> void:
	var metal := _mat(Color("#394249"), 0.0, 0.65, 0.15)
	_box(self, Vector3(41, 1.2, 12), Vector3(12.0, -0.72, 5.0), Color(), 0.0, metal)
	# One quiet edge line, so the platform has a border without becoming a light show.
	_box(self, Vector3(41.2, 0.08, 0.12), Vector3(12.0, -0.14, 11.06), Color("#6f8c94"), 0.5)
	_box(self, Vector3(0.12, 0.08, 12.2), Vector3(32.56, -0.14, 5.0), Color("#6f8c94"), 0.5)
	for x in [2.0, 12.0, 22.0, 31.0]:  # light the terrace so the platform reads as a floor, not a void
		var l := OmniLight3D.new()
		l.position = Vector3(x, 1.6, 10.4)
		l.light_color = Color("#a9bcc6")
		l.light_energy = 0.5
		l.omni_range = 5.0
		add_child(l)
	# Railings along the open edges, with a gap at the landing pad.
	for x in range(-8, 33, 2):
		_box(self, Vector3(0.06, 1.0, 0.06), Vector3(x, 0.5, 10.85), Color("#2a3036"), 0.0)
	_box(self, Vector3(40.6, 0.05, 0.05), Vector3(12.3, 1.0, 10.85), Color("#4a535a"), 0.0)
	for z in range(0, 11, 2):
		_box(self, Vector3(0.06, 1.0, 0.06), Vector3(32.3, 0.5, z), Color("#2a3036"), 0.0)


func _van_pad() -> void:
	_box(self, Vector3(5.6, 0.04, 4.0), Vector3(-5.2, 0.02, 4.8), Color("#1a1f24"), 0.0)
	for c in [Vector3(-7.9, 0.03, 2.9), Vector3(-2.5, 0.03, 2.9), Vector3(-7.9, 0.03, 6.7), Vector3(-2.5, 0.03, 6.7)]:
		_box(self, Vector3(0.5, 0.02, 0.5), c, YELLOW, 0.5)
	if not ResourceLoader.exists("res://assets/van.glb"):
		return
	_van = load("res://assets/van.glb").instantiate()
	_van.position = Vector3(-5.2, 0.55, 4.8)
	_van.rotation_degrees.y = 90
	add_child(_van)
	var glow := OmniLight3D.new()
	glow.position = Vector3(-5.2, 0.15, 4.8)
	glow.light_color = Color("#9fd8e0")
	glow.light_energy = 0.6
	glow.omni_range = 4.0
	add_child(glow)


# ---------- helpers ----------

func _rain(parent: Node, region: AABB, amount: int) -> void:
	var p := CPUParticles3D.new()
	p.amount = amount
	p.lifetime = 1.4
	p.preprocess = 2.0
	p.emission_shape = CPUParticles3D.EMISSION_SHAPE_BOX
	p.emission_box_extents = region.size / 2.0
	p.position = region.get_center() + Vector3(0, 30, 0)
	p.direction = Vector3(0.12, -1, 0.05)
	p.spread = 2.0
	p.gravity = Vector3(0, -30, 0)
	p.initial_velocity_min = 28.0
	p.initial_velocity_max = 36.0
	var q := QuadMesh.new()
	q.size = Vector2(0.05, 1.4)
	var m := StandardMaterial3D.new()
	m.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	m.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA
	m.blend_mode = BaseMaterial3D.BLEND_MODE_ADD
	m.albedo_color = Color(0.45, 0.7, 0.9, 0.16)
	m.billboard_mode = BaseMaterial3D.BILLBOARD_FIXED_Y
	q.material = m
	p.mesh = q
	p.local_coords = false
	parent.add_child(p)


func _box(parent: Node, size: Vector3, pos: Vector3, c: Color, energy: float, material: Material = null) -> MeshInstance3D:
	var m := MeshInstance3D.new()
	var b := BoxMesh.new()
	b.size = size
	m.mesh = b
	m.position = pos
	m.material_override = material if material else _mat(c, energy)
	parent.add_child(m)
	return m


func _box_into(parent: Node3D, size: Vector3, pos: Vector3, c: Color, energy: float) -> void:
	_box(parent, size, pos, c, energy)


func _mat(c: Color, energy: float, rough := 0.5, metal := 0.0) -> StandardMaterial3D:
	var m := StandardMaterial3D.new()
	m.albedo_color = c
	m.roughness = rough
	m.metallic = metal
	if energy > 0.0:
		m.emission_enabled = true
		m.emission = c
		m.emission_energy_multiplier = energy
	return m
