class_name Nav
extends RefCounted
## Walkable floor as a grid: room interiors are open, walls and furniture are solid, doors open and close.

const CELL := 0.25
const ORIGIN := Vector2(-2.0, -1.0)

var grid := AStarGrid2D.new()
var doors := {}  # door id -> Array of Vector2i cells


func _init(layout: Dictionary) -> void:
	grid.region = Rect2i(0, 0, int(36.0 / CELL), int(12.0 / CELL))
	grid.cell_size = Vector2(CELL, CELL)
	grid.offset = ORIGIN + Vector2(CELL, CELL) * 0.5
	grid.diagonal_mode = AStarGrid2D.DIAGONAL_MODE_ONLY_IF_NO_OBSTACLES
	grid.default_compute_heuristic = AStarGrid2D.HEURISTIC_OCTILE
	grid.update()
	grid.fill_solid_region(grid.region, true)
	for r in layout.rooms:
		_fill(Rect2(r.x + 0.3, r.z + 0.4, r.w - 0.6, r.d - 0.75), false)
	for p in layout.props:
		if not p.has("w"):
			continue
		var size := Vector2(p.w, p.d)
		if absf(fmod(absf(p.yaw), 180.0) - 90.0) < 1.0:
			size = Vector2(p.d, p.w)
		_fill(Rect2(Vector2(p.x, p.z) - size / 2.0 - Vector2(0.15, 0.15), size + Vector2(0.3, 0.3)), true)
	for d in layout.doors:
		var rect := Rect2(d.x - 0.7, d.z - d.width / 2.0 + 0.2, 1.4, d.width - 0.4)
		if d.wall == "west":
			rect = rect.grow_side(SIDE_LEFT, 1.0)
		elif d.wall == "east":
			rect = rect.grow_side(SIDE_RIGHT, 1.2)
		doors[d.id] = _cells(rect)
		set_door(d.id, d.id == "wejscie")


func set_door(id: String, open: bool) -> void:
	for c in doors[id]:
		grid.set_point_solid(c, not open)


func path(from: Vector3, to: Vector3) -> PackedVector3Array:
	var a := _open_near(_cell(Vector2(from.x, from.z)))
	var b := _open_near(_cell(Vector2(to.x, to.z)))
	var pts := grid.get_point_path(a, b)
	var out := PackedVector3Array()
	if pts.is_empty():
		return out
	var anchor := 0
	for i in range(2, pts.size()):
		if not _clear(pts[anchor], pts[i]):
			out.append(Vector3(pts[i - 1].x, 0, pts[i - 1].y))
			anchor = i - 1
	out.append(Vector3(pts[pts.size() - 1].x, 0, pts[pts.size() - 1].y))
	return out


func is_open(p: Vector3) -> bool:
	var c := _cell(Vector2(p.x, p.z))
	return grid.is_in_boundsv(c) and not grid.is_point_solid(c)


func _cell(p: Vector2) -> Vector2i:
	var c := Vector2i(floori((p.x - ORIGIN.x) / CELL), floori((p.y - ORIGIN.y) / CELL))
	return c.clamp(grid.region.position, grid.region.end - Vector2i.ONE)


func _cells(rect: Rect2) -> Array:
	var out := []
	var a := _cell(rect.position)
	var b := _cell(rect.end)
	for x in range(a.x, b.x + 1):
		for y in range(a.y, b.y + 1):
			out.append(Vector2i(x, y))
	return out


func _fill(rect: Rect2, solid: bool) -> void:
	for c in _cells(rect):
		grid.set_point_solid(c, solid)


func _open_near(c: Vector2i) -> Vector2i:
	if not grid.is_point_solid(c):
		return c
	for radius in range(1, 10):
		for dx in range(-radius, radius + 1):
			for dy in range(-radius, radius + 1):
				var n := c + Vector2i(dx, dy)
				if grid.is_in_boundsv(n) and not grid.is_point_solid(n):
					return n
	return c


func _clear(a: Vector2, b: Vector2) -> bool:
	var steps := int(a.distance_to(b) / (CELL * 0.4)) + 1
	for i in range(steps + 1):
		var c := _cell(a.lerp(b, float(i) / steps))
		if grid.is_point_solid(c):
			return false
	return true
