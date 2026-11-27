class_name Hud
extends CanvasLayer
## Overlay in the brandbook style: clock, rooms with puzzle states, variant, AI director, team, log, controls.

signal start_requested
signal intro_page(index: int)  # the intro turned a page; the camera goes to what the page is about
signal speed_requested(value: float)
signal pause_toggled
signal new_game_requested
signal music_toggled(on: bool)
signal meddled(what: String)  # a visitor interferes with the team: stall | rush | wrong | help

const YELLOW := Color("#fcee0a")
const CYAN := Color("#00f0ff")
const BLACK := Color("#000000")
const WHITE := Color("#ffffff")
const GREY := Color("#cbcbcb")
const SCALE := 0.78
const PINK := Color("#ff4fa3")  # everything Shadowman does: his lamps, his panel, his moves
const MUTED := Color("#8fb3b8")  # labels and finished things; cyan is kept for what is active now
const INTRO := [  # the game may be opened on its own, without the landing page: it tells its story first
	["/// THE STORY_", "THE CITY BELONGS TO Z.", "Z Corporation does not rule by force. It rules by subscription: the phone, the car, the food.\nTonight five Nomads walk into its tower to switch off The Core. They have sixty minutes."],
	["/// THE CREW_", "FIVE NOMADS.", ""],  # the page with the cast, see CAST
	["/// THE ROOM_", "IT IS AN ESCAPE ROOM.", "Three floors, eleven puzzles, real locks, lamps, screens and sensors.\nHere the room and its five players are simulated, so a whole night fits into a few minutes."],
	["/// THE GAME MASTER_", "NOBODY SITS BEHIND THE WALL.", "Shadowman runs the night. He reads every sensor, decides who needs help and when,\nspeaks through the room's voices, lights a lamp, throws a twist.\nHe is an AI agent, live on a Fly.io Sprite; his model and his voices reach him through a Sprites Connector."],
]
const CAST_PAGE := 1
const CAST := [  # name, scene, a line about them, colour (the one their ring has in the room)
	["SPARK", "PUNK", "Opens what is locked.\nTalks first, thinks later.", "#fcee0a"],
	["RUST", "METALHEAD", "Twenty years of fixing engines.\nReads any manual.", "#00f0ff"],
	["MOUSE", "GOTH", "Says little.\nNotices everything.", "#ffffff"],
	["THUNDER", "CYBER RAVER", "The loud one.\nCarries what is heavy.", "#b06bff"],
	["FOX", "SKATER", "The youngest.\nFastest hands in the Zone.", "#9dff5c"],
]
# One escape room, three floors: "room" is the whole game, as its owner says it.
const ROOMS := {"lobby": "FLOOR 1 · LOBBY", "habit": "FLOOR 2 · HABIT FLOOR", "core": "FLOOR 3 · THE CORE"}
const STATES := {"locked": "LOCKED_", "active": "ACTIVE_", "solved": "SOLVED_"}


class CutPanel extends PanelContainer:
	## A panel with a 1px frame and the bottom-right corner cut, as in the brandbook.
	var line := Color(0.6, 0.72, 0.76, 0.35)
	var fill := Color(0.03, 0.04, 0.05, 0.72)
	var cut := 14.0

	func _init() -> void:
		var sb := StyleBoxEmpty.new()
		for side in ["left", "right", "top", "bottom"]:
			sb.set("content_margin_" + side, 14.0)
		add_theme_stylebox_override("panel", sb)
		mouse_filter = Control.MOUSE_FILTER_IGNORE
		resized.connect(queue_redraw)

	func _draw() -> void:
		var s := size
		var pts := PackedVector2Array([Vector2.ZERO, Vector2(s.x, 0), Vector2(s.x, s.y - cut), Vector2(s.x - cut, s.y), Vector2(0, s.y)])
		draw_colored_polygon(pts, fill)
		pts.append(Vector2.ZERO)
		draw_polyline(pts, line, 1.0)


class Tags extends Control:
	## Labels pinned to things in the room (the puzzle in play, his lamps) and a pulse around the picture when he moves.
	var items: Array = []  # {at: Vector2, text: String, color: Color, progress: float (below 0: no bar)}
	var speech := {}  # the line being said: {at, below, who, text, color, his}
	var font: Font
	var words: Font
	var pulse := 0.0

	func _draw() -> void:
		for it in items:
			var w: float = font.get_string_size(it.text, HORIZONTAL_ALIGNMENT_LEFT, -1, 13).x + 16.0
			var r := Rect2(it.at.x - w / 2.0, it.at.y - 26.0, w, 22.0)
			draw_rect(r, Color(0.02, 0.03, 0.04, 0.85))
			draw_rect(r, Color(it.color, 0.7), false, 1.0)
			draw_string(font, r.position + Vector2(8, 16), it.text, HORIZONTAL_ALIGNMENT_LEFT, -1, 13, it.color)
			if it.progress >= 0.0:
				draw_rect(Rect2(r.position.x, r.end.y + 2, w, 3), Color(1, 1, 1, 0.15))
				draw_rect(Rect2(r.position.x, r.end.y + 2, w * clampf(it.progress, 0.0, 1.0), 3), it.color)
			draw_line(Vector2(it.at.x, r.end.y + 6), it.at + Vector2(0, 14), Color(it.color, 0.5), 1.0)
		if not speech.is_empty():
			_bubble()
		if pulse > 0.0:
			draw_rect(Rect2(Vector2.ZERO, size).grow(-3), Color(PINK, pulse), false, 6.0)

	func _bubble() -> void:
		## A speech bubble at the place the voice comes from: who speaks (and that the words are Shadowman's),
		## then the line, wrapped.
		var lines: Array[String] = [""]
		for word in String(speech.text).split(" "):
			if words.get_string_size(lines[-1] + " " + word, HORIZONTAL_ALIGNMENT_LEFT, -1, 17).x > 300.0 and lines[-1] != "":
				lines.append("")
			lines[-1] = (lines[-1] + " " + word).strip_edges()
		var head := String(speech.who) + ("   · SHADOWMAN'S WORDS" if speech.his else "")
		var w := font.get_string_size(head, HORIZONTAL_ALIGNMENT_LEFT, -1, 11).x
		for l in lines:
			w = maxf(w, words.get_string_size(l, HORIZONTAL_ALIGNMENT_LEFT, -1, 17).x)
		var box := Vector2(w + 24.0, 30.0 + lines.size() * 21.0)
		var at: Vector2 = speech.at
		var corner := Vector2(at.x - box.x / 2.0, at.y + 12.0 if speech.below else at.y - box.y - 12.0)
		corner = corner.clamp(Vector2(8, 8), size - box - Vector2(8, 8))
		var r := Rect2(corner, box)
		draw_rect(r, Color(0.02, 0.03, 0.04, 0.9))
		draw_rect(r, Color(speech.color, 0.9), false, 1.5)
		draw_line(Vector2(at.x, r.position.y if speech.below else r.end.y), at, Color(speech.color, 0.9), 1.5)
		draw_string(font, corner + Vector2(12, 17), String(speech.who), HORIZONTAL_ALIGNMENT_LEFT, -1, 11, speech.color)
		if speech.his:
			draw_string(font, corner + Vector2(12 + font.get_string_size(String(speech.who), HORIZONTAL_ALIGNMENT_LEFT, -1, 11).x, 17),
				"   · SHADOWMAN'S WORDS", HORIZONTAL_ALIGNMENT_LEFT, -1, 11, PINK)
		for i in lines.size():
			draw_string(words, corner + Vector2(12, 40 + i * 21), lines[i], HORIZONTAL_ALIGNMENT_LEFT, -1, 17, Color.WHITE)


class Emblem extends Control:
	## The mark of a voice that has no body in the room: ZETA's iris of rings, RAVEN's radio wave.
	var kind := "zeta"
	var tint := Color.WHITE

	func _draw() -> void:
		var c := size / 2.0
		if kind == "zeta":
			for r in [0.46, 0.31, 0.16]:
				draw_arc(c, size.y * r, 0, TAU, 40, tint, 3.0)
			draw_circle(c, size.y * 0.06, tint)
		else:
			var levels := [0.2, 0.45, 0.7, 0.35, 0.9, 0.5, 0.75, 0.3, 0.15]
			for i in levels.size():
				var h: float = size.y * levels[i]
				draw_rect(Rect2(size.x * (0.07 + i * 0.1), c.y - h / 2.0, size.x * 0.06, h), tint)


var fonts: Dictionary
var film := false
var _folded := ""
var _tags: Tags
var _now: Label
var _pips: HBoxContainer
var _banner: CutPanel
var _banner_bar: ColorRect
var _banner_tag: Label
var _banner_text: Label
var _banners: Array = []
var _banner_until := 0
var _status: Label
var _moves: VBoxContainer
var _variant_panel: CutPanel
var _intro: Control
var _head: Node3D  # Shadowman's avatar: his head in a little world of its own, in his panel
var _eye: StandardMaterial3D
var _grin: StandardMaterial3D
var _mood := "watching"  # watching | thinking | away
var _life := 0.0
var _moved_at := -10.0
var _paused: Control
var _page := 0
var _root: Control
var _film: Control
var _film_clock: Label
var _subtitle: RichTextLabel
var _subtitle_until := 0
var _countdown: Label
var _flash: ColorRect
var _clock: Label
var _stage: Label
var _rooms := {}  # room -> VBoxContainer
var _rows := {}  # task id -> status Label
var _variant: VBoxContainer
var _director: Label
var _team: VBoxContainer
var _log: RichTextLabel
var _start: Control
var _end: Control
var _end_text: Label
var _speed_buttons := {}
var _pause: Button
var _music: Button


func build(fonts_: Dictionary) -> void:
	fonts = fonts_
	var root := Control.new()
	root.mouse_filter = Control.MOUSE_FILTER_IGNORE
	add_child(root)
	# The whole overlay is drawn at 78%, so the rooms keep most of the picture.
	_root = root
	root.scale = Vector2(SCALE, SCALE)
	var fit := func(): root.size = Vector2(get_viewport().get_visible_rect().size) / SCALE
	get_viewport().size_changed.connect(fit)
	fit.call()

	var top := VBoxContainer.new()
	top.position = Vector2(28, 18)
	top.add_theme_constant_override("separation", -6)
	top.mouse_filter = Control.MOUSE_FILTER_IGNORE
	root.add_child(top)
	top.add_child(_text("/// THE_CORE.SIM_", "mono", 14, CYAN))
	_clock = _text("60:00", "display", 46, YELLOW)
	top.add_child(_clock)
	_stage = _text("", "text", 22, WHITE)
	top.add_child(_stage)
	_pips = HBoxContainer.new()
	_pips.add_theme_constant_override("separation", 4)
	_pips.custom_minimum_size.y = 26
	top.add_child(_pips)
	_now = _text("", "display", 20, CYAN)
	top.add_child(_now)

	var left := VBoxContainer.new()
	left.position = Vector2(28, 204)
	left.custom_minimum_size.x = 330
	left.add_theme_constant_override("separation", 12)
	left.mouse_filter = Control.MOUSE_FILTER_IGNORE
	root.add_child(left)
	for room in ROOMS:
		var panel := CutPanel.new()
		left.add_child(panel)
		var box := VBoxContainer.new()
		box.add_theme_constant_override("separation", 2)
		panel.add_child(box)
		box.add_child(_text(ROOMS[room], "display", 17, MUTED))
		_rooms[room] = box

	var right := VBoxContainer.new()
	right.anchor_left = 1.0
	right.anchor_right = 1.0
	right.offset_left = -372
	right.offset_right = -28
	right.offset_top = 18
	right.add_theme_constant_override("separation", 12)
	right.mouse_filter = Control.MOUSE_FILTER_IGNORE
	root.add_child(right)
	# Shadowman's panel leads: what he is doing now, why he woke up, his last note, his last moves.
	var dp := CutPanel.new()
	dp.line = Color(PINK, 0.75)
	right.add_child(dp)
	var dbox := VBoxContainer.new()
	dbox.add_theme_constant_override("separation", 4)
	dp.add_child(dbox)
	var who := HBoxContainer.new()
	who.add_theme_constant_override("separation", 12)
	dbox.add_child(who)
	var face := _build_avatar()
	if face:
		who.add_child(face)
	var titles := VBoxContainer.new()
	titles.add_theme_constant_override("separation", 2)
	who.add_child(titles)
	titles.add_child(_text("SHADOWMAN", "display", 26, PINK))
	_status = _text("THE AI GAME MASTER", "mono", 13, PINK)
	_status.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_status.custom_minimum_size.x = 186 if face else 310
	titles.add_child(_status)
	_director = _text("", "text", 19, WHITE)
	_director.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_director.custom_minimum_size.x = 310
	dbox.add_child(_director)
	_moves = VBoxContainer.new()
	_moves.add_theme_constant_override("separation", 0)
	dbox.add_child(_moves)
	var tp := CutPanel.new()
	right.add_child(tp)
	_team = VBoxContainer.new()
	_team.add_theme_constant_override("separation", 0)
	tp.add_child(_team)
	_variant_panel = CutPanel.new()  # this game's answers: for the curious, behind the V key
	_variant_panel.visible = false
	right.add_child(_variant_panel)
	_variant = VBoxContainer.new()
	_variant.add_theme_constant_override("separation", 0)
	_variant_panel.add_child(_variant)

	var lp := CutPanel.new()
	lp.anchor_top = 1.0
	lp.anchor_bottom = 1.0
	lp.offset_left = 28
	lp.offset_right = 660
	lp.offset_top = -164
	lp.offset_bottom = -24
	root.add_child(lp)
	_log = RichTextLabel.new()
	_log.bbcode_enabled = true
	_log.scroll_following = true
	_log.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_log.add_theme_font_override("normal_font", fonts.text)
	_log.add_theme_font_override("bold_font", fonts.mono)
	_log.add_theme_font_size_override("normal_font_size", 20)
	_log.add_theme_font_size_override("bold_font_size", 14)
	lp.add_child(_log)

	# The visitor's hand in the game: push the team around and watch what he does about it.
	var meddle := HBoxContainer.new()
	meddle.anchor_left = 0.5
	meddle.anchor_right = 0.5
	meddle.anchor_top = 0.0
	meddle.offset_left = -380
	meddle.offset_right = 380
	meddle.offset_top = 16
	meddle.alignment = BoxContainer.ALIGNMENT_CENTER
	meddle.add_theme_constant_override("separation", 10)
	root.add_child(meddle)
	var tag := _text("TEST HIM", "mono", 14, WHITE)  # the buttons act on the team; what is tested is him
	tag.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	meddle.add_child(tag)
	for item in [["stall", "STALL THE TEAM"], ["rush", "RUSH THE TEAM"], ["wrong", "WRONG ANSWER"], ["help", "ASK FOR HELP"]]:
		var b := _button(item[1], false)
		b.add_theme_font_size_override("font_size", 16)
		b.custom_minimum_size = Vector2(0, 36)
		b.pressed.connect(func(): meddled.emit(item[0]))
		meddle.add_child(b)

	# One line for whatever just happened: a puzzle solved, his move, the visitor's hand.
	_banner = CutPanel.new()
	_banner.anchor_left = 0.5
	_banner.anchor_right = 0.5
	_banner.offset_left = -380
	_banner.offset_right = 380
	_banner.offset_top = 70
	_banner.custom_minimum_size.x = 760
	_banner.modulate.a = 0.0
	_banner.fill = Color(0.02, 0.03, 0.04, 0.9)
	root.add_child(_banner)
	var brow := HBoxContainer.new()
	brow.add_theme_constant_override("separation", 14)
	_banner.add_child(brow)
	_banner_bar = ColorRect.new()
	_banner_bar.custom_minimum_size.x = 5
	brow.add_child(_banner_bar)
	var bcol := VBoxContainer.new()
	bcol.add_theme_constant_override("separation", -2)
	bcol.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	brow.add_child(bcol)
	_banner_tag = _text("", "mono", 14, WHITE)
	bcol.add_child(_banner_tag)
	_banner_text = _text("", "display", 26, WHITE)
	_banner_text.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_banner_text.custom_minimum_size.x = 700
	bcol.add_child(_banner_text)

	var controls := HBoxContainer.new()
	controls.anchor_left = 1.0
	controls.anchor_right = 1.0
	controls.anchor_top = 1.0
	controls.anchor_bottom = 1.0
	controls.offset_left = -760
	controls.offset_right = -28
	controls.offset_top = -72
	controls.offset_bottom = -24
	controls.alignment = BoxContainer.ALIGNMENT_END
	controls.add_theme_constant_override("separation", 10)
	root.add_child(controls)
	_pause = _button("PAUSE", false)
	_pause.pressed.connect(func(): pause_toggled.emit())
	controls.add_child(_pause)
	for v in [1.0, 5.0, 10.0, 20.0]:
		var b := _button("×%d" % int(v), false)
		b.pressed.connect(func(): speed_requested.emit(v))
		controls.add_child(b)
		_speed_buttons[v] = b
	_music = _button("MUSIC: ON", false)
	_music.toggle_mode = true
	_music.button_pressed = true
	_music.toggled.connect(func(on): _music.text = "MUSIC: ON" if on else "MUSIC: OFF"; music_toggled.emit(on))
	controls.add_child(_music)
	var ng := _button("NEW GAME", true)
	ng.pressed.connect(func(): new_game_requested.emit())
	controls.add_child(ng)

	var hint := _text("drag: move   ·   wheel: zoom   ·   1 2 3: floors   ·   F: follow the team   ·   V: this game's answers   ·   H: hide panels   ·   space: pause", "mono", 12, GREY)
	hint.anchor_top = 1.0
	hint.anchor_bottom = 1.0
	hint.anchor_left = 1.0
	hint.anchor_right = 1.0
	hint.offset_left = -1040
	hint.offset_top = -100
	root.add_child(hint)

	_start = _overlay("THE CORE", "A simulated escape room standing in for real hardware. Five players, sixty minutes, three floors.\nThe game master is real, and live.", "LET THEM IN", [
		[CYAN, "The five figures are simulated players, standing in for a venue's guests. Cyan marks the puzzle they are on."],
		[PINK, "Shadowman is the AI game master. He watches the room and answers with lamps, voices and twists. Pink is him."],
		[WHITE, "The buttons at the top are yours: stall the team, rush it, break things. Nobody tells him. He has to notice."]])
	_start.get_meta("button").pressed.connect(func(): _start.visible = false; start_requested.emit())
	_start.visible = false
	root.add_child(_start)
	_build_intro(root)
	_end = _overlay("THE END", "", "NEW GAME")
	_end_text = _end.get_meta("body")
	_end.get_meta("button").pressed.connect(func(): new_game_requested.emit())
	_end.visible = false
	root.add_child(_end)
	_build_film()


func _build_intro(root: Control) -> void:
	## A few pages of story and the crew over a slow camera, then the start screen. A lighter veil than the start screen's,
	## so the picture behind each page is part of it.
	_intro = ColorRect.new()
	(_intro as ColorRect).color = Color(0, 0, 0, 0.55)
	_intro.set_anchors_preset(Control.PRESET_FULL_RECT)
	root.add_child(_intro)
	var box := VBoxContainer.new()
	box.anchor_left = 0.08
	box.anchor_right = 0.08
	box.anchor_top = 0.5
	box.anchor_bottom = 0.5
	box.grow_vertical = Control.GROW_DIRECTION_BOTH
	box.add_theme_constant_override("separation", 16)
	_intro.add_child(box)
	var tag := _text("", "mono", 16, CYAN)
	var title := _text("", "display", 76, YELLOW)
	var body := _text("", "text", 28, WHITE)
	for l in [tag, title, body]:
		box.add_child(l)
	var cast := _build_cast()
	box.add_child(cast)
	var row := HBoxContainer.new()
	row.add_theme_constant_override("separation", 14)
	box.add_child(row)
	var next := _button("— NEXT", true, true)
	next.custom_minimum_size = Vector2(240, 56)
	next.add_theme_font_size_override("font_size", 24)
	row.add_child(next)
	next.tooltip_text = "Space"
	var skip := _button("SKIP THE STORY", false)
	skip.custom_minimum_size = Vector2(0, 56)
	row.add_child(skip)
	var show := func(page: int):
		if not _intro.visible:  # a game that starts by itself (film, stage, a new game) has no intro
			return
		_page = page
		intro_page.emit(page)
		_panels(page >= INTRO.size())  # the story is told over the bare picture; the panels come with the start screen
		if page >= INTRO.size():
			_intro.visible = false
			_start.visible = true
			return
		tag.text = "%s   %d / %d" % [INTRO[page][0], page + 1, INTRO.size() + 1]
		title.text = INTRO[page][1]
		body.text = INTRO[page][2]
		body.visible = body.text != ""
		cast.visible = page == CAST_PAGE
	next.pressed.connect(func(): show.call(_page + 1))
	skip.pressed.connect(func(): show.call(INTRO.size()))
	set_meta("next_page", func(): show.call(_page + 1))
	show.call_deferred(0)


func _build_avatar() -> Control:
	## His head, lit from one side only, so half of it is always in the dark; the eye on that side is his state:
	## a slow pulse while he watches, fast while he thinks, a flare when he moves, out when he is not in the room.
	if not ResourceLoader.exists("res://assets/shadowman.glb"):
		return null
	var vp := SubViewport.new()
	vp.size = Vector2i(224, 224)
	vp.own_world_3d = true
	vp.transparent_bg = true
	add_child(vp)
	var bust: Node3D = load("res://assets/shadowman.glb").instantiate()
	vp.add_child(bust)
	_head = bust.find_child("Head", true, false)
	for part in [["Eye", "_eye"], ["Grin", "_grin"]]:
		var mesh := bust.find_child(part[0], true, false) as MeshInstance3D
		if mesh:  # his own copy of the glowing material, so its strength can be animated
			var m := (mesh.get_active_material(0) as StandardMaterial3D).duplicate() as StandardMaterial3D
			mesh.material_override = m
			set(part[1], m)
	var cam := Camera3D.new()
	cam.fov = 26.0
	cam.position = Vector3(0, 0.42, 1.5)
	cam.environment = Environment.new()
	cam.environment.background_mode = Environment.BG_COLOR
	cam.environment.background_color = Color(0, 0, 0, 0)
	cam.environment.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	cam.environment.ambient_light_color = Color(0.16, 0.17, 0.2)
	vp.add_child(cam)
	cam.look_at(Vector3(0, 0.39, 0))  # his eye line; the horns just fit
	var key := DirectionalLight3D.new()
	key.light_energy = 1.5
	key.rotation_degrees = Vector3(-28, -52, 0)  # from the viewer's upper left
	vp.add_child(key)
	var rim := OmniLight3D.new()
	rim.light_color = PINK
	rim.light_energy = 3.0
	rim.omni_range = 2.5
	rim.position = Vector3(0.7, 0.75, -0.6)
	vp.add_child(rim)
	var picture := TextureRect.new()
	picture.texture = vp.get_texture()
	picture.custom_minimum_size = Vector2(112, 112)
	picture.expand_mode = TextureRect.EXPAND_IGNORE_SIZE
	picture.stretch_mode = TextureRect.STRETCH_KEEP_ASPECT_CENTERED
	return picture


func set_mood(mood: String) -> void:
	_mood = mood


func _avatar(delta: float) -> void:
	if _head == null:
		return
	_life += delta
	var thinking := _mood == "thinking"
	var since := _life - _moved_at
	var k := 1.0 - exp(-delta * 4.0)
	# Watching, his head sweeps the room slowly; thinking, it drops and holds still; a move turns it to the viewer.
	var yaw := 0.0 if thinking or since < 1.5 or _mood == "away" else sin(_life * 0.55) * 0.3
	_head.rotation.y = lerpf(_head.rotation.y, yaw, k)
	_head.rotation.x = lerpf(_head.rotation.x, 0.07 if thinking else sin(_life * 0.4) * 0.02, k)  # more, and his chin meets the hood
	var glow := 2.4 + 0.6 * sin(_life * 1.8)
	if thinking:
		glow = 3.0 + 2.5 * (0.5 + 0.5 * sin(_life * 10.0))
	elif fmod(_life, 5.3) < 0.12:
		glow = 0.2  # a blink
	if since < 1.5:
		glow += 9.0 * (1.0 - since / 1.5)
	if _mood == "away":
		glow = 0.1
	if _eye:
		_eye.emission_energy_multiplier = glow
	if _grin:
		_grin.emission_energy_multiplier = 0.08 if _mood == "away" else (2.2 if since < 1.5 else 0.7)


func _unhandled_input(e: InputEvent) -> void:
	## Space, Enter or the right arrow turn the intro's pages and let the team in, for those who do not reach for the mouse.
	var key := e as InputEventKey
	if key == null or not key.pressed or key.echo or key.keycode not in [KEY_SPACE, KEY_ENTER, KEY_RIGHT]:
		return
	if _intro.visible:
		get_meta("next_page").call()
	elif _start.visible:
		_start.visible = false
		start_requested.emit()
	else:
		return
	get_viewport().set_input_as_handled()


func _build_cast() -> Control:
	## The five players, each with a portrait, a name in their colour and a line; then the two voices of the room.
	var all := VBoxContainer.new()
	all.add_theme_constant_override("separation", 14)
	var cards := HBoxContainer.new()
	cards.add_theme_constant_override("separation", 18)
	all.add_child(cards)
	for c in CAST:
		var card := VBoxContainer.new()
		card.add_theme_constant_override("separation", 0)
		card.custom_minimum_size.x = 240
		var picture := TextureRect.new()
		picture.custom_minimum_size = Vector2(240, 300)
		picture.expand_mode = TextureRect.EXPAND_IGNORE_SIZE
		picture.stretch_mode = TextureRect.STRETCH_KEEP_ASPECT_CENTERED
		var file := "res://assets/cast/%s.png" % String(c[0]).to_lower()
		if ResourceLoader.exists(file):
			picture.texture = load(file)
		card.add_child(picture)
		var line := ColorRect.new()
		line.color = Color(c[3])
		line.custom_minimum_size.y = 3
		card.add_child(line)
		card.add_child(_text(c[0], "display", 34, Color(c[3])))
		card.add_child(_text(c[1], "mono", 15, MUTED))
		card.add_child(_text(c[2], "text", 21, GREY))
		cards.add_child(card)
	# The two who are heard and never seen, and whose words are his.
	var voices := HBoxContainer.new()
	voices.add_theme_constant_override("separation", 40)
	all.add_child(voices)
	for v in [["RAVEN", "raven", "A VOICE IN THEIR EARS", "The crew's hacker, in the van below the tower.", YELLOW],
			["ZETA", "zeta", "THE VOICE OF THE BUILDING", "Z Corporation's AI. Polite. Everywhere.", CYAN]]:
		var block := HBoxContainer.new()
		block.add_theme_constant_override("separation", 14)
		var face := "res://assets/cast/%s.png" % v[1]
		if ResourceLoader.exists(face):  # ZETA has a face; RAVEN is only ever a voice on the radio
			var portrait := TextureRect.new()
			portrait.texture = load(face)
			portrait.custom_minimum_size = Vector2(96, 96)
			portrait.expand_mode = TextureRect.EXPAND_IGNORE_SIZE
			portrait.stretch_mode = TextureRect.STRETCH_KEEP_ASPECT_CENTERED
			block.add_child(portrait)
		else:
			var mark := Emblem.new()
			mark.kind = v[1]
			mark.tint = v[4]
			mark.custom_minimum_size = Vector2(72, 72)
			block.add_child(mark)
		var words := VBoxContainer.new()
		words.add_theme_constant_override("separation", 0)
		words.add_child(_text(v[0], "display", 28, v[4]))
		words.add_child(_text(v[2], "mono", 13, MUTED))
		words.add_child(_text(v[3], "text", 19, GREY))
		block.add_child(words)
		voices.add_child(block)
	all.add_child(_text("Neither has a body in the room. Shadowman speaks through both.", "text", 21, PINK))
	return all


func _build_film() -> void:
	## Film mode: no panels, only letterbox bars, a small clock, subtitles; plus the finale countdown and flash.
	_film = Control.new()
	_film.set_anchors_preset(Control.PRESET_FULL_RECT)
	_film.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_film.visible = false
	add_child(_film)
	for top in [true, false]:
		var bar := ColorRect.new()
		bar.color = Color.BLACK
		bar.anchor_right = 1.0
		bar.anchor_top = 0.0 if top else 0.9
		bar.anchor_bottom = 0.1 if top else 1.0
		bar.mouse_filter = Control.MOUSE_FILTER_IGNORE
		_film.add_child(bar)
	_film_clock = _text("60:00", "display", 26, YELLOW)
	_film_clock.anchor_top = 0.025
	_film_clock.anchor_bottom = 0.025
	_film_clock.offset_left = 28
	_film.add_child(_film_clock)
	_tags = Tags.new()
	_tags.font = fonts.mono
	_tags.words = fonts.text
	_tags.set_anchors_preset(Control.PRESET_FULL_RECT)
	_tags.mouse_filter = Control.MOUSE_FILTER_IGNORE
	add_child(_tags)
	move_child(_tags, 0)  # under the panels
	# Spoken lines are subtitled in both modes: over the picture with the panels on, in the lower bar in film mode.
	_subtitle = RichTextLabel.new()
	_subtitle.bbcode_enabled = true
	_subtitle.fit_content = true
	_subtitle.scroll_active = false
	_subtitle.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_subtitle.anchor_left = 0.22
	_subtitle.anchor_right = 0.78
	_subtitle.grow_vertical = Control.GROW_DIRECTION_BEGIN
	_subtitle.visible = false
	var back := StyleBoxFlat.new()
	back.bg_color = Color(0.01, 0.015, 0.02, 0.82)
	back.set_content_margin_all(12)
	_subtitle.add_theme_stylebox_override("normal", back)
	_subtitle.add_theme_font_override("normal_font", fonts.text)
	_subtitle.add_theme_font_override("bold_font", fonts.mono)
	_subtitle.add_theme_font_size_override("normal_font_size", 26)
	_subtitle.add_theme_font_size_override("bold_font_size", 14)
	add_child(_subtitle)
	_place_subtitle()
	# The countdown and the flash sit above everything, in both modes.
	_countdown = _text("", "display", 64, YELLOW)
	_countdown.anchor_left = 0.0
	_countdown.anchor_right = 1.0
	_countdown.anchor_top = 0.14
	_countdown.anchor_bottom = 0.14
	_countdown.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	add_child(_countdown)
	# A paused game says so across the whole picture. Clicks pass through it, so the PAUSE button still works.
	_paused = ColorRect.new()
	(_paused as ColorRect).color = Color(0, 0, 0, 0.55)
	_paused.set_anchors_preset(Control.PRESET_FULL_RECT)
	_paused.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_paused.visible = false
	add_child(_paused)
	var word := _text("PAUSE", "display", 200, YELLOW)
	word.set_anchors_preset(Control.PRESET_FULL_RECT)
	word.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	word.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	_paused.add_child(word)
	var how := _text("SPACE OR THE BUTTON TO GO ON", "mono", 16, WHITE)
	how.set_anchors_preset(Control.PRESET_FULL_RECT)
	how.anchor_top = 0.68
	how.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	_paused.add_child(how)
	_flash = ColorRect.new()
	_flash.set_anchors_preset(Control.PRESET_FULL_RECT)
	_flash.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_flash.color = Color(1, 1, 1, 0)
	add_child(_flash)


func _place_subtitle() -> void:
	_subtitle.anchor_top = 0.985 if film else 0.8
	_subtitle.anchor_bottom = _subtitle.anchor_top
	_subtitle.offset_top = 0
	_subtitle.offset_bottom = 0


func set_film(on: bool) -> void:
	film = on
	_film.visible = on
	_root.visible = not on or _end.visible
	_place_subtitle()


func set_tags(items: Array, speech := {}) -> void:
	_tags.items = items
	_tags.speech = speech
	_tags.queue_redraw()


func banner(tag: String, text: String, color: Color) -> void:
	_banners.append([tag, text, color])
	if _banners.size() > 3:  # a burst: the oldest news gives way
		_banners.pop_front()


func set_now(text: String) -> void:
	_now.text = text


func set_pips(states: Array) -> void:
	## One mark per puzzle of the whole game, in order; a gap between rooms.
	for c in _pips.get_children():
		c.queue_free()
	var room := ""
	for st in states:
		if room != "" and st[0] != room:
			var gap := Control.new()
			gap.custom_minimum_size.x = 6
			_pips.add_child(gap)
		room = st[0]
		var pip := ColorRect.new()
		pip.custom_minimum_size = Vector2(22, 7)
		pip.size_flags_vertical = Control.SIZE_SHRINK_CENTER
		pip.color = {"locked": Color(0.3, 0.34, 0.36), "active": CYAN, "solved": YELLOW}[st[1]]
		_pips.add_child(pip)


func set_status(text: String, live := true) -> void:
	_status.text = text
	_status.add_theme_color_override("font_color", PINK if live else MUTED)


func add_move(clock: String, text: String) -> void:
	## His last moves, newest first; and the picture's edge pulses, so a move is felt even when nobody reads.
	var row := _text("%s  %s" % [clock, text], "text", 17, WHITE)
	row.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	row.custom_minimum_size.x = 310
	_moves.add_child(row)
	_moves.move_child(row, 0)
	_moved_at = _life
	for i in _moves.get_child_count():
		(_moves.get_child(i) as Label).add_theme_color_override("font_color", PINK if i == 0 else GREY)
		if i >= 4:
			_moves.get_child(i).queue_free()
	_tags.pulse = 0.9


func toggle_variant() -> void:
	_variant_panel.visible = not _variant_panel.visible


func set_countdown(text: String) -> void:
	_countdown.text = text


func flash() -> void:
	_flash.color = Color(1, 1, 1, 1)
	create_tween().tween_property(_flash, "color", Color(0.04, 0.02, 0.02, 0), 3.0).set_ease(Tween.EASE_OUT)


func _panels(on: bool) -> void:
	for c in _root.get_children():
		if c not in [_intro, _start, _end]:
			c.visible = on


func hide_start() -> void:
	_start.visible = false
	_intro.visible = false
	_panels(true)


func set_clock(clock: String, room: String, _plan_minutes: int, paused: bool) -> void:
	_clock.text = clock
	_film_clock.text = clock
	_stage.text = "%s%s" % [ROOMS[room].replace(" · ", " OF 3 · "), "   ·   PAUSED" if paused else ""]
	if _paused.visible != paused:
		_paused.visible = paused
		_pause.text = "RESUME" if paused else "PAUSE"
		# the word spans the picture at any window size
		(_paused.get_child(0) as Label).add_theme_font_size_override("font_size", int(get_viewport().get_visible_rect().size.x * 0.22))
	if _folded == room:
		return
	_folded = room
	# Only the room in play lists its puzzles; the others fold to a title, so the eye has one list to read.
	for r in _rooms:
		var box: VBoxContainer = _rooms[r]
		var solved := 0
		for i in range(1, box.get_child_count()):
			box.get_child(i).visible = r == room
			if (box.get_child(i).get_child(1) as Label).text == STATES.solved:
				solved += 1
		(box.get_child(0) as Label).text = ROOMS[r] if r == room else "%s   %d/%d" % [ROOMS[r], solved, box.get_child_count() - 1]
		(box.get_child(0) as Label).add_theme_color_override("font_color", WHITE if r == room else MUTED)


func set_speed(value: float) -> void:
	for v in _speed_buttons:
		_style_button(_speed_buttons[v], v == value)


func add_task(id: String, name: String, room: String) -> void:
	var row := HBoxContainer.new()
	var n := _text(name, "text", 19, WHITE)
	n.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_child(n)
	var st := _text("", "mono", 13, GREY)
	row.add_child(st)
	_rooms[room].add_child(row)
	_rows[id] = st
	set_task(id, "locked")


func set_task(id: String, state: String) -> void:
	_folded = ""
	var st: Label = _rows[id]
	st.text = STATES[state]
	st.add_theme_color_override("font_color", {"locked": Color(0.45, 0.48, 0.5), "active": CYAN, "solved": MUTED}[state])
	(st.get_parent().get_child(0) as Label).add_theme_color_override("font_color", GREY if state == "locked" else WHITE)


func set_variant(lines: Array[String]) -> void:
	for c in _variant.get_children():
		c.queue_free()
	_variant.add_child(_text("/// GAME.VARIANT_", "mono", 13, MUTED))
	for l in lines:
		var lab := _text(l, "text", 17, WHITE)
		lab.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
		lab.custom_minimum_size.x = 310
		_variant.add_child(lab)


func set_director(text: String) -> void:
	_director.text = text


func set_team(npcs: Array) -> void:
	if _team.get_child_count() != npcs.size() + 1:
		for c in _team.get_children():
			c.queue_free()
		_team.add_child(_text("/// NOMADS_", "mono", 13, CYAN))
		for n in npcs:
			var row := HBoxContainer.new()
			var dot := ColorRect.new()
			dot.color = n.color
			dot.custom_minimum_size = Vector2(10, 10)
			dot.size_flags_vertical = Control.SIZE_SHRINK_CENTER
			row.add_child(dot)
			row.add_child(_text(n.display_name.to_upper(), "display", 15, WHITE))
			var act := _text("", "text", 17, GREY)
			act.size_flags_horizontal = Control.SIZE_EXPAND_FILL
			act.horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT
			row.add_child(act)
			_team.add_child(row)
	for i in npcs.size():
		(_team.get_child(i + 1).get_child(2) as Label).text = npcs[i].activity


func add_log(speaker: String, text: String) -> void:
	var c: String = {"RAVEN": "#fcee0a", "ZETA": "#00f0ff", "NOMADS": "#9fd8de", "SYSTEM": "#cbcbcb", "YOU": "#ffffff"}.get(speaker, "#cbcbcb")  # pink is his alone; the visitor is white
	_log.append_text("[b][color=%s]%s>[/color][/b] [color=%s]%s[/color]\n" % [c, speaker, "#ffffff" if speaker != "SYSTEM" else "#cbcbcb", text])
	if speaker in ["RAVEN", "ZETA"]:  # subtitles carry the two voices; the team's chatter stays above their heads
		subtitle(speaker, text, 5.0)


func subtitle(speaker: String, text: String, seconds: float, his := false) -> void:
	## `his`: a line Shadowman chose to say through one of the room's characters, not a scripted one.
	var c: String = {"RAVEN": "#fcee0a", "ZETA": "#00f0ff"}.get(speaker, "#ffffff")
	var who := "[color=#ff4fa3]SHADOWMAN, AS [/color][color=%s]%s[/color]" % [c, speaker] if his else "[color=%s]%s[/color]" % [c, speaker]
	_subtitle.text = "[center][b]%s[/b]\n%s[/center]" % [who, text]
	_subtitle.visible = true
	_subtitle_until = Time.get_ticks_msec() + int(seconds * 1000.0)


func _process(delta: float) -> void:
	_avatar(delta)
	var now := Time.get_ticks_msec()
	if _subtitle and _subtitle.visible and now > _subtitle_until:
		_subtitle.visible = false
	if _tags and _tags.pulse > 0.0:
		_tags.pulse = maxf(0.0, _tags.pulse - delta * 0.9)
		_tags.queue_redraw()
	if _banner == null:
		return
	if now > _banner_until and not _banners.is_empty():
		var b: Array = _banners.pop_front()
		_banner_tag.text = b[0]
		_banner_tag.add_theme_color_override("font_color", b[2])
		_banner_text.text = b[1]
		_banner_bar.color = b[2]
		_banner.line = Color(b[2], 0.75)
		_banner.queue_redraw()
		_banner.size.y = 0  # back to one line after a long one
		_banner.modulate.a = 0.0
		create_tween().tween_property(_banner, "modulate:a", 1.0, 0.18)
		_banner_until = now + (3400 if _banners.is_empty() else 1900)
	elif now > _banner_until and _banner.modulate.a >= 1.0:
		create_tween().tween_property(_banner, "modulate:a", 0.0, 0.5)


func clear_log() -> void:
	_log.clear()


func show_end(summary: String) -> void:
	_end_text.text = summary
	_end.visible = true
	_root.visible = true


func hide_end() -> void:
	_end.visible = false


func _text(t: String, font: String, size: int, c: Color) -> Label:
	var l := Label.new()
	l.text = t
	l.mouse_filter = Control.MOUSE_FILTER_IGNORE
	l.add_theme_font_override("font", fonts[font])
	l.add_theme_font_size_override("font_size", size)
	l.add_theme_color_override("font_color", c)
	return l


func _button(t: String, primary: bool, solid := false) -> Button:
	var b := Button.new()
	b.text = t
	# Buttons are for the mouse and take no keyboard focus: a focused button also answers Space and Enter, so one
	# key press would both turn an intro page (or pause the game) and press whatever was clicked last.
	b.focus_mode = Control.FOCUS_NONE
	b.add_theme_font_override("font", fonts.text)
	b.add_theme_font_size_override("font_size", 20)
	b.custom_minimum_size = Vector2(0, 44)
	b.set_meta("primary", primary)
	b.set_meta("solid", solid)  # only a call to action is a solid yellow block; lit bar buttons are outlined
	_style_button(b, primary)
	return b


func _style_button(b: Button, lit: bool) -> void:
	for state in ["normal", "hover", "pressed", "focus"]:
		var sb := StyleBoxFlat.new()
		sb.skew = Vector2(0.18, 0)
		sb.content_margin_left = 18
		sb.content_margin_right = 18
		var on: bool = lit or state == "pressed"  # a toggled button shows as pressed
		if on and b.get_meta("solid", false):
			sb.bg_color = YELLOW
		elif on:
			sb.bg_color = Color(0.99, 0.93, 0.04, 0.12)
			sb.set_border_width_all(1)
			sb.border_color = YELLOW
		else:
			sb.bg_color = Color(0, 0, 0, 0.8)
			sb.set_border_width_all(1)
			sb.border_color = CYAN if state != "focus" else YELLOW
		if state == "hover" and not lit:
			sb.bg_color = Color(0, 0.94, 1, 0.15)
		if state == "focus":
			sb.set_border_width_all(3)
			sb.border_color = CYAN
			sb.draw_center = lit
		b.add_theme_stylebox_override(state, sb)
	var fc := (BLACK if b.get_meta("solid", false) else YELLOW) if lit else CYAN
	for k in ["font_color", "font_hover_color", "font_focus_color"]:
		b.add_theme_color_override(k, fc)
	b.add_theme_color_override("font_pressed_color", BLACK if b.get_meta("solid", false) else YELLOW)


func _overlay(title: String, body: String, action: String, legend: Array = []) -> Control:
	var o := ColorRect.new()
	o.color = Color(0, 0, 0, 0.82)
	o.set_anchors_preset(Control.PRESET_FULL_RECT)
	var center := CenterContainer.new()
	center.set_anchors_preset(Control.PRESET_FULL_RECT)
	o.add_child(center)
	var box := VBoxContainer.new()
	box.add_theme_constant_override("separation", 18)
	center.add_child(box)
	box.add_child(_text("/// NOMADS.OPERATION_CORE_", "mono", 16, CYAN))
	box.add_child(_text(title, "display", 120, YELLOW))
	var b := _text(body, "text", 26, WHITE)
	box.add_child(b)
	for item in legend:  # what the colours on the screen mean, before the first click
		var row := HBoxContainer.new()
		row.add_theme_constant_override("separation", 14)
		var dot := ColorRect.new()
		dot.color = item[0]
		dot.custom_minimum_size = Vector2(14, 14)
		dot.size_flags_vertical = Control.SIZE_SHRINK_CENTER
		row.add_child(dot)
		row.add_child(_text(item[1], "text", 22, GREY))
		box.add_child(row)
	var btn := _button("— " + action, true, true)
	btn.size_flags_horizontal = Control.SIZE_SHRINK_BEGIN
	btn.custom_minimum_size = Vector2(380, 56)
	btn.add_theme_font_size_override("font_size", 24)
	box.add_child(btn)
	o.set_meta("button", btn)
	o.set_meta("body", b)
	return o
