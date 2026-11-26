class_name Hud
extends CanvasLayer
## Overlay in the brandbook style: clock, rooms with puzzle states, variant, AI director, team, log, controls.

signal start_requested
signal speed_requested(value: float)
signal pause_toggled
signal new_game_requested
signal music_toggled(on: bool)

const YELLOW := Color("#fcee0a")
const CYAN := Color("#00f0ff")
const BLACK := Color("#000000")
const WHITE := Color("#ffffff")
const GREY := Color("#cbcbcb")
const MUTED := Color("#8fb3b8")  # labels and finished things; cyan is kept for what is active now
const ROOMS := {"hol": "POKÓJ 1 · HOL", "pietro": "POKÓJ 2 · PIĘTRO NAWYKÓW", "jadro": "POKÓJ 3 · JĄDRO"}
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


var fonts: Dictionary
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
	root.set_anchors_preset(Control.PRESET_FULL_RECT)
	root.mouse_filter = Control.MOUSE_FILTER_IGNORE
	add_child(root)

	var top := VBoxContainer.new()
	top.position = Vector2(28, 18)
	top.add_theme_constant_override("separation", -6)
	top.mouse_filter = Control.MOUSE_FILTER_IGNORE
	root.add_child(top)
	top.add_child(_text("/// JADRO.SIM_", "mono", 14, CYAN))
	_clock = _text("60:00", "display", 46, YELLOW)
	top.add_child(_clock)
	_stage = _text("", "text", 22, WHITE)
	top.add_child(_stage)

	var left := VBoxContainer.new()
	left.position = Vector2(28, 160)
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
	var vp := CutPanel.new()
	right.add_child(vp)
	_variant = VBoxContainer.new()
	_variant.add_theme_constant_override("separation", 0)
	vp.add_child(_variant)
	var dp := CutPanel.new()
	right.add_child(dp)
	var dbox := VBoxContainer.new()
	dp.add_child(dbox)
	dbox.add_child(_text("/// AI.DIRECTOR_", "mono", 13, MUTED))
	_director = _text("", "text", 19, WHITE)
	_director.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_director.custom_minimum_size.x = 310
	dbox.add_child(_director)
	var tp := CutPanel.new()
	right.add_child(tp)
	_team = VBoxContainer.new()
	_team.add_theme_constant_override("separation", 0)
	tp.add_child(_team)

	var lp := CutPanel.new()
	lp.anchor_top = 1.0
	lp.anchor_bottom = 1.0
	lp.offset_left = 28
	lp.offset_right = 760
	lp.offset_top = -214
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

	var controls := HBoxContainer.new()
	controls.anchor_left = 1.0
	controls.anchor_right = 1.0
	controls.anchor_top = 1.0
	controls.anchor_bottom = 1.0
	controls.offset_left = -690
	controls.offset_right = -28
	controls.offset_top = -72
	controls.offset_bottom = -24
	controls.alignment = BoxContainer.ALIGNMENT_END
	controls.add_theme_constant_override("separation", 10)
	root.add_child(controls)
	_pause = _button("PAUZA", false)
	_pause.pressed.connect(func(): pause_toggled.emit())
	controls.add_child(_pause)
	for v in [1.0, 5.0, 20.0]:
		var b := _button("×%d" % int(v), false)
		b.pressed.connect(func(): speed_requested.emit(v))
		controls.add_child(b)
		_speed_buttons[v] = b
	_music = _button("MUZYKA: ON", false)
	_music.toggle_mode = true
	_music.button_pressed = true
	_music.toggled.connect(func(on): _music.text = "MUZYKA: ON" if on else "MUZYKA: OFF"; music_toggled.emit(on))
	controls.add_child(_music)
	var ng := _button("NOWA GRA", true)
	ng.pressed.connect(func(): new_game_requested.emit())
	controls.add_child(ng)

	var hint := _text("LPM / PPM: przesuń   ·   kółko: zoom   ·   1 2 3: pokoje   ·   F: śledź drużynę   ·   H: ukryj panele", "mono", 12, GREY)
	hint.anchor_top = 1.0
	hint.anchor_bottom = 1.0
	hint.anchor_left = 1.0
	hint.anchor_right = 1.0
	hint.offset_left = -690
	hint.offset_top = -100
	root.add_child(hint)

	_start = _overlay("JĄDRO", "Pięcioro Nomadów, trzy pokoje, sześćdziesiąt minut. Każda gra ma inny wariant,\na reżyser AI dobiera podpowiedzi i zwroty akcji do tempa drużyny.", "ROZPOCZNIJ SYMULACJĘ")
	_start.get_meta("button").pressed.connect(func(): _start.visible = false; start_requested.emit())
	root.add_child(_start)
	_end = _overlay("KONIEC", "", "NOWA GRA")
	_end_text = _end.get_meta("body")
	_end.get_meta("button").pressed.connect(func(): new_game_requested.emit())
	_end.visible = false
	root.add_child(_end)


func hide_start() -> void:
	_start.visible = false


func set_clock(clock: String, room: String, plan_minutes: int, paused: bool) -> void:
	_clock.text = clock
	_stage.text = "%s   ·   PLAN %d:00%s" % [ROOMS[room], plan_minutes, "   ·   PAUZA" if paused else ""]


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
	var st: Label = _rows[id]
	st.text = STATES[state]
	st.add_theme_color_override("font_color", {"locked": Color(0.45, 0.48, 0.5), "active": CYAN, "solved": MUTED}[state])
	(st.get_parent().get_child(0) as Label).add_theme_color_override("font_color", GREY if state == "locked" else WHITE)


func set_variant(lines: Array[String]) -> void:
	for c in _variant.get_children():
		c.queue_free()
	_variant.add_child(_text("/// WARIANT.GRY_", "mono", 13, MUTED))
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
		_team.add_child(_text("/// NOMADZI_", "mono", 13, CYAN))
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
	var c: String = {"KRUK": "#fcee0a", "ZETA": "#00f0ff", "NOMADZI": "#ffffff", "SYSTEM": "#cbcbcb"}.get(speaker, "#cbcbcb")
	_log.append_text("[b][color=%s]%s>[/color][/b] [color=%s]%s[/color]\n" % [c, speaker, "#ffffff" if speaker != "SYSTEM" else "#cbcbcb", text])


func clear_log() -> void:
	_log.clear()


func show_end(summary: String) -> void:
	_end_text.text = summary
	_end.visible = true


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
	b.focus_mode = Control.FOCUS_ALL
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


func _overlay(title: String, body: String, action: String) -> Control:
	var o := ColorRect.new()
	o.color = Color(0, 0, 0, 0.82)
	o.set_anchors_preset(Control.PRESET_FULL_RECT)
	var center := CenterContainer.new()
	center.set_anchors_preset(Control.PRESET_FULL_RECT)
	o.add_child(center)
	var box := VBoxContainer.new()
	box.add_theme_constant_override("separation", 18)
	center.add_child(box)
	box.add_child(_text("/// NOMADZI.OPERACJA_JADRO_", "mono", 16, CYAN))
	box.add_child(_text(title, "display", 120, YELLOW))
	var b := _text(body, "text", 26, WHITE)
	box.add_child(b)
	var btn := _button("— " + action, true, true)
	btn.size_flags_horizontal = Control.SIZE_SHRINK_BEGIN
	btn.custom_minimum_size = Vector2(380, 56)
	btn.add_theme_font_size_override("font_size", 24)
	box.add_child(btn)
	o.set_meta("button", btn)
	o.set_meta("body", b)
	return o
