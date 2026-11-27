class_name Screens
extends RefCounted
## In-room screens: small 2D pages in the brandbook style, rendered into textures on the 3D screen meshes.

const YELLOW := Color("#fcee0a")
const CYAN := Color("#00f0ff")
const BLACK := Color("#000000")
const WHITE := Color("#ffffff")
const GREY := Color("#cbcbcb")
const GESTURES := {
	"Z": [1, 2, 3, 5, 7, 8, 9], "S": [3, 2, 1, 4, 5, 6, 9, 8, 7], "N": [7, 4, 1, 5, 9, 6, 3],
	"U": [1, 4, 7, 8, 9, 6, 3], "L": [1, 4, 7, 8, 9], "7": [1, 2, 3, 5, 7],
}

class Face extends Control:
	## ZETA as the building shows her: an iris of rings that opens when she speaks, and the level of her voice.
	var talk := 0.0  # 0 silent, 1 speaking
	var t := 0.0

	func _draw() -> void:
		var c := Vector2(size.y * 0.5, size.y * 0.5)
		var r := size.y * 0.36
		var tint := Screens.YELLOW if talk > 0.0 else Screens.CYAN
		draw_arc(c, r, 0, TAU, 48, Screens.CYAN, 4.0)
		draw_arc(c, r * 0.74, t * 0.6, t * 0.6 + 4.6, 32, Color(Screens.CYAN, 0.7), 3.0)
		draw_arc(c, r * (0.36 + 0.14 * talk * (0.5 + 0.5 * sin(t * 9.0))), 0, TAU, 32, tint, 8.0)
		draw_circle(c, r * 0.13, Screens.WHITE)
		for i in 12:  # her voice
			var h := 8.0 + 70.0 * talk * absf(sin(t * (5.0 + i * 0.7) + i))
			draw_rect(Rect2(size.y + 10 + i * 20, size.y * 0.72 - h / 2.0, 12, h), tint)


var fonts: Dictionary
var _frames := 0
var _talk_until := 0
var parts := {}  # kind -> {name: Control}


func _init(fonts_: Dictionary) -> void:
	fonts = fonts_


func make(kind: String, px: Vector2i, sim: Sim) -> SubViewport:
	var vp := SubViewport.new()
	vp.size = px
	vp.render_target_update_mode = SubViewport.UPDATE_ALWAYS
	var root := Control.new()
	root.set_anchors_preset(Control.PRESET_FULL_RECT)
	vp.add_child(root)
	_fill(kind, root, px, sim)
	parts[kind].vp = vp
	return vp


func refresh(kind: String, sim: Sim) -> void:
	## Rebuilds a screen's page after the engine changed its clue (a rerolled puzzle).
	if not parts.has(kind):
		return
	var root: Control = parts[kind].root
	var px: Vector2i = parts[kind].px
	var vp: SubViewport = parts[kind].vp
	for c in root.get_children():
		c.queue_free()
	_fill(kind, root, px, sim)
	parts[kind].vp = vp
	touch(kind)


func touch(kind: String) -> void:
	## A screen is drawn again only when its page changed; the rest of the time its picture just stays.
	if parts.has(kind) and parts[kind].has("vp"):
		(parts[kind].vp as SubViewport).render_target_update_mode = SubViewport.UPDATE_ONCE


func _fill(kind: String, root: Control, px: Vector2i, sim: Sim) -> void:
	var p := {"root": root, "px": px}
	parts[kind] = p
	var v := sim.variant
	var s := float(px.y) / 300.0  # type scale relative to a 300 px tall screen
	match kind:
		"ad":
			_bg(root, YELLOW)
			_label(root, "Z.", "display", 110 * s, BLACK, Vector2(24, 4) * s)
			_label(root, "ONE GESTURE\nAND YOU'RE HOME", "display", 34 * s, BLACK, Vector2(26, 150) * s)
			_label(root, "/// Z-PHONE.UNLOCK_", "mono", 13 * s, BLACK, Vector2(28, 250) * s)
			_gesture(root, v.gesture, Vector2(px.x - 230 * s, 40 * s), 90 * s, BLACK, 9 * s)
		"zphone":
			_bg(root, BLACK)
			_gesture(root, "", Vector2(px.x * 0.5 - 45 * s, 70 * s), 45 * s, GREY, 0)
			p.state = _label(root, "LOCKED", "mono", 16 * s, GREY, Vector2(px.x * 0.5 - 60 * s, 30 * s))
		"roster":
			_bg(root, BLACK)
			_label(root, "/// SECURITY.SHIFTS_", "mono", 18 * s, CYAN, Vector2(24, 18) * s)
			var y := 70.0
			for who in ["Marek", "Ola", "Tomek"]:
				var sh: Array = v.shifts[who]
				_label(root, "%s   %02d:00–%02d:00" % [who.to_upper(), sh[0], sh[1]], "text", 34 * s, WHITE, Vector2(24, y) * s)
				if who == v.duty:
					_chip(root, "TODAY", Vector2(330, y + 6) * s, s)
				y += 62.0
		"meter":
			_bg(root, YELLOW)
			_label(root, "Z ZONE DEPENDENCY INDEX", "display", 30 * s, BLACK, Vector2(28, 18) * s)
			p.value = _label(root, "97.40%", "display", 120 * s, BLACK, Vector2(28, 70) * s)
			p.v = 97.4
		"report":
			_bg(root, BLACK)
			p.title = _label(root, "SCREEN TIME: TEENAGERS", "mono", 15 * s, CYAN, Vector2(18, 14) * s)
			var y := 52.0
			for app in v.minutes.keys():
				var m: int = v.minutes[app]
				_label(root, app, "text", 22 * s, WHITE, Vector2(18, y - 4) * s)
				var bar := ColorRect.new()
				bar.color = CYAN
				bar.position = Vector2(120, y + 4) * s
				bar.size = Vector2(m * 1.2, 16) * s
				root.add_child(bar)
				_label(root, "%d min" % m, "mono", 13 * s, GREY, Vector2(130 + m * 1.2, y) * s)
				y += 40.0
		"zdrive":
			_bg(root, BLACK)
			p.top = _label(root, "BRAKE SUBSCRIPTION EXPIRED", "display", 26 * s, WHITE, Vector2(20, 24) * s)
			p.mid = _label(root, "RENEW FOR 299 Z/MONTH", "text", 34 * s, YELLOW, Vector2(20, 90) * s)
			p.code = _label(root, "SERVICE CODE: %s" % v.code, "mono", 20 * s, CYAN, Vector2(20, 190) * s)
		"kiosk":
			_bg(root, YELLOW)
			_label(root, "Z-MART\nLOYALTY", "display", 40 * s, BLACK, Vector2(20, 18) * s)
			_label(root, "/// TODAY'S CUSTOMER_", "mono", 14 * s, BLACK, Vector2(22, 140) * s)
			p.body = _label(root, v.customer.replace(", ", "\n"), "text", 26 * s, BLACK, Vector2(22, 170) * s)
		"zeta_face":
			# Her hologram: a black page drawn additively, so only the lines glow in the air.
			_bg(root, BLACK)
			p.face = Face.new()
			p.face.set_anchors_preset(Control.PRESET_FULL_RECT)
			root.add_child(p.face)
			_label(root, "ZETA", "display", 110 * s, CYAN, Vector2(px.y + 8, 10 * s))  # her words are in the subtitles
		"zeta":
			_bg(root, BLACK)
			_label(root, "ZETA//CORE", "display", 40 * s, CYAN, Vector2(22, 16) * s)
			p.line = _label(root, "System active.", "text", 28 * s, WHITE, Vector2(22, 90) * s)
			p.line.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
			p.line.size = Vector2(px.x - 44 * s, 150 * s)
			_label(root, "[ I ACCEPT ]", "mono", 22 * s, YELLOW, Vector2(22, 245) * s)


func solved(kind: String, fragment := -1) -> void:
	var p: Dictionary = parts.get(kind, {})
	touch.call_deferred(kind)
	match kind:
		"zphone":
			p.state.text = "UNLOCKED"
			p.state.add_theme_color_override("font_color", CYAN)
		"report":
			p.title.text = "DISCONNECTED  ·  FRAGMENT 1: %d" % fragment
			p.title.add_theme_color_override("font_color", YELLOW)
		"zdrive":
			p.top.text = "SERVICE MODE: OK"
			p.mid.text = "FRAGMENT 2: %d" % fragment
			p.code.text = "BRAKES: ACTIVE"
		"kiosk":
			p.body.text = "RECEIPT PRINTED\nFRAGMENT 3: %d" % fragment
		"meter":
			p.v -= 8.0


func update(sim: Sim, zeta_line: String) -> void:
	_frames += 1
	if _frames == 30:  # the pages have settled: stop redrawing them every frame
		for kind in parts:
			touch(kind)
	if parts.has("meter"):
		var p: Dictionary = parts.meter
		p.v = clampf(p.v + 0.0004, 0.0, 99.99)
		var shown := "%.2f%%" % p.v
		if p.value.text != shown:
			p.value.text = shown
			touch("meter")
	if parts.has("zeta") and zeta_line != "" and parts.zeta.line.text != zeta_line:
		parts.zeta.line.text = zeta_line
		touch("zeta")
	if parts.has("zeta_face") and _frames % 3 == 0:  # her face moves, at a third of the frame rate
		var f: Face = parts.zeta_face.face
		f.t = _frames / 30.0
		f.talk = 1.0 if Time.get_ticks_msec() < _talk_until else 0.0
		f.queue_redraw()
		touch("zeta_face")


func zeta_says(seconds: float) -> void:
	_talk_until = Time.get_ticks_msec() + int(seconds * 1000.0)


func _bg(root: Control, c: Color) -> void:
	var r := ColorRect.new()
	r.color = c
	r.set_anchors_preset(Control.PRESET_FULL_RECT)
	root.add_child(r)


func _label(root: Control, text: String, font: String, size: float, c: Color, pos: Vector2) -> Label:
	var l := Label.new()
	l.text = text
	l.position = pos
	l.add_theme_font_override("font", fonts[font])
	l.add_theme_font_size_override("font_size", int(size))
	l.add_theme_color_override("font_color", c)
	l.add_theme_constant_override("line_spacing", int(-size * 0.12))
	root.add_child(l)
	return l


func _chip(root: Control, text: String, pos: Vector2, s: float) -> void:
	var panel := PanelContainer.new()
	var sb := StyleBoxFlat.new()
	sb.bg_color = YELLOW
	sb.content_margin_left = 8 * s
	sb.content_margin_right = 8 * s
	sb.skew = Vector2(0.25, 0)
	panel.add_theme_stylebox_override("panel", sb)
	panel.position = pos
	root.add_child(panel)
	var l := Label.new()
	l.text = text
	l.add_theme_font_override("font", fonts.mono)
	l.add_theme_font_size_override("font_size", int(18 * s))
	l.add_theme_color_override("font_color", BLACK)
	panel.add_child(l)


func _gesture(root: Control, letter: String, origin: Vector2, step: float, c: Color, width: float) -> void:
	for i in 9:
		var dot := ColorRect.new()
		dot.color = c
		dot.size = Vector2(step, step) * 0.16
		dot.position = origin + Vector2(i % 3, i / 3) * step - dot.size / 2
		root.add_child(dot)
	if letter == "":
		return
	var line := Line2D.new()
	line.width = width
	line.default_color = c
	line.joint_mode = Line2D.LINE_JOINT_SHARP
	for k in GESTURES[letter]:
		line.add_point(origin + Vector2((k - 1) % 3, (k - 1) / 3) * step)
	root.add_child(line)
