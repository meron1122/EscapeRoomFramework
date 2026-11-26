"""Builds every 3D asset of the simulator in Blender and exports them as glTF for Godot.

Run: /Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup --python simulator/blender/build_assets.py
Append `-- room_hol npc ...` to rebuild only those assets.
Coordinates below are Godot's (x right, y up, z towards the camera); `bl()` converts them for Blender.
Objects that Godot drives (Screen, Headlights, LegL, Door, ...) keep fixed names (KEEP and the npc pivots); so do the
Jacket, Skin and Visor materials. Everything else in a model is merged into one mesh per parent when it is exported,
so a room or prop is a handful of draw calls in WebGL.
"""
import json
import math
import pathlib
import random
import sys

import bpy
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "godot" / "assets"
LAYOUT = json.loads((ROOT / "godot" / "layout.json").read_text())
ONLY = set(sys.argv[sys.argv.index("--") + 1:]) if "--" in sys.argv else set()
KEEP = {"Screen", "Headlights", "Ring", "KeypadLight", "FlapL", "FlapR", "DoorL", "DoorR", "Core", "Door",
        *(f"SlotLight{i}" for i in range(6)), *(f"Sensor{i}" for i in range(5)), *(f"SlotRing{i}" for i in range(3))}
FONT = None


# ---------- materials ----------

def lin(hex_):
    c = [int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    return [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]


def material(name, color, rough=0.5, metal=0.0, emit=None, strength=0.0, alpha=1.0):
    m = bpy.data.materials.get(name)
    if m:
        return m
    m = bpy.data.materials.new(name)
    bsdf = next(n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    bsdf.inputs["Base Color"].default_value = (*lin(color), 1)
    bsdf.inputs["Roughness"].default_value = rough
    bsdf.inputs["Metallic"].default_value = metal
    if emit:
        bsdf.inputs["Emission Color"].default_value = (*lin(emit), 1)
        bsdf.inputs["Emission Strength"].default_value = strength
    if alpha < 1:
        bsdf.inputs["Alpha"].default_value = alpha
        for attr, value in (("surface_render_method", "BLENDED"), ("blend_method", "BLEND")):
            try:
                setattr(m, attr, value)
            except (AttributeError, TypeError):
                pass
    return m


def tex_material(name, img, rough=0.5, metal=0.0, normal=None, emit=0.0):
    """A material whose base colour (and optionally emission and normal) come from generated images."""
    m = bpy.data.materials.get(name) or material(name, "#ffffff", rough, metal)
    nt = m.node_tree
    bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
    t = nt.nodes.new("ShaderNodeTexImage")
    t.image = img
    nt.links.new(t.outputs["Color"], bsdf.inputs["Base Color"])
    if emit:
        nt.links.new(t.outputs["Color"], bsdf.inputs["Emission Color"])
        bsdf.inputs["Emission Strength"].default_value = emit
    if normal:
        n = nt.nodes.new("ShaderNodeTexImage")
        n.image = normal
        nm = nt.nodes.new("ShaderNodeNormalMap")
        nt.links.new(n.outputs["Color"], nm.inputs["Color"])
        nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
    return m


# ---------- procedural textures (numpy, sRGB, row 0 at the bottom) ----------

def srgb(hex_):
    return np.array([int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5)])


def _vnoise(n, cy, cx, rng):
    """Tileable value noise with cy x cx cells."""
    g = rng.random((cy, cx))
    ty, tx = np.arange(n) * cy / n, np.arange(n) * cx / n
    y0, x0 = ty.astype(int), tx.astype(int)
    fy, fx = ty - y0, tx - x0
    fy, fx = (fy * fy * (3 - 2 * fy))[:, None], (fx * fx * (3 - 2 * fx))[None, :]
    y1, x1 = (y0 + 1) % cy, (x0 + 1) % cx
    top = g[np.ix_(y0, x0)] * (1 - fx) + g[np.ix_(y0, x1)] * fx
    bot = g[np.ix_(y1, x0)] * (1 - fx) + g[np.ix_(y1, x1)] * fx
    return top * (1 - fy) + bot * fy


def fbm(n, rng, cells=(4, 8, 16, 32), stretch=1):
    out = sum(_vnoise(n, c * stretch, c, rng) * 0.55 ** k for k, c in enumerate(cells))
    return out / sum(0.55 ** k for k in range(len(cells)))


def image(name, rgb, data=False):
    h, w, _ = rgb.shape
    # is_data at creation: changing the colour space afterwards regenerates the image and wipes the pixels
    img = bpy.data.images.new(name, w, h, is_data=data)
    px = np.ones((h, w, 4), np.float32)
    px[..., :3] = np.clip(rgb, 0, 1)
    img.pixels.foreach_set(px.ravel())
    img.pack()
    return img


def normal_map(name, height, strength):
    gx = (np.roll(height, -1, 1) - np.roll(height, 1, 1)) * strength
    gy = (np.roll(height, -1, 0) - np.roll(height, 1, 0)) * strength
    nrm = np.dstack([-gx, -gy, np.ones_like(height)])
    nrm /= np.linalg.norm(nrm, axis=2, keepdims=True)
    return image(name, nrm * 0.5 + 0.5, data=True)


def tex_stone():
    """Light warm-grey terrazzo, 1 m tiles, the image covers 2 m."""
    n, rng = 512, np.random.default_rng(1)
    shade = 1 + (fbm(n, rng) - 0.5) * 0.08
    rgb = srgb("#b3ada3") * shade[..., None]
    chips = np.kron(rng.random((n // 2, n // 2)), np.ones((2, 2)))
    rgb[chips > 0.975] = srgb("#8c857a")
    rgb[chips < 0.02] = srgb("#d3cec5")
    idx = np.arange(n) // 256
    rgb *= 1 + rng.uniform(-0.03, 0.03, (2, 2))[np.ix_(idx, idx)][..., None]
    m = np.arange(n) % 256
    g = (m < 1) | (m > 254)
    rgb[g[:, None] | g[None, :]] = srgb("#9c968c")
    return image("Stone", rgb)


def tex_brushed(name, base, seed, amp=1.0):
    """Brushed panel, the image covers 1 m; amp < 1 keeps painted walls quiet."""
    n, rng = 256, np.random.default_rng(seed)
    streak = fbm(n, rng, (2, 4, 8, 16), stretch=24)
    shade = 1 + ((streak - 0.5) * 0.35 + (fbm(n, rng, (2, 4)) - 0.5) * 0.25) * amp
    return image(name, srgb(base) * shade[..., None])


def tex_plates():
    """Steel floor plates with seams and rivets: 1 m plates, the image covers 2 m. Returns (colour, normal)."""
    n, rng = 512, np.random.default_rng(2)
    streak = fbm(n, rng, (2, 4, 8, 16), stretch=16)
    shade = 1 + (streak - 0.5) * 0.22 + (fbm(n, rng, (3, 6, 12)) - 0.5) * 0.3
    yy, xx = np.mgrid[0:n, 0:n] % 256
    height = np.ones((n, n))
    edge = np.minimum(np.minimum(xx, 255 - xx), np.minimum(yy, 255 - yy))
    height = np.clip(edge / 4.0, 0, 1)
    for cx in (14, 128, 241):
        for cy in (14, 128, 241):
            if cx == 128 and cy == 128:
                continue
            d = (xx - cx) ** 2 + (yy - cy) ** 2
            height += np.clip(1 - d / 22.0, 0, 1) * 0.8
    rgb = srgb("#a4aaae") * shade[..., None] * (0.75 + 0.25 * np.clip(height, 0, 1))[..., None]
    rgb += (height > 1.2)[..., None] * 0.04
    return image("Plates", rgb), normal_map("PlatesN", height, 2.5)


def tex_diamond():
    """Diamond (checker) plate, the image covers 0.5 m."""
    n, rng, c = 512, np.random.default_rng(3), 32
    yy, xx = np.mgrid[0:n, 0:n]
    u, v = (xx % c) / c * 2 - 1, (yy % c) / c * 2 - 1
    flip = ((xx // c + yy // c) % 2) * 2 - 1
    a, b = (u + flip * v) / 1.414, (u - flip * v) / 1.414
    height = np.clip(1 - (a / 0.78) ** 2 - (b / 0.2) ** 2, 0, 1) ** 0.5
    shade = 1 + (fbm(n, rng, (2, 4, 8, 16)) - 0.5) * 0.35
    rgb = srgb("#6a727b") * shade[..., None] * (0.8 + 0.3 * height)[..., None]
    return image("Diamond", rgb), normal_map("DiamondN", height, 3.0)


def tex_grating():
    """Bar grating over a dark pit, the image covers 0.5 m."""
    n, rng = 512, np.random.default_rng(4)
    yy, xx = np.mgrid[0:n, 0:n]
    bar = ((xx % 32) < 7) | ((yy % 96) < 5)
    height = bar * 1.0
    shade = 1 + (fbm(n, rng, (2, 4, 8)) - 0.5) * 0.3
    rgb = np.where(bar[..., None], srgb("#6b7580") * shade[..., None], srgb("#2c323a") + (fbm(n, rng, (4, 8)) * 0.03)[..., None])
    return image("Grating", rgb), normal_map("GratingN", height, 2.0)


def tex_concrete():
    """Cold blue-grey board-formed concrete with tie holes and water streaks, the image covers 2.4 m."""
    n, rng = 512, np.random.default_rng(5)
    shade = 1 + (fbm(n, rng, (4, 8, 16, 32, 64, 128)) - 0.5) * 0.4
    shade -= (rng.random((n, n)) > 0.994) * 0.25
    yy, xx = np.mgrid[0:n, 0:n]
    px, py = xx % 256, yy % 128
    shade[(px < 2) | (py < 2)] *= 0.6
    for cx in (40, 216):
        for cy in (32, 96):
            shade[((px - cx) ** 2 + (py - cy) ** 2) < 22] *= 0.35
    for _ in range(70):
        x, top, length, w = rng.integers(0, n), rng.integers(0, 4) * 128 + 127, rng.integers(20, 120), rng.integers(1, 5)
        ys = np.arange(max(0, top - length), top)
        fade = (ys - (top - length)) / length
        for dx in range(w):
            shade[ys, (x + dx) % n] *= 1 - 0.18 * fade
    return image("Concrete", srgb("#5c636c") * shade[..., None])


def tex_hazard():
    """Worn yellow/black stripes, one stripe pair per image (0.4 m)."""
    n, rng = 128, np.random.default_rng(6)
    yy, xx = np.mgrid[0:n, 0:n]
    s = ((xx + yy) % 128) < 64
    rgb = np.where(s[..., None], srgb("#f2e20c"), srgb("#2a2d32"))
    return image("Hazard", rgb * (1 - (fbm(n, rng, (4, 8, 16)) > 0.64)[..., None] * 0.3))


def tex_rug():
    """A muted kilim for the cosy corner of the lobby, stretched over the whole rug."""
    n = 256
    yy, xx = np.mgrid[0:n, 0:n] / n
    d = np.minimum(np.minimum(xx, 1 - xx), np.minimum(yy, 1 - yy))
    pal = [srgb(c) for c in ("#5b5048", "#8a6a58", "#a89276", "#d6ccb8", "#6f6358")]
    rgb = np.empty((n, n, 3))
    rgb[:] = pal[4]
    for lim, c in ((0.14, pal[1]), (0.09, pal[3]), (0.07, pal[2]), (0.03, pal[0])):
        rgb[d < lim] = c
    cx, cy = np.abs(xx - 0.5), np.abs(yy - 0.5)
    inner = d >= 0.14
    lozenge = (np.abs(((cx * 6) % 1) - 0.5) + np.abs(((cy * 4) % 1) - 0.5)) < 0.32
    rgb[inner & lozenge] = pal[2]
    rgb[inner & ((cx + cy) < 0.16)] = pal[3]
    rng = np.random.default_rng(7)
    return image("Rug", rgb * (0.88 + 0.24 * fbm(n, rng, (8, 16, 32)))[..., None])


M = {}


def mats():
    stone = tex_stone()
    plates, plates_n = tex_plates()
    diamond, diamond_n = tex_diamond()
    grating, grating_n = tex_grating()
    M.update({
        # props and trims: a calm, desaturated base at mid values; colour only where play or story needs it
        "white": material("White", "#e2e0da", 0.45),
        "matte": material("WhiteMatte", "#cfd1d2", 0.6),
        "beige": material("Beige", "#cbbfa9", 0.6),
        "black": material("Charcoal", "#2a2d32", 0.35),
        "graphite": material("Graphite", "#454b53", 0.45, 0.15),
        "steel": material("Steel", "#a2a9b0", 0.35, 0.5),
        "dsteel": material("DarkSteel", "#5d646c", 0.4, 0.35),
        "chrome": material("Chrome", "#c9d0d6", 0.15, 0.6),
        "brass": material("Brass", "#a88a58", 0.35, 0.5),
        "yellow": material("YellowPaint", "#fcee0a", 0.45),  # brand yellow: Z logos, labels, hazard only
        "ochre": material("Ochre", "#bfae62", 0.6),
        "panel": material("PanelWarm", "#9a958d", 0.55),
        "panel_t": material("PanelTeal", "#71878a", 0.5, 0.1),
        "glass": material("Glass", "#d6e6ee", 0.05, alpha=0.22),
        "wglass": material("WindowGlass", "#a9bccb", 0.04, 0.0, alpha=0.12),
        "dglass": material("SmokedGlass", "#2a3238", 0.05, 0.2, alpha=0.6),
        "leaf": material("Leaf", "#3a6243", 0.7),
        "leaf2": material("Leaf2", "#527a55", 0.7),
        "soil": material("Soil", "#3a3029", 0.95),
        "fabric": material("Fabric", "#6b6560", 0.85),
        "fabric_d": material("FabricDark", "#4d4a47", 0.85),
        "leather_w": material("LeatherWarm", "#7d6f62", 0.5),
        "wood": material("WoodDark", "#5a4030", 0.5),
        "wood_l": material("WoodLight", "#b89a74", 0.5),
        "olive": material("CaseSteel", "#5c6255", 0.55, 0.3),
        "olive2": material("CaseSteelWorn", "#6d7264", 0.7, 0.2),
        "rubber": material("Rubber", "#2a2b2d", 0.85),
        "cable": material("Cable", "#2c2f33", 0.55),
        "cable_y": material("CableYellow", "#b3a54a", 0.5),
        "cable_c": material("CableCyan", "#3f8288", 0.5),
        "cable_w": material("CableWhite", "#c9ced3", 0.5),
        "tape": material("DuctTape", "#7d8288", 0.8, 0.2),
        "paint_w": material("PaintWhite", "#e4e0d4", 0.9),
        "paint_c": material("PaintCyan", "#4aa3aa", 0.85),
        "red": material("RedPaint", "#a83a34", 0.45),
        "pipe": material("PipePaint", "#5a636c", 0.4, 0.3),
        "pipe_t": material("PipeTeal", "#4f7471", 0.4, 0.3),
        # former neon: plain, muted trim with no emission
        "ny": material("TrimChampagne", "#b5a27a", 0.4, 0.3),
        "nc": material("TrimSteelTeal", "#7d9aa0", 0.4, 0.2),
        "nt": material("TrimTeal", "#7fa39e", 0.45),
        "nw": material("TrimOffWhite", "#e6e4de", 0.5),
        "warm": material("Lampshade", "#e3d8c2", 0.7),
        "light": material("Diffuser", "#e4e6e8", 0.5),
        "liquid": material("LiquidTeal", "#5f948d", 0.1),
        "liquid_c": material("LiquidCyan", "#5f8fa0", 0.1),
        "poster_y": material("PosterPaper", "#e8e4da", 0.6),
        "poster_t": material("PosterTeal", "#4d6663", 0.6),
        "poster_k": material("PosterDark", "#2f343a", 0.6),
        # light that matters for play: small indicators, exit signs, screens, the core
        "led_c": material("LedCyan", "#7fe6ee", 0.3, emit="#00f0ff", strength=1.5),
        "led_y": material("LedYellow", "#fcee0a", 0.3, emit="#fcee0a", strength=1.5),
        "led_w": material("LedWhite", "#f4f6f8", 0.3, emit="#eef4ff", strength=1.5),
        "ledr": material("LedRed", "#ff2020", 0.3, emit="#ff2020", strength=1.5),
        "ng": material("ExitGreen", "#00c853", 0.3, emit="#00c853", strength=1.2),
        "screen": material("Screen", "#05080a", 0.2, emit="#0b3a44", strength=1.0),
        "display": material("Display", "#05080a", 0.2, emit="#0f4a57", strength=1.0),
        "lcd": material("DisplayYellow", "#fcee0a", 0.3, emit="#fcee0a", strength=1.2),
        # rooms (metallic at most 0.2 so floors and walls never go black without reflections)
        "floor_hol": tex_material("FloorTerrazzo", stone, 0.4, 0.0),
        "wall_hol": tex_material("WallPanelHol", tex_brushed("PanelHol", "#d3cfc6", 8, 0.25), 0.6, 0.0),
        "floor_pietro": tex_material("FloorPlates", plates, 0.4, 0.1, normal=plates_n),
        "wall_pietro": tex_material("WallPanelLab", tex_brushed("PanelLab", "#b2c2c0", 9, 0.25), 0.55, 0.0),
        "floor_jadro": tex_material("FloorGrating", grating, 0.45, 0.2, normal=grating_n),
        "diamond": tex_material("DiamondPlate", diamond, 0.35, 0.2, normal=diamond_n),
        "wall_jadro": tex_material("WallConcrete", tex_concrete(), 0.8, 0.0),
        "hazard": tex_material("Hazard", tex_hazard(), 0.6),
        "rug": tex_material("Rug", tex_rug(), 0.95),
        # characters (Godot tints Jacket per character and drives the Visor colour)
        "jacket": material("Jacket", "#4a4f57", 0.75),
        "skin": material("Skin", "#b8987e", 0.6),
        "visor": material("Visor", "#7fc8cf", 0.25, emit="#5fd0da", strength=0.6),
        "pants": material("Pants", "#3a3e44", 0.85),
        "boots": material("Boots", "#3a332d", 0.55),
        "leather": material("Leather", "#5a4636", 0.5),
        "webbing": material("Webbing", "#4a4f44", 0.9),
        "canvas": material("Canvas", "#6b6a58", 0.9),
        "canvas2": material("CanvasDark", "#55574a", 0.9),
        "cloth": material("Scarf", "#8a7a66", 0.95),
        "armor": material("Armor", "#5a6068", 0.45, 0.3),
        "glove": material("Glove", "#2e2f31", 0.6),
        "hair": material("Hair", "#2a2a2c", 0.7),
        "hair_m": material("HairDyed", "#7a4a3c", 0.55),
        "core": material("Core", "#00f0ff", 0.1, emit="#00f0ff", strength=3.0),
        # van: gritty on purpose
        "van": material("VanPaint", "#4f5a5a", 0.6, 0.2),
        "van2": material("VanPaintFaded", "#5f6a66", 0.75, 0.15),
        "primer": material("Primer", "#7a7f80", 0.85, 0.1),
        "rust": material("Rust", "#7a5a45", 0.9, 0.2),
        "mud": material("Mud", "#5a5044", 0.95),
        "tail": material("TailLight", "#7a0a0a", 0.3, emit="#ff2a1a", strength=1.0),
    })


# ---------- geometry helpers (Godot coordinates) ----------

def bl(x, y, z):
    return (x, -z, y)


def _active(o):
    bpy.ops.object.select_all(action="DESELECT")
    o.select_set(True)
    bpy.context.view_layer.objects.active = o


def finish(obj, mat, bevel, seg=2):
    obj.data.materials.append(mat)
    if bevel:
        mod = obj.modifiers.new("Bevel", "BEVEL")
        mod.width = bevel
        mod.segments = seg
        mod.limit_method = "ANGLE"
    for p in obj.data.polygons:
        p.use_smooth = False
    return obj


def _euler(rot):
    # Godot x -> Blender x, Godot y (yaw) -> Blender z, Godot z (roll) -> Blender -y
    return (math.radians(rot[0]), math.radians(-rot[2]), math.radians(rot[1]))


def box(name, size, pos, mat, bevel=0.0, rot=(0, 0, 0), seg=2):
    """size and pos in Godot axes; rot = (pitch about x, yaw about y, roll about z) in degrees."""
    bpy.ops.mesh.primitive_cube_add(size=1, location=bl(*pos))
    o = bpy.context.active_object
    o.name = name
    o.scale = (size[0], size[2], size[1])
    o.rotation_euler = _euler(rot)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    return finish(o, mat, bevel, seg)


def cyl(name, radius, height, pos, mat, axis="y", verts=24, bevel=0.0, r2=None):
    if r2 is None:
        bpy.ops.mesh.primitive_cylinder_add(vertices=verts, radius=radius, depth=height, location=bl(*pos))
    else:
        bpy.ops.mesh.primitive_cone_add(vertices=verts, radius1=radius, radius2=r2, depth=height, location=bl(*pos))
    o = bpy.context.active_object
    o.name = name
    if axis == "x":
        o.rotation_euler = (0, math.radians(90), 0)
    elif axis == "z":
        o.rotation_euler = (math.radians(90), 0, 0)
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
    return finish(o, mat, bevel)


def torus(name, major, minor, pos, mat, axis="y", seg=32, mseg=8, rot=(0, 0, 0), squash=1.0):
    """squash < 1 flattens the tube along its axis (floor rings stay decal-thin)."""
    bpy.ops.mesh.primitive_torus_add(major_radius=major, minor_radius=minor, major_segments=seg, minor_segments=mseg,
                                     location=bl(*pos))
    o = bpy.context.active_object
    o.name = name
    o.scale = (1, 1, squash)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    base = {"y": (0, 0, 0), "z": (90, 0, 0), "x": (0, 0, 90)}[axis]
    o.rotation_euler = _euler(tuple(a + b for a, b in zip(base, rot)))
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
    return finish(o, mat, 0)


def ball(name, radius, pos, mat, subdiv=2, scale=(1, 1, 1), rot=(0, 0, 0)):
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=subdiv, radius=radius, location=bl(*pos))
    o = bpy.context.active_object
    o.name = name
    o.scale = (scale[0], scale[2], scale[1])
    o.rotation_euler = _euler(rot)
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    return finish(o, mat, 0)


def cable(name, pts, radius, mat, res=6, sides=1):
    """A tube through the points (Godot coords): cables, hoses, bent pipes. Two points give a straight rod."""
    cu = bpy.data.curves.new(name, "CURVE")
    cu.dimensions = "3D"
    cu.bevel_depth = radius
    cu.bevel_resolution = sides
    cu.use_fill_caps = True
    sp = cu.splines.new("NURBS")
    sp.points.add(len(pts) - 1)
    for p, q in zip(sp.points, pts):
        p.co = (*bl(*q), 1.0)
    sp.use_endpoint_u = True
    sp.order_u = min(3, len(pts))
    sp.resolution_u = res if len(pts) > 2 else 1
    o = bpy.data.objects.new(name, cu)
    bpy.context.collection.objects.link(o)
    o.data.materials.append(mat)
    _active(o)
    bpy.ops.object.convert(target="MESH")
    return o


def droop(a, b, sag, n=6):
    """Points of a cable hanging between a and b."""
    return [[a[k] + (b[k] - a[k]) * t - (sag * 4 * t * (1 - t) if k == 1 else 0) for k in range(3)]
            for t in (i / n for i in range(n + 1))]


def text(name, s, pos, size, mat, yaw=0.0, flat=False, depth=0.01, align="CENTER", pitch=0.0):
    """Extruded text facing +z (turned by yaw like a prop), or lying flat reading along +x with its top to the north."""
    bpy.ops.object.text_add()
    o = bpy.context.active_object
    o.name = name
    c = o.data
    c.body, c.font, c.size, c.extrude = s, FONT, size, depth / 2
    c.align_x, c.align_y, c.resolution_u = align, "CENTER", 2
    bpy.ops.object.convert(target="MESH")
    o.rotation_euler = (math.radians((0 if flat else 90) + pitch), 0, math.radians(yaw))
    o.location = bl(*pos)
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
    return finish(o, mat, 0)


def uvw(o, tile, off=(0.0, 0.0, 0.0)):
    """Box-projected UVs from world position, so tiling textures keep their real-world scale on any part."""
    tu, tv = tile if isinstance(tile, tuple) else (tile, tile)
    me, mw = o.data, o.matrix_world
    if not me.uv_layers:
        me.uv_layers.new()
    uv = me.uv_layers.active.data
    rot = mw.to_3x3()
    for p in me.polygons:
        n = rot @ p.normal
        ax = max(range(3), key=lambda i: abs(n[i]))
        for li in p.loop_indices:
            co = mw @ me.vertices[me.loops[li].vertex_index].co
            gx, gy, gz = co.x - off[0], co.z - off[1], -co.y - off[2]
            u, v = (gx, -gz) if ax == 2 else (gx, gy) if ax == 1 else (-gz, gy)
            uv[li].uv = (u / tu, v / tv)
    return o


def tbox(name, size, pos, mat, tile, off=(0.0, 0.0, 0.0), **kw):
    return uvw(box(name, size, pos, M[mat], **kw), tile, off)


def pivot(name, pos, *children):
    """An empty at a joint (hip, shoulder, hinge) so Godot can rotate the part around it."""
    e = bpy.data.objects.new(name, None)
    bpy.context.collection.objects.link(e)
    e.location = bl(*pos)
    bpy.context.view_layer.update()  # matrix_world is stale until the depsgraph updates; without this parts float
    for c in children:
        world = c.matrix_world.copy()
        c.parent = e
        c.matrix_world = world
    return e


def join(objs, name, apply=True):
    """Merges meshes (modifiers applied) into one object; with apply the result has an identity transform."""
    objs = [o for o in objs if o.type == "MESH"]
    if not objs:
        return None
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    bpy.ops.object.convert(target="MESH")
    if len(objs) > 1:
        bpy.ops.object.join()
    o = bpy.context.view_layer.objects.active
    if apply:
        bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    o.name = o.data.name = name
    return o


def set_origin(o, pos):
    bpy.context.scene.cursor.location = bl(*pos)
    _active(o)
    bpy.ops.object.origin_set(type="ORIGIN_CURSOR")
    bpy.context.scene.cursor.location = (0, 0, 0)


class Made:
    """Collects the objects created inside a with-block, so a part built at the origin can be placed as one."""

    def __enter__(self):
        self.before = set(bpy.data.objects)
        return self

    def __exit__(self, *exc):
        self.objs = [o for o in bpy.data.objects if o not in self.before]


def place(objs, pos, yaw=0.0, name="Group"):
    """Moves parts built around the origin facing +z to pos, turned by yaw (Godot convention: 90 faces +x)."""
    o = join(objs, name)
    o.rotation_euler = (0, 0, math.radians(yaw))
    o.location = bl(*pos)
    return o


def z_letter(prefix, cx, cy, cz, h, mat, depth=0.06):
    """The Z mark: two bars and a diagonal, built from boxes."""
    w, t = h * 0.8, h * 0.18
    box(prefix + "Top", (w, t, depth), (cx, cy + h / 2 - t / 2, cz), mat)
    box(prefix + "Bottom", (w, t, depth), (cx, cy - h / 2 + t / 2, cz), mat)
    diag = math.hypot(w - t, h - 2 * t)
    box(prefix + "Diag", (diag, t, depth), (cx, cy, cz), mat, rot=(0, 0, math.degrees(math.atan2(h - 2 * t, w - t))))


def z_flat(prefix, cx, cz, h, mat, y=0.004):
    """The Z mark lying on the floor, reading correctly from above (top bar to the north)."""
    w, t = h * 0.8, h * 0.18
    box(prefix + "Top", (w, 0.006, t), (cx, y, cz - h / 2 + t / 2), mat)
    box(prefix + "Bottom", (w, 0.006, t), (cx, y, cz + h / 2 - t / 2), mat)
    diag = math.hypot(w - t, h - 2 * t)
    box(prefix + "Diag", (diag, 0.006, t), (cx, y, cz), mat, rot=(0, math.degrees(math.atan2(h - 2 * t, w - t)), 0))


def clear():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()


def export(name):
    """Merges everything Godot does not look up by name (one mesh per parent), smooths by angle and writes the glb."""
    groups = {}
    for o in bpy.data.objects:
        if o.type in ("MESH", "CURVE", "FONT") and o.name not in KEEP:
            groups.setdefault(o.parent, []).append(o)
    for parent, objs in groups.items():
        join(objs, f"{parent.name}Mesh" if parent else "Body", apply=parent is None)
    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    bpy.ops.object.select_all(action="DESELECT")
    for o in meshes:
        o.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    bpy.ops.object.convert(target="MESH")
    bpy.ops.object.shade_smooth_by_angle(angle=math.radians(50))
    OUT.mkdir(parents=True, exist_ok=True)
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.export_scene.gltf(filepath=str(OUT / f"{name}.glb"), export_format="GLB", use_selection=True, export_apply=True)
    clear()
    print("exported", name)


# ---------- room kit ----------

H, LOW = LAYOUT["wallHeight"], LAYOUT["lowWall"]


def wall_x(name, x, z0, z1, height, mat, gaps=(), t=0.2, lintel=None, tile=None):
    """A wall running along z, with door gaps (z centre, width); a lintel closes a gap above the door in tall walls."""
    cuts = sorted((gz - gw / 2, gz + gw / 2) for gz, gw in gaps)
    edge, parts = z0, []
    for a, b in cuts + [(z1, z1)]:
        if a > edge:
            parts.append(box(name, (t, height, a - edge), (x, height / 2, (edge + a) / 2), M[mat]))
        if b > a and lintel:
            parts.append(box(name + "Lintel", (t, height - lintel, b - a), (x, (height + lintel) / 2, (a + b) / 2), M[mat]))
        edge = b
    for p in parts:
        uvw(p, tile or 1.0)
    return parts


def low_wall(x, z0, z1, mat, gaps, neon, t=0.4):
    """A low divider (the camera looks over it): wall, dark cap with a trim line and a reveal groove on both faces."""
    wall_x("Divider", x, z0, z1, LOW, mat, gaps=gaps, t=t)
    cuts = sorted((gz - gw / 2, gz + gw / 2) for gz, gw in gaps)
    edge = z0
    for a, b in cuts + [(z1, z1)]:
        if a > edge:
            c, ln = (edge + a) / 2, a - edge
            box("Cap", (t + 0.04, 0.05, ln), (x, LOW + 0.025, c), M["graphite"], 0.01)
            box("CapNeon", (0.03, 0.012, ln - 0.04), (x, LOW + 0.056, c), M[neon])
            for s in (-1, 1):
                box("Groove", (0.006, 0.02, ln), (x + s * (t / 2 + 0.002), 0.55, c), M["graphite"])
                box("Kick", (0.01, 0.1, ln), (x + s * (t / 2 + 0.004), 0.05, c), M["graphite"])
        edge = b


def portal(name, x, zc, width, height, mat, neon, label_w=None, label_e=None, ink="nw"):
    """A heavy door portal in a low divider: posts, header with signs on both faces, trim reveals."""
    t = 0.46
    for s in (-1, 1):
        z = zc + s * (width / 2 + 0.07)
        box(name + "Post", (t, height, 0.14), (x, height / 2, z), M[mat], 0.012)
        box(name + "Reveal", (t - 0.06, height - 0.1, 0.012), (x, height / 2, zc + s * (width / 2 + 0.002)), M[neon])
        tbox(name + "Haz", (t + 0.01, 0.5, 0.145), (x, 0.25, z), "hazard", 0.4)
    box(name + "Header", (t, 0.4, width + 0.28), (x, height + 0.2, zc), M[mat], 0.012)
    for s, label in ((-1, label_w), (1, label_e)):
        if label:
            box(name + "Sign", (0.01, 0.26, width + 0.1), (x + s * (t / 2 + 0.005), height + 0.2, zc), M["black"])
            text(name + "Text", label, (x + s * (t / 2 + 0.012), height + 0.2, zc), 0.13, M[ink], yaw=90 * s)


def window(xa, xb, y0, y1, accent, t=0.2):
    """Window opening in a north wall (inner face at z=0): reveal frame, mullions, transom, glass, trimmed sill."""
    w, h, cx, cy = xb - xa, y1 - y0, (xa + xb) / 2, (y0 + y1) / 2
    fm = M["graphite"]
    box("WinJamb", (0.07, h, t + 0.04), (xa + 0.035, cy, -t / 2), fm)
    box("WinJamb", (0.07, h, t + 0.04), (xb - 0.035, cy, -t / 2), fm)
    box("WinHead", (w, 0.08, t + 0.04), (cx, y1 - 0.04, -t / 2), fm)
    n = max(2, round(w / 0.7))
    for i in range(1, n):
        box("Mullion", (0.045, h, 0.1), (xa + w * i / n, cy, -t / 2), fm)
    box("Transom", (w, 0.045, 0.1), (cx, y1 - 0.5, -t / 2), fm)
    box("Glass", (w, h, 0.01), (cx, cy, -t / 2 - 0.04), M["wglass"])
    box("Sill", (w + 0.14, 0.05, 0.36), (cx, y0 + 0.025, -0.04), M["dsteel"], 0.01)
    box("SillLight", (w, 0.012, 0.012), (cx, y0 + 0.004, 0.13), M[accent])
    for x in (xa + 0.004, xb - 0.004):
        box("JambLight", (0.008, h - 0.1, 0.008), (x, cy, 0.01), M[accent])


def north_wall(x0, x1, mat, windows=(), sill=0.4, head=2.9, accent="nc", tile=1.0, off=(0, 0, 0), t=0.2):
    cur, parts = x0, []
    for xa, xb in sorted(windows):
        parts.append(box("Wall", (xa - cur, H, t), ((cur + xa) / 2, H / 2, -t / 2), M[mat]))
        parts.append(box("WallSill", (xb - xa, sill, t), ((xa + xb) / 2, sill / 2, -t / 2), M[mat]))
        parts.append(box("WallHead", (xb - xa, H - head, t), ((xa + xb) / 2, (H + head) / 2, -t / 2), M[mat]))
        window(xa, xb, sill, head, accent, t)
        cur = xb
    parts.append(box("Wall", (x1 - cur, H, t), ((cur + x1) / 2, H / 2, -t / 2), M[mat]))
    for p in parts:
        uvw(p, tile, off)


def floor_slab(x0, x1, z1, mat, tile, off):
    uvw(box("Floor", (x1 - x0 + 0.4, 0.12, z1 + 0.2), ((x0 + x1) / 2, -0.06, z1 / 2 - 0.1), M[mat]), tile, off)


def south_curb(x0, x1, z1, mat, neon):
    box("CurbSouth", (x1 - x0 + 0.4, 0.25, 0.2), ((x0 + x1) / 2, 0.125, z1 + 0.1), M[mat], 0.01)
    box("CurbSouthNeon", (x1 - x0 + 0.4, 0.02, 0.03), ((x0 + x1) / 2, 0.26, z1 + 0.04), M[neon])
    box("CurbSouthCap", (x1 - x0 + 0.4, 0.02, 0.2), ((x0 + x1) / 2, 0.255, z1 + 0.1), M["graphite"])


def seams(face, x0, x1, y0, y1, step, mat, rows=(), at=0.0, skip=()):
    """Panel seams on a wall face: "n" = north wall (along x, inner face z=at), "w" = facing +x (along z, face x=at)."""
    u = x0 + step
    while u < x1 - 0.05:
        if not any(a <= u <= b for a, b in skip):
            if face == "n":
                box("Seam", (0.014, y1 - y0, 0.008), (u, (y0 + y1) / 2, at + 0.003), M[mat])
            else:
                box("Seam", (0.008, y1 - y0, 0.014), (at + 0.003, (y0 + y1) / 2, u), M[mat])
        u += step
    for y in rows:
        if face == "n":
            box("SeamH", (x1 - x0, 0.014, 0.008), ((x0 + x1) / 2, y, at + 0.003), M[mat])
        else:
            box("SeamH", (0.008, 0.014, x1 - x0), (at + 0.003, y, (x0 + x1) / 2), M[mat])


def light_slot(face, u, y0, y1, mat, at=0.0, w=0.03):
    """A vertical trim slot set into a wall panel."""
    h, y = y1 - y0, (y0 + y1) / 2
    if face == "n":
        box("SlotFrame", (w + 0.05, h + 0.05, 0.012), (u, y, at + 0.006), M["panel"])
        box("Slot", (w, h, 0.01), (u, y, at + 0.014), M[mat])
    else:
        box("SlotFrame", (0.012, h + 0.05, w + 0.05), (at + 0.006, y, u), M["panel"])
        box("Slot", (0.01, h, w), (at + 0.014, y, u), M[mat])


# The camera looks down the (-1, -1, -1) diagonal, so anything hung in front of a back wall at depth d and height y
# hides the wall around height y - d. Overhead parts stay high and shallow so screens and signs below 2.7 m stay visible.

def beams_x(x0, x1, z_out=0.38, y=H - 0.2, step=1.5, trough="light"):
    """Ceiling structure along the top of a north wall: cantilevers, an I-beam and a light trough under it."""
    m = M["graphite"]
    box("BeamTop", (x1 - x0, 0.03, 0.2), ((x0 + x1) / 2, y + 0.185, z_out), m)
    box("BeamWeb", (x1 - x0, 0.17, 0.03), ((x0 + x1) / 2, y + 0.085, z_out), m)
    box("BeamBottom", (x1 - x0, 0.03, 0.2), ((x0 + x1) / 2, y + 0.015, z_out), m)
    box("Trough", (x1 - x0, 0.05, 0.16), ((x0 + x1) / 2, y - 0.025, z_out), M["dsteel"])
    box("TroughLight", (x1 - x0 - 0.04, 0.01, 0.1), ((x0 + x1) / 2, y - 0.052, z_out), M[trough])
    x = x0 + 0.1
    while x < x1:
        box("Cantilever", (0.12, 0.2, z_out), (x, y + 0.1, z_out / 2), m)
        x += step


def beams_z(z0, z1, x_out=0.38, y=H - 0.2, step=1.5, trough="light"):
    """The same ceiling structure along the top of the Hol west wall."""
    m = M["graphite"]
    box("BeamTop", (0.2, 0.03, z1 - z0), (x_out, y + 0.185, (z0 + z1) / 2), m)
    box("BeamWeb", (0.03, 0.17, z1 - z0), (x_out, y + 0.085, (z0 + z1) / 2), m)
    box("BeamBottom", (0.2, 0.03, z1 - z0), (x_out, y + 0.015, (z0 + z1) / 2), m)
    box("Trough", (0.16, 0.05, z1 - z0), (x_out, y - 0.025, (z0 + z1) / 2), M["dsteel"])
    box("TroughLight", (0.1, 0.01, z1 - z0 - 0.04), (x_out, y - 0.052, (z0 + z1) / 2), M[trough])
    z = z0 + 0.1
    while z < z1:
        box("Cantilever", (x_out, 0.2, 0.12), (x_out / 2, y + 0.1, z), m)
        z += step


def pipe_x(x0, x1, y, z, r, mat, flange=1.6, glass=False):
    """A horizontal pipe along a north wall with flanges, wall brackets and (optionally) glass sight sections."""
    cyl("Pipe", r, x1 - x0, ((x0 + x1) / 2, y, z), M[mat], axis="x", verts=16)
    n = max(1, int((x1 - x0) / flange))
    for i in range(n + 1):
        x = x0 + 0.05 + i * (x1 - x0 - 0.1) / n
        cyl("Flange", r + 0.03, 0.05, (x, y, z), M["dsteel"], axis="x", verts=16)
        box("Bracket", (0.06, 0.05, z), (x + 0.15, y, z / 2), M["dsteel"])
        box("Clamp", (0.05, r * 2 + 0.04, 0.03), (x + 0.15, y, z), M["dsteel"])
    if glass:
        for i in range(n):
            x = x0 + 0.05 + (i + 0.5) * (x1 - x0 - 0.1) / n
            if i % 2 == 0:
                cyl("PipeGlass", r + 0.012, 0.5, (x, y, z), M["glass"], axis="x", verts=16)
                cyl("PipeFlow", r * 0.75, 0.5, (x, y, z), M["liquid_c"], axis="x", verts=12)
                for s in (-0.26, 0.26):
                    cyl("GlassRing", r + 0.03, 0.04, (x + s, y, z), M["steel"], axis="x", verts=16)


def tray_x(x0, x1, y, z0, z1, n_cables=6):
    """A ladder cable tray along a north wall, hung from the ceiling, with a cable bundle in it."""
    m = M["dsteel"]
    for z in (z0, z1):
        box("TrayRail", (x1 - x0, 0.08, 0.02), ((x0 + x1) / 2, y, z), m)
    x = x0 + 0.1
    while x < x1:
        box("Rung", (0.03, 0.015, z1 - z0), (x, y - 0.035, (z0 + z1) / 2), m)
        x += 0.3
    cols = ["cable", "cable", "cable_c", "cable", "cable_y", "cable", "cable_w", "cable"]
    for i in range(n_cables):
        zz = z0 + 0.05 + (i % 4) * (z1 - z0 - 0.1) / 3
        cyl("TrayCable", 0.022, x1 - x0 - 0.05, ((x0 + x1) / 2, y - 0.005 + 0.035 * (i // 4), zz), M[cols[i % 8]],
            axis="x", verts=8)
    for x in (x0 + 0.2, x1 - 0.2):
        for z in (z0, z1):
            cyl("Hanger", 0.008, H - y + 0.1, (x, (H + y) / 2 + 0.05, z), m, verts=6)


def poster(w, h, kind):
    """Printed poster in a slim frame, built at the origin facing +z (centre at y=0)."""
    box("PosterFrame", (w + 0.06, h + 0.06, 0.05), (0, 0, 0.0), M["graphite"], 0.008)
    bg = {"phone": "poster_y", "boost": "poster_y", "drive": "poster_k", "metric": "poster_t", "time": "poster_k",
          "zeta": "poster_t"}[kind]
    box("PosterFace", (w, h, 0.01), (0, 0, 0.026), M[bg])
    ink = M["black"] if bg == "poster_y" else M["nw"]
    z = 0.033
    if kind == "phone":
        box("PhoneArt", (w * 0.3, h * 0.46, 0.012), (w * 0.2, -h * 0.05, z), M["black"], 0.01)
        for a, b in (((-1, 1), (1, 1)), ((1, 1), (-1, -1)), ((-1, -1), (1, -1))):
            cable("Gesture", [(w * 0.2 + a[0] * w * 0.08, -h * 0.05 + a[1] * h * 0.12, z + 0.01),
                              (w * 0.2 + b[0] * w * 0.08, -h * 0.05 + b[1] * h * 0.12, z + 0.01)], 0.008, M["yellow"])
        text("PosterT1", "JEDEN GEST", (-w * 0.45, h * 0.38, z), h * 0.075, ink, align="LEFT")
        text("PosterT2", "I JESTEŚ", (-w * 0.45, h * 0.28, z), h * 0.055, ink, align="LEFT")
        text("PosterT3", "W DOMU", (-w * 0.45, h * 0.2, z), h * 0.055, ink, align="LEFT")
        box("PosterBar", (w, h * 0.1, 0.012), (0, -h * 0.45, z), M["black"])
        text("PosterT4", "Z-PHONE", (0, -h * 0.45, z + 0.008), h * 0.06, M["yellow"])
    elif kind == "drive":
        for i, (sx, sy, x, y) in enumerate(((0.7, 0.08, 0, -0.05), (0.42, 0.07, -0.02, 0.02), (0.82, 0.02, 0, -0.1))):
            box("CarArt", (w * sx, h * sy, 0.012), (w * x, h * y, z), M["yellow"] if i < 2 else M["nc"], 0.005)
        text("PosterT1", "Z-DRIVE", (0, h * 0.36, z), h * 0.1, M["yellow"])
        text("PosterT2", "WIE, DOKĄD", (0, h * 0.24, z), h * 0.05, ink)
        text("PosterT3", "JEDZIESZ", (0, h * 0.18, z), h * 0.05, ink)
        text("PosterT4", "HAMULCE W ABONAMENCIE", (0, -h * 0.4, z), h * 0.03, M["yellow"])
    elif kind == "boost":
        text("PosterT1", "NIE ŚPISZ?", (0, h * 0.34, z), h * 0.085, ink)
        box("PosterBar", (w * 0.9, h * 0.004 + 0.01, 0.012), (0, h * 0.24, z), M["black"])
        text("PosterT2", "RANO Z-BOOST", (0, h * 0.08, z), h * 0.06, ink)
        text("PosterT3", "WIECZOREM", (0, -h * 0.08, z), h * 0.06, ink)
        text("PosterT4", "Z-SEN", (0, -h * 0.2, z), h * 0.09, ink)
        z_letter("PosterZ", 0, -h * 0.38, z, h * 0.1, M["black"], depth=0.012)
    elif kind in ("metric", "time"):
        title = "ZALEŻNOŚĆ" if kind == "metric" else "CZAS EKRANOWY"
        text("PosterT1", title, (0, h * 0.38, z), h * 0.06, M["nt"] if kind == "time" else ink)
        for i in range(6):
            bh = h * (0.08 + 0.07 * i)
            box("PosterChart", (w * 0.1, bh, 0.012), (-w * 0.35 + i * w * 0.14, -h * 0.3 + bh / 2, z),
                M["nt"] if i == 5 else M["nw"] if kind == "metric" else M["yellow"])
        text("PosterT2", "97,4%" if kind == "metric" else "+38 MIN", (0, h * 0.24, z), h * 0.08, M["yellow"])
        text("PosterT3", "STREFA Z" if kind == "metric" else "NASTOLATKI / DZIEŃ", (0, -h * 0.42, z), h * 0.035, ink)
    elif kind == "zeta":
        torus("PosterEye", w * 0.28, 0.012, (0, h * 0.05, z), M["nc"], axis="z")
        cyl("PosterPupil", w * 0.1, 0.01, (0, h * 0.05, z), M["nc"], axis="z")
        text("PosterT1", "ZETA SŁUCHA", (0, h * 0.36, z), h * 0.06, ink)
        text("PosterT2", "DLA TWOJEGO DOBRA", (0, -h * 0.36, z), h * 0.035, M["nt"])


def cctv(pos, yaw):
    with Made() as g:
        box("CamPlate", (0.12, 0.16, 0.02), (0, 0, 0.01), M["graphite"])
        box("CamArm", (0.04, 0.04, 0.16), (0, 0, 0.09), M["graphite"])
        box("CamBody", (0.12, 0.11, 0.26), (0, -0.03, 0.2), M["white"], 0.02, rot=(18, 0, 0))
        box("CamHood", (0.14, 0.012, 0.3), (0, 0.03, 0.22), M["graphite"], rot=(18, 0, 0))
        cyl("CamLens", 0.035, 0.03, (0, -0.075, 0.33), M["black"], axis="z", verts=12)
        box("CamLed", (0.012, 0.012, 0.012), (0.04, -0.04, 0.33), M["ledr"])
    place(g.objs, pos, yaw)


def extinguisher(pos, yaw=0.0):
    with Made() as g:
        box("ExtBracket", (0.12, 0.1, 0.03), (0, 0.55, 0.015), M["dsteel"])
        cyl("ExtBody", 0.075, 0.46, (0, 0.38, 0.1), M["red"], verts=16, bevel=0.02)
        cyl("ExtHead", 0.03, 0.08, (0, 0.65, 0.1), M["black"], verts=10)
        box("ExtLever", (0.1, 0.015, 0.03), (0.02, 0.69, 0.1), M["steel"])
        cable("ExtHose", [(0.03, 0.66, 0.11), (0.09, 0.6, 0.15), (0.09, 0.3, 0.15)], 0.01, M["rubber"])
        box("ExtSign", (0.2, 0.2, 0.006), (0, 1.0, 0.003), M["red"])
        box("ExtSignMark", (0.06, 0.12, 0.004), (0, 1.0, 0.008), M["paint_w"])
    place(g.objs, pos, yaw)


def floor_window(x, z, w, d, glow):
    """A glass floor panel (decal height) with coolant lines underneath."""
    box("FloorWinFrame", (w + 0.08, 0.006, d + 0.08), (x, 0.003, z), M["dsteel"])
    box("FloorWinGlass", (w, 0.008, d), (x, 0.004, z), M["graphite"])
    n = max(2, int(min(w, d) / 0.18))
    for k in range(n):
        o = -0.5 + (k + 0.5) / n
        if w >= d:
            box("FloorWinLine", (w - 0.06, 0.009, 0.03), (x, 0.0045, z + o * d), M[glow])
        else:
            box("FloorWinLine", (0.03, 0.009, d - 0.06), (x + o * w, 0.0045, z), M[glow])
    for k in range(int(max(w, d) / 0.4) + 1):
        o = -0.5 + k / max(1, int(max(w, d) / 0.4))
        if w >= d:
            box("FloorWinRib", (0.02, 0.01, d), (x + o * w, 0.005, z), M["dsteel"])
        else:
            box("FloorWinRib", (w, 0.01, 0.02), (x, 0.005, z + o * d), M["dsteel"])


def cable_cover(a, b, w=0.14):
    """A flat rubber cable protector between two floor points along x or z, with yellow edges."""
    (xa, za), (xb, zb) = a, b
    along_x = abs(xb - xa) > abs(zb - za)
    ln = abs(xb - xa) if along_x else abs(zb - za)
    c = ((xa + xb) / 2, (za + zb) / 2)
    box("CableCover", (ln, 0.008, w) if along_x else (w, 0.008, ln), (c[0], 0.004, c[1]), M["rubber"])
    for s in (-1, 1):
        if along_x:
            box("CoverEdge", (ln, 0.009, 0.015), (c[0], 0.0045, c[1] + s * (w / 2 - 0.01)), M["yellow"])
        else:
            box("CoverEdge", (0.015, 0.009, ln), (c[0] + s * (w / 2 - 0.01), 0.0045, c[1]), M["yellow"])


def glass_column(pos, h=2.5, r=0.19):
    """A tall glass cylinder of liquid with steel end caps, bubbles and a feed hose."""
    x, _, z = pos
    cyl("ColBase", r + 0.04, 0.22, (x, 0.11, z), M["dsteel"], verts=20, bevel=0.01)
    cyl("ColBaseRing", r + 0.045, 0.02, (x, 0.23, z), M["nt"], verts=20)
    cyl("ColGlass", r, h - 0.44, (x, 0.22 + (h - 0.44) / 2, z), M["glass"], verts=20)
    cyl("ColLiquid", r - 0.03, h - 0.6, (x, 0.24 + (h - 0.6) / 2, z), M["liquid"], verts=16)
    cyl("ColTop", r + 0.04, 0.22, (x, h - 0.11, z), M["dsteel"], verts=20, bevel=0.01)
    for i in range(3):
        cyl("ColRib", 0.012, h - 0.44, (x + math.cos(i * 2.09 + 0.5) * (r + 0.015), 0.22 + (h - 0.44) / 2,
                                       z + math.sin(i * 2.09 + 0.5) * (r + 0.015)), M["steel"], verts=6)
    rng = random.Random(int(x * 10))
    for i in range(7):
        ball("Bubble", rng.uniform(0.012, 0.03), (x + rng.uniform(-0.08, 0.08), rng.uniform(0.5, h - 0.5),
                                                   z + rng.uniform(-0.08, 0.08)), M["nw"], 1)
    cable("ColHose", [(x, h, z), (x, h + 0.25, z - 0.05), (x + 0.2, H - 0.05, z - 0.1)], 0.03, M["pipe_t"])


# ---------- rooms ----------

def room_hol():
    x0, x1, z1 = 0.0, 9.0, 7.0
    doors = {dd["id"]: dd for dd in LAYOUT["doors"]}
    floor_slab(x0, x1, z1, "floor_hol", 2.0, (0, 0, 0))
    # floor: brass inlay lines, the Z medallion, entrance mat, a muted rug in the sofa corner, lift threshold
    for (xa, xb, z) in ((0.6, 3.6, 4.45), (0.6, 3.6, 5.55), (1.0, 4.6, 2.75)):
        box("FloorLine", (xb - xa, 0.004, 0.025), ((xa + xb) / 2, 0.002, z), M["ny"])
    for (za, zb, x) in ((0.5, 1.45, 8.75), (3.0, 6.6, 8.75)):
        box("FloorLine", (0.025, 0.004, zb - za), (x, 0.002, (za + zb) / 2), M["ny"])
    cyl("Medallion", 1.62, 0.004, (5.2, 0.002, 4.6), M["panel"], verts=64)
    torus("MedallionRing", 1.6, 0.018, (5.2, 0.004, 4.6), M["ny"], seg=64, mseg=4, squash=0.25)
    torus("MedallionRing2", 1.45, 0.008, (5.2, 0.004, 4.6), M["brass"], seg=64, mseg=4, squash=0.25)
    z_flat("FloorZ", 5.2, 4.6, 1.9, M["brass"], y=0.005)
    box("EntryMat", (1.1, 0.008, 1.5), (0.65, 0.004, 5.0), M["rubber"])
    text("EntryMatText", "WITAMY", (0.65, 0.009, 5.0), 0.16, M["yellow"], flat=True, depth=0.002)
    tbox("Rug", (2.5, 0.008, 2.0), (2.3, 0.004, 5.85), "rug", (2.5, 2.0), off=(1.05, 0, 6.85))
    tbox("LiftHazard", (0.45, 0.006, 1.4), (8.75, 0.003, 2.2), "hazard", 0.4)
    # north wall: Z feature wall, one big window, the ad wall, the security corner
    north_wall(-0.2, 9.2, "wall_hol", windows=[(2.35, 4.55)], sill=0.4, head=2.85, accent="ny")
    seams("n", 4.55, 9.0, 0.12, 2.85, 0.9, "panel", rows=(0.12, 1.05, 2.85))
    box("FeatureBack", (2.15, 2.75, 0.02), (1.2, 1.5, 0.01), M["wood"])
    for i in range(17):
        box("Fin", (0.045, 2.7, 0.06), (0.2 + i * 0.125, 1.5, 0.05), M["wood_l"], 0.005)
    for y in (0.16, 2.84):
        box("FinLight", (2.1, 0.012, 0.012), (1.2, y, 0.085), M["ny"])
    for x in (4.75, 7.35):
        light_slot("n", x, 0.2, 2.8, "ny")
    light_slot("n", 8.95, 0.2, 2.8, "nw")
    for x in (4.68, 7.32):  # glass fins with trimmed edges framing the ad screen
        box("GlassFin", (0.02, 2.2, 0.3), (x, 1.75, 0.16), M["glass"])
        box("GlassFinEdge", (0.024, 2.2, 0.012), (x, 1.75, 0.31), M["ny"])
    cctv((8.85, 2.7, 0.0), -35)
    extinguisher((7.55, 0, 0.0))
    box("SecPlate", (0.62, 0.16, 0.02), (8.35, 2.32, 0.01), M["black"])
    text("SecText", "OCHRONA", (8.35, 2.32, 0.025), 0.085, M["nw"])
    box("Reader", (0.09, 0.14, 0.03), (7.55, 1.3, 0.015), M["black"], 0.005)
    box("ReaderLed", (0.05, 0.01, 0.004), (7.55, 1.35, 0.032), M["led_c"])
    beams_x(0.0, 9.0)
    box("BrandPlate", (3.2, 0.2, 0.02), (6.0, 3.1, 0.49), M["black"])
    box("BrandEdge", (3.2, 0.012, 0.012), (6.0, 2.995, 0.5), M["brass"])
    text("Brand", "KORPORACJA Z", (6.0, 3.1, 0.505), 0.21, M["yellow"])
    # west wall: entrance with glass sidelights, planter, posters, a brass sign above the cosy corner
    dd = doors["wejscie"]
    zc, half = dd["z"], dd["width"] / 2
    side = [(3.5, 4.2)]
    gaps = [((a + b) / 2, b - a) for a, b in side] + [(zc, dd["width"])]
    wall_x("WallWest", x0 - 0.1, -0.2, z1, H, "wall_hol", gaps=gaps, lintel=2.45)
    for a, b in side:
        box("SideGlass", (0.012, 2.45, b - a), (-0.1, 1.225, (a + b) / 2), M["wglass"])
        for z in (a + 0.02, b - 0.02):
            box("SideMullion", (0.2, 2.45, 0.04), (-0.1, 1.225, z), M["graphite"])
            box("SideNeon", (0.012, 2.35, 0.012), (0.01, 1.225, z), M["ny"])
        box("SideSill", (0.24, 0.06, b - a), (-0.08, 0.03, (a + b) / 2), M["graphite"])
    for s in (-1, 1):
        box("EntryPost", (0.3, 2.5, 0.08), (x0 - 0.1, 1.25, zc + s * (half + 0.04)), M["graphite"], 0.01)
        box("EntryNeon", (0.012, 2.4, 0.012), (x0 + 0.06, 1.2, zc + s * (half + 0.01)), M["ny"])
    box("EntryHead", (0.3, 0.1, dd["width"] + 0.16), (x0 - 0.1, 2.5, zc), M["graphite"])
    box("ExitBracket", (0.3, 0.02, 0.02), (0.15, 2.72, zc), M["dsteel"])
    box("ExitBlade", (0.5, 0.15, 0.04), (0.3, 2.62, zc), M["ng"])
    text("ExitText", "WEJŚCIE", (0.3, 2.62, zc + 0.022), 0.075, M["black"])
    box("EntryReader", (0.03, 0.14, 0.09), (0.015, 1.25, 3.35), M["black"])
    box("EntryReaderLed", (0.006, 0.012, 0.06), (0.032, 1.3, 3.35), M["led_c"])
    seams("w", 0.0, 3.5, 0.12, 2.85, 0.9, "panel", rows=(0.12, 2.85), at=0.0)
    box("Planter", (0.42, 0.45, 3.0), (0.21, 0.225, 1.95), M["black"], 0.01)
    box("PlanterRim", (0.44, 0.03, 3.02), (0.22, 0.46, 1.95), M["brass"])
    box("PlanterSoil", (0.36, 0.02, 2.94), (0.21, 0.45, 1.95), M["soil"])
    box("PlanterLight", (0.012, 0.012, 2.9), (0.43, 0.42, 1.95), M["ny"])
    rng = random.Random(3)
    for i in range(10):
        z = 0.65 + i * 0.29
        if i % 3 == 1:
            ball("Shrub", 0.17, (0.21, 0.6, z), M["leaf2"], 1, scale=(1, 0.8, 1.1))
            continue
        for k in range(6):
            box("Blade", (0.025, rng.uniform(0.35, 0.7), 0.006), (0.21 + rng.uniform(-0.05, 0.05), 0.65,
                z + rng.uniform(-0.06, 0.06)), M["leaf" if k % 2 else "leaf2"],
                rot=(rng.uniform(-28, 28), rng.uniform(0, 180), rng.uniform(-28, 28)))
    for z, kind in ((1.0, "phone"), (2.7, "drive")):
        with Made() as g:
            poster(1.0, 1.4, kind)
        place(g.objs, (0.0, 1.75, z), 90)
        box("PosterLamp", (0.08, 0.03, 0.7), (0.06, 2.53, z), M["graphite"])
    light_slot("w", 1.85, 0.6, 2.8, "nw")
    text("CosySign", "zostań", (0.03, 2.05, 6.35), 0.26, M["ny"], yaw=90, depth=0.02)
    box("CosySignBack", (0.012, 0.36, 0.9), (0.006, 2.05, 6.35), M["black"])
    cctv((0.0, 2.7, 6.85), 135)
    beams_z(0.0, 7.0)
    # divider with the lift portal
    dd = doors["winda"]
    low_wall(dd["x"], 0.0, 9.0, "panel", [(dd["z"], dd["width"])], "ny")
    portal("Lift", dd["x"], dd["z"], dd["width"], 2.5, "graphite", "ny", "WINDA", "PIĘTRO NAWYKÓW")
    for i, z in enumerate((5.2, 6.1, 7.0)):  # Z-Mart rules and metrics on the Pietro face of the divider
        with Made() as g:
            poster(0.62, 0.8, ("boost", "metric", "time")[i])
        place(g.objs, (dd["x"] + 0.2, 0.6, z), 90)
    south_curb(x0, x1, z1, "panel", "ny")


def room_pietro():
    x0, x1, z1 = 9.4, 21.4, 9.0
    doors = {dd["id"]: dd for dd in LAYOUT["doors"]}
    off = (x0, 0, 0)
    floor_slab(x0, x1, z1, "floor_pietro", 2.0, off)
    # floor: walkway panels, station zones, the Z-Drive turntable, thresholds
    for x in (11.4, 12.9, 14.4, 15.9, 17.4, 18.9):
        box("FloorPanel", (0.56, 0.006, 0.56), (x, 0.003, 4.95), M["dsteel"])
        box("FloorGlow", (0.5, 0.008, 0.5), (x, 0.004, 4.95), M["nt"])
    for (xa, xb, za, zb) in ((10.1, 13.3, 0.35, 3.1), (11.0, 17.6, 5.55, 8.7)):
        for (a, b, c, d) in ((xa, xb, za, za), (xa, xb, zb, zb), (xa, xa, za, zb), (xb, xb, za, zb)):
            box("ZoneLine", (max(b - a, 0.03), 0.004, max(d - c, 0.03)), ((a + b) / 2, 0.003, (c + d) / 2), M["ochre"])
    text("ZoneText", "Z-PHONE", (11.0, 0.006, 3.35), 0.22, M["yellow"], flat=True, depth=0.003)
    text("ZoneText", "Z-MART", (11.9, 0.006, 5.3), 0.22, M["yellow"], flat=True, depth=0.003)
    cyl("Turntable", 1.75, 0.006, (18.6, 0.003, 2.2), M["dsteel"], verts=64)
    torus("TurntableRing", 1.72, 0.016, (18.6, 0.005, 2.2), M["nc"], seg=64, mseg=4, squash=0.25)
    torus("TurntableRing2", 1.5, 0.006, (18.6, 0.006, 2.2), M["ochre"], seg=64, mseg=4, squash=0.25)
    text("ZoneText", "Z-DRIVE", (18.6, 0.006, 4.4), 0.3, M["yellow"], flat=True, depth=0.003)
    tbox("LiftHazard", (0.45, 0.006, 1.4), (9.65, 0.003, 2.2), "hazard", 0.4)
    dd = doors["zejscie"]
    tbox("StairHazard", (0.5, 0.006, 1.5), (21.15, 0.003, dd["z"]), "hazard", 0.4)
    for z in (4.2, 8.2):
        for x in (10.1, 20.7):
            torus("ScanPad", 0.42, 0.012, (x, 0.004, z), M["nt"], seg=32, mseg=4, squash=0.25)
    for x, z, w, d in ((14.0, 1.25, 1.2, 0.5), (16.4, 1.25, 1.2, 0.5), (19.2, 6.4, 0.6, 1.6), (13.6, 8.3, 1.6, 0.5)):
        floor_window(x, z, w, d, "nt")
    cable_cover((11.0, 0.3), (11.0, 1.9))
    cable_cover((15.6, 0.45), (15.6, 3.0))
    cable_cover((14.3, 7.0), (14.75, 7.0))
    # north wall: glass columns, phone station, lab cabinets under the counter screen, windows behind the car
    north_wall(9.2, 21.6, "wall_pietro", windows=[(17.65, 19.25), (20.55, 21.35)], sill=0.45, head=2.65, accent="nt",
               off=off)
    seams("n", 9.4, 17.65, 0.12, 2.65, 1.0, "panel_t", rows=(0.12, 2.65))
    box("WallStripe", (8.25, 0.05, 0.02), (13.525, 1.0, 0.012), M["nt"])
    for x in (9.8, 13.41, 17.4):
        glass_column((x, 0, 0.22), r=0.17)
    for i in range(6):  # lab cabinets with a worktop and clutter
        x = 13.9 + i * 0.6
        box("Cabinet", (0.58, 0.82, 0.42), (x, 0.47, 0.22), M["matte"], 0.008)
        box("CabHandle", (0.3, 0.02, 0.02), (x, 0.8, 0.44), M["steel"])
        box("CabSeam", (0.01, 0.8, 0.005), (x + 0.29, 0.47, 0.43), M["black"])
    box("CabKick", (3.6, 0.06, 0.4), (15.4, 0.03, 0.2), M["black"])
    box("CabKickLight", (3.56, 0.01, 0.01), (15.4, 0.062, 0.43), M["nt"])
    box("Worktop", (3.62, 0.04, 0.46), (15.4, 0.9, 0.23), M["graphite"], 0.006)
    box("Sink", (0.4, 0.012, 0.3), (14.2, 0.921, 0.22), M["steel"])
    cable("Faucet", [(14.2, 0.92, 0.05), (14.2, 1.15, 0.05), (14.2, 1.2, 0.15), (14.2, 1.08, 0.22)], 0.012, M["chrome"])
    rng = random.Random(5)
    for i in range(9):  # glassware
        x = 14.6 + i * 0.28 + rng.uniform(-0.05, 0.05)
        r, hh = rng.uniform(0.03, 0.06), rng.uniform(0.1, 0.24)
        cyl("Flask", r, hh, (x, 0.92 + hh / 2, 0.2 + rng.uniform(-0.08, 0.08)), M["glass"], verts=12)
        cyl("FlaskLiquid", r * 0.85, hh * 0.5, (x, 0.92 + hh * 0.25, 0.2), M["liquid" if i % 3 else "ochre"], verts=10)
    box("Centrifuge", (0.4, 0.22, 0.34), (16.6, 1.03, 0.2), M["white"], 0.03)
    cyl("CentrifugeLid", 0.12, 0.02, (16.6, 1.15, 0.2), M["dglass"], verts=16)
    box("CentLed", (0.08, 0.02, 0.005), (16.6, 1.0, 0.372), M["led_c"])
    for x in (13.9, 15.1, 16.3):  # backsplash outlets and gas taps
        box("Outlet", (0.12, 0.08, 0.02), (x, 1.15, 0.01), M["graphite"])
        box("OutletLed", (0.02, 0.01, 0.004), (x + 0.04, 1.17, 0.022), M["led_c"])
    # overhead: big duct, coolant pipe, conduit, pendant rings and light housings
    cyl("Duct", 0.16, 12.0, (15.4, 3.02, 0.2), M["dsteel"], axis="x", verts=20)
    for i in range(9):
        x = 9.8 + i * 1.45
        cyl("DuctRing", 0.175, 0.05, (x, 3.02, 0.2), M["graphite"], axis="x", verts=20)
        box("DuctStrap", (0.04, 0.2, 0.02), (x, 3.1, 0.37), M["graphite"])
        if i % 2 == 0 and x < 20.5:
            box("Diffuser", (0.3, 0.04, 0.2), (x + 0.7, 2.85, 0.2), M["graphite"])
            box("DiffuserGlow", (0.24, 0.01, 0.14), (x + 0.7, 2.828, 0.2), M["nt"])
    pipe_x(9.4, 21.4, 2.8, 0.1, 0.06, "pipe_t", flange=2.0)
    cyl("Conduit", 0.025, 12.0, (15.4, 2.72, 0.04), M["dsteel"], axis="x", verts=8)
    for x in (11.7, 15.2):  # station pendant rings
        torus("Pendant", 0.55, 0.03, (x, 2.95, 2.3 if x < 12 else 3.4), M["nw"], seg=40)
        torus("PendantHousing", 0.55, 0.045, (x, 2.99, 2.3 if x < 12 else 3.4), M["graphite"], seg=40, mseg=6)
        for k in range(3):
            a = k * 2.09
            cyl("PendantWire", 0.005, 0.5, (x + math.cos(a) * 0.55, 3.25, (2.3 if x < 12 else 3.4) + math.sin(a) * 0.55),
                M["graphite"], verts=4)
    for x0_, x1_, z in ((17.8, 19.4, 1.3), (17.8, 19.4, 3.1)):  # showroom light frame over the car
        box("CarLight", (x1_ - x0_, 0.04, 0.06), ((x0_ + x1_) / 2, 3.05, z), M["nw"])
        box("CarLightHousing", (x1_ - x0_, 0.05, 0.1), ((x0_ + x1_) / 2, 3.09, z), M["graphite"])
    for x, z in ((12.6, 6.2), (15.4, 6.6)):
        box("MartLight", (2.2 if x < 14 else 1.4, 0.04, 0.08), (x, 2.9, z), M["ny"])
        box("MartLightHousing", (2.2 if x < 14 else 1.4, 0.06, 0.14), (x, 2.95, z), M["graphite"])
    extinguisher((20.25, 0, 0.0))
    cctv((21.3, 2.6, 0.0), -40)
    # divider to the core with the stair portal
    low_wall(dd["x"], 0.0, 9.0, "panel_t", [(dd["z"], dd["width"])], "nt")
    portal("Stair", dd["x"], dd["z"], dd["width"], 2.5, "dsteel", "nc", "POZIOM -3", "POZIOM -3")
    pipe_side = dd["x"] + 0.28  # the Jadro face of the divider: low coolant pipes
    for za, zb in ((0.1, dd["z"] - dd["width"] / 2 - 0.2), (dd["z"] + dd["width"] / 2 + 0.2, 8.9)):
        cyl("LowPipe", 0.05, zb - za, (pipe_side, 0.82, (za + zb) / 2), M["pipe"], axis="z", verts=12)
        cyl("LowPipe", 0.035, zb - za, (pipe_side, 0.62, (za + zb) / 2), M["pipe_t"], axis="z", verts=10)
        for z in np.arange(za + 0.3, zb, 1.2):
            box("LowClamp", (0.09, 0.32, 0.04), (pipe_side - 0.03, 0.72, z), M["dsteel"])
    south_curb(x0, x1, z1, "panel_t", "nt")


def room_jadro():
    x0, x1, z1 = 21.8, 30.8, 9.0
    doors = {dd["id"]: dd for dd in LAYOUT["doors"]}
    off = (x0, 0, 0)
    floor_slab(x0, x1, z1, "floor_jadro", 0.5, off)
    cx, cz = 26.3, 3.6
    # floor: diamond-plate ring around the core with hazard tape, walkways with trim strips, trench covers
    cyl("CoreDeck", 2.0, 0.006, (cx, 0.003, cz), M["black"], verts=48)
    uvw(cyl("CoreDiamond", 1.95, 0.008, (cx, 0.004, cz), M["diamond"], verts=48), 0.5, off)
    for i in range(40):
        a = i * math.tau / 40
        box("HazRing", (0.3, 0.003, 0.2), (cx + math.cos(a) * 1.15, 0.0085, cz + math.sin(a) * 1.15),
            M["yellow"] if i % 2 else M["black"], rot=(0, 90 - math.degrees(a), 0))
    torus("CoreGlow", 1.96, 0.014, (cx, 0.006, cz), M["nc"], seg=64, mseg=4, squash=0.25)
    for (xa, xb, za, zb) in ((21.8, 24.4, 6.0, 7.2), (28.1, 30.8, 4.0, 5.2)):
        uvw(box("Walkway", (xb - xa, 0.008, zb - za), ((xa + xb) / 2, 0.004, (za + zb) / 2), M["diamond"]), 0.5, off)
        for z in (za + 0.03, zb - 0.03):
            box("WalkGlow", (xb - xa, 0.01, 0.02), ((xa + xb) / 2, 0.005, z), M["nc"])
    uvw(box("Trench", (1.4, 0.008, 0.36), (28.95, 0.004, 2.8), M["diamond"]), 0.5, off)
    for z in (3.4, 4.2):
        uvw(box("Trench", (1.9, 0.006, 0.36), (24.1, 0.003, z), M["diamond"]), 0.5, off)  # 1 mm under the core deck it runs into
        for x in (23.3, 24.1, 24.9):
            cyl("TrenchBolt", 0.012, 0.004, (x, 0.008, z + 0.15), M["steel"], verts=6)
    dd = doors["wyjscie"]
    tbox("ExitHazard", (0.5, 0.006, 1.5), (30.5, 0.003, dd["z"]), "hazard", 0.4)
    dz = doors["zejscie"]
    tbox("StairHazard", (0.4, 0.006, 1.5), (22.05, 0.003, dz["z"]), "hazard", 0.4)
    text("LevelText", "-3", (23.0, 0.005, 5.3), 0.6, M["yellow"], flat=True, depth=0.003)
    for x, z, w, d in ((23.6, 2.15, 1.6, 0.55), (29.0, 1.6, 1.4, 0.45), (26.0, 8.45, 1.6, 0.45), (22.5, 8.75, 0.9, 0.3)):
        floor_window(x, z, w, d, "nc")
    for k, p in enumerate(p for p in LAYOUT["props"] if p["model"] == "rack" and p["yaw"] == 0):
        text("RowText", ("A", "B")[k // 3] + str(k % 3 + 1), (p["x"], 0.005, 1.45), 0.16, M["yellow"], flat=True, depth=0.002)
    for k in range(3):
        for s in (-1, 1):
            box("ExitArrow", (0.28, 0.009, 0.05), (29.2 + k * 0.32, 0.0045, dd["z"] + s * 0.09), M["ng"],
                rot=(0, s * 35, 0))
    for x in np.arange(22.6, 30.2, 1.2):
        box("InsetLight", (0.5, 0.009, 0.06), (x, 0.0045, 1.38), M["nc"])
    for x, z in ((25.0, 6.9), (28.9, 3.9)):
        box("Drain", (0.5, 0.006, 0.5), (x, 0.003, z), M["dsteel"])
        for k in range(6):
            box("DrainSlot", (0.42, 0.004, 0.025), (x, 0.005, z - 0.19 + k * 0.075), M["black"])
    # north wall: concrete, steel columns, coolers flanking the ZETA eye, glass coolant tubes, pipes, trays, cables
    north_wall(21.6, 31.0, "wall_jadro", tile=2.4, off=off, t=0.3)
    for x in (21.95, 30.65):
        box("ColumnWeb", (0.04, H, 0.22), (x, H / 2, 0.11), M["dsteel"])
        for s in (-1, 1):
            box("ColumnFlange", (0.22, H, 0.03), (x, H / 2, 0.11 + s * 0.11), M["dsteel"])
        tbox("ColumnHaz", (0.24, 0.6, 0.26), (x, 0.3, 0.11), "hazard", 0.4)
    box("TopBeam", (9.4, 0.25, 0.3), (26.3, H - 0.125, 0.15), M["dsteel"])
    box("TopBeamLip", (9.4, 0.03, 0.34), (26.3, H - 0.265, 0.17), M["graphite"])
    for xa in (24.72, 27.06):  # cooling units
        xc = xa + 0.41
        box("Cooler", (0.82, 1.9, 0.42), (xc, 0.95, 0.21), M["dsteel"], 0.012)
        box("CoolerFace", (0.74, 1.3, 0.01), (xc, 1.05, 0.425), M["graphite"])
        for y in (0.75, 1.35):
            torus("Fan", 0.26, 0.02, (xc, y, 0.43), M["steel"], axis="z", seg=24)
            cyl("FanHub", 0.06, 0.02, (xc, y, 0.43), M["black"], axis="z", verts=12)
            for k in range(5):
                box("FanBlade", (0.04, 0.22, 0.006), (xc, y, 0.432), M["black"], rot=(0, 0, k * 72))
        box("CoolerScreen", (0.3, 0.1, 0.006), (xc, 1.8, 0.425), M["display"])
        for k in range(3):
            box("CoolerLed", (0.02, 0.02, 0.006), (xc - 0.3 + k * 0.05, 1.8, 0.427), M["ledr" if k == 0 else "led_c"])
        tbox("CoolerHaz", (0.84, 0.1, 0.44), (xc, 0.1, 0.21), "hazard", 0.4)
        text("CoolerText", "CHŁODZENIE", (xc, 0.3, 0.428), 0.06, M["nw"])
        box("Beacon", (0.12, 0.05, 0.12), (xc + 0.25, 1.925, 0.2), M["graphite"])
        cyl("BeaconDome", 0.05, 0.08, (xc + 0.25, 1.99, 0.2), M["red"], verts=12)
        cable("CoolerPipe", [(xc - 0.2, 1.9, 0.2), (xc - 0.2, 2.4, 0.2), (xc - 0.2, 2.62, 0.18), (xc - 0.2, 2.9, 0.18)],
              0.05, M["pipe"], res=6, sides=2)
    for x in (25.66, 26.94):  # glass coolant tubes either side of the eye
        cyl("TubeFoot", 0.11, 0.2, (x, 0.1, 0.16), M["dsteel"], verts=16)
        cyl("TubeGlass", 0.085, 2.3, (x, 1.35, 0.16), M["glass"], verts=16)
        cyl("TubeFlow", 0.065, 2.2, (x, 1.35, 0.16), M["liquid_c"], verts=12)
        cyl("TubeHead", 0.11, 0.2, (x, 2.6, 0.16), M["dsteel"], verts=16)
        for y in (0.6, 1.35, 2.1):
            cyl("TubeBand", 0.095, 0.04, (x, y, 0.16), M["steel"], verts=16)
        cable("TubeFeed", [(x, 2.7, 0.16), (x, 2.82, 0.16), (x, 2.85, 0.12)], 0.04, M["pipe"], res=4, sides=2)
    torus("EyeHousing", 0.48, 0.06, (cx, 2.0, 0.07), M["dsteel"], axis="z", seg=40)
    cyl("EyeBack", 0.46, 0.03, (cx, 2.0, 0.03), M["black"], axis="z", verts=40)
    torus("EyeIris", 0.34, 0.016, (cx, 2.0, 0.06), M["nc"], axis="z", seg=40, mseg=6)
    torus("EyeIris2", 0.21, 0.022, (cx, 2.0, 0.065), M["nc"], axis="z", seg=32, mseg=6)
    cyl("EyePupil", 0.09, 0.02, (cx, 2.0, 0.07), M["led_c"], axis="z", verts=24)
    for k in range(4):
        a = math.radians(45 + k * 90)
        box("EyeBracket", (0.2, 0.05, 0.05), (cx + math.cos(a) * 0.58, 2.0 + math.sin(a) * 0.58, 0.05), M["dsteel"],
            rot=(0, 0, 45 + k * 90))
    text("EyeText", "/// ZETA.CORE", (cx, 1.36, 0.02), 0.075, M["nc"])
    pipe_x(21.8, 30.8, 2.9, 0.18, 0.12, "pipe", flange=1.5, glass=True)
    pipe_x(21.8, 30.8, 3.06, 0.12, 0.07, "pipe_t", flange=1.8)
    cable("PipeDrop", [(22.25, 2.9, 0.18), (22.25, 2.5, 0.2), (22.2, 0.3, 0.22), (22.2, 0.0, 0.22)], 0.07, M["pipe"],
          res=6, sides=2)
    for x in (25.12, 27.47):
        cyl("Valve", 0.03, 0.12, (x, 2.9, 0.34), M["dsteel"], axis="z", verts=8)
        torus("ValveWheel", 0.1, 0.012, (x, 2.9, 0.41), M["yellow"], axis="z", seg=16, mseg=6)
    racks = [p for p in LAYOUT["props"] if p["model"] == "rack" and p["yaw"] == 0]
    for xa, xb in ((22.25, 24.75), (27.85, 30.35)):
        tray_x(xa, xb, 2.45, 0.35, 1.15, n_cables=8)
    for p in racks:  # cable bundles dropping from the trays into the racks
        for k in range(5):
            dx = -0.2 + k * 0.1
            top = (p["x"] + dx * 0.8, 2.42, 0.55 + k * 0.08)
            cable("Drop", droop(top, (p["x"] + dx, 2.2, p["z"] - 0.2 + k * 0.05), 0.12 + 0.04 * k),
                  0.016, M[("cable", "cable_c", "cable", "cable_y", "cable")[k]])
    for x in (24.8, 27.8):  # loose cables hanging between the trays
        for k in range(3):
            cable("Hang", droop((x, 2.4, 0.5 + k * 0.2), (x + 0.6 + k * 0.2, 2.4, 0.6 + k * 0.2), 0.35 + k * 0.12, 8),
                  0.015, M[("cable", "cable_c", "cable_w")[k]])
    for x in (25.28, 27.62):
        box("WarnPlate", (0.22, 0.22, 0.01), (x, 2.3, 0.005), M["yellow"])
        text("WarnText", "!", (x, 2.3, 0.012), 0.16, M["black"])
    for x in np.arange(22.4, 30.4, 0.8):
        box("WallLed", (0.02, 0.02, 0.01), (x, 2.3, 0.006), M["ledr"])
    # divider face is built with the Pietro room; east wall: low with the exit
    wall_x("WallEast", dd["x"] + 0.1, -0.2, z1 + 0.2, LOW, "wall_jadro", gaps=[(dd["z"], dd["width"])], tile=2.4)
    for za, zb in ((-0.2, dd["z"] - dd["width"] / 2), (dd["z"] + dd["width"] / 2, z1 + 0.2)):
        box("EastCap", (0.24, 0.05, zb - za), (dd["x"] + 0.1, LOW + 0.025, (za + zb) / 2), M["graphite"])
        box("EastNeon", (0.03, 0.012, zb - za - 0.04), (dd["x"] + 0.1, LOW + 0.056, (za + zb) / 2), M["nc"])
    portal("Exit", dd["x"] + 0.1, dd["z"], dd["width"], 2.4, "dsteel", "nc", "WYJŚCIE", "WYJŚCIE", "ng")
    box("ExitSign", (0.06, 0.25, 0.6), (dd["x"] + 0.1, 2.95, dd["z"]), M["ng"])
    south_curb(x0, x1, z1, "dsteel", "nc")


ROOMS = {"hol": room_hol, "pietro": room_pietro, "jadro": room_jadro}


# ---------- props (origin at the floor, centre of the footprint, facing +z) ----------

def desk():
    """Reception counter: beige body, light wood top, curved ends, fluted fascia, brass edges, clutter."""
    box("Body", (2.1, 0.92, 0.82), (0, 0.47, -0.02), M["beige"], 0.01)
    for s in (-1, 1):
        cyl("End", 0.41, 0.92, (s * 1.05, 0.47, -0.02), M["beige"], verts=32)
        cyl("TopEnd", 0.45, 0.05, (s * 1.05, 0.984, 0), M["wood_l"], verts=32)  # 1 mm under Top, never coplanar
        for k in range(9):  # trim strips following the curved ends
            phi = math.radians(-90 + k * 22.5) if s > 0 else math.radians(90 + k * 22.5)
            box("EndNeon", (0.012, 0.012, 0.165), (s * 1.05 + math.cos(phi) * 0.415, 0.94, -0.02 + math.sin(phi) * 0.415),
                M["ny"], rot=(0, -math.degrees(phi), 0))
    box("Kick", (2.9, 0.08, 0.7), (0, 0.04, -0.04), M["graphite"])
    box("KickLight", (2.8, 0.012, 0.012), (0, 0.075, 0.33), M["ny"])
    for i in range(19):
        box("Flute", (0.05, 0.74, 0.03), (-0.99 + i * 0.11, 0.5, 0.39), M["wood_l"], 0.01)
    box("FrontNeon", (2.1, 0.012, 0.012), (0, 0.93, 0.405), M["ny"])
    box("EmblemPlate", (0.6, 0.6, 0.02), (0, 0.52, 0.41), M["black"], 0.008)
    z_letter("Emblem", 0, 0.52, 0.424, 0.4, M["yellow"], depth=0.015)
    box("Top", (2.1, 0.05, 0.9), (0, 0.985, 0), M["wood_l"])
    box("TopNeon", (2.1, 0.012, 0.012), (0, 0.955, 0.444), M["ny"])
    box("Keyboard", (0.44, 0.018, 0.15), (-0.8, 1.019, 0.12), M["graphite"], 0.004)
    box("Keys", (0.4, 0.006, 0.11), (-0.8, 1.03, 0.12), M["dsteel"])
    box("Mouse", (0.06, 0.02, 0.1), (-0.48, 1.02, 0.13), M["graphite"], 0.008)
    cyl("Cup", 0.04, 0.1, (0.15, 1.06, -0.12), M["white"], verts=16)
    cyl("CupBand", 0.041, 0.02, (0.15, 1.07, -0.12), M["ochre"], verts=16)
    for i, (x, z, a) in enumerate(((-0.2, 0.0, 8), (-0.18, 0.02, -5), (1.0, -0.15, 20))):
        box("Paper", (0.21, 0.003, 0.297), (x, 1.012 + i * 0.003, z), M["white"], rot=(0, a, 0))
    cyl("PenCup", 0.035, 0.1, (0.45, 1.06, -0.25), M["graphite"], verts=12)
    for i in range(3):
        cyl("Pen", 0.005, 0.14, (0.44 + 0.01 * i, 1.1, -0.26 + 0.01 * i), M["ochre" if i == 0 else "black"], verts=6)
    cyl("BellBase", 0.05, 0.015, (1.15, 1.017, 0.25), M["graphite"], verts=16)
    ball("Bell", 0.045, (1.15, 1.03, 0.25), M["brass"], scale=(1, 0.8, 1))
    # a brass desk lamp for the receptionist
    cyl("LampFoot", 0.07, 0.02, (-1.25, 1.02, -0.25), M["brass"], verts=16)
    cable("LampArm", [(-1.25, 1.02, -0.25), (-1.25, 1.35, -0.25), (-1.15, 1.42, -0.15)], 0.01, M["brass"], res=4)
    cyl("LampShade", 0.09, 0.08, (-1.12, 1.4, -0.12), M["brass"], verts=16, r2=0.04)
    cyl("LampGlow", 0.08, 0.005, (-1.12, 1.358, -0.12), M["warm"], verts=16)
    for s in (-1, 1):
        box("Drawer", (0.6, 0.25, 0.01), (s * 0.5, 0.75, -0.435), M["graphite"])
        box("DrawerHandle", (0.2, 0.02, 0.02), (s * 0.5, 0.8, -0.445), M["steel"])


def monitor():
    box("BasePlate", (0.24, 0.012, 0.17), (0, 0.006, -0.04), M["graphite"], 0.004)
    box("Neck", (0.05, 0.3, 0.03), (0, 0.17, -0.07), M["dsteel"], 0.006, rot=(8, 0, 0))
    box("Housing", (0.48, 0.28, 0.04), (0, 0.38, -0.035), M["graphite"], 0.01)
    box("Frame", (0.62, 0.4, 0.035), (0, 0.38, 0), M["black"], 0.006)
    box("Screen", (0.57, 0.35, 0.005), (0, 0.38, 0.022), M["screen"])
    box("Power", (0.012, 0.006, 0.004), (0.27, 0.19, 0.019), M["led_c"])
    cable("MonCable", [(0.05, 0.3, -0.06), (0.06, 0.12, -0.09), (0.12, 0.0, -0.12)], 0.006, M["rubber"])


def phone_dock():
    box("Dock", (0.15, 0.03, 0.13), (0, 0.015, 0), M["graphite"], 0.008)
    box("Cradle", (0.11, 0.09, 0.02), (0, 0.07, -0.01), M["graphite"], 0.005, rot=(-12, 0, 0))
    box("DockLed", (0.06, 0.004, 0.004), (0, 0.02, 0.066), M["led_c"])
    box("Phone", (0.09, 0.17, 0.012), (0, 0.13, 0.02), M["black"], 0.004, rot=(-12, 0, 0))
    box("Screen", (0.08, 0.15, 0.002), (0, 0.13, 0.028), M["screen"], rot=(-12, 0, 0))
    cable("DockCable", [(0, 0.01, -0.06), (0.02, 0.005, -0.12), (0.08, 0.005, -0.16)], 0.004, M["rubber"])


def wall_screen(w=2.4, h=1.4, halo="ny"):
    box("Halo", (w + 0.05, h + 0.05, 0.005), (0, 0, -0.045), M[halo])
    box("Frame", (w, h, 0.07), (0, 0, -0.005), M["black"], 0.012)
    box("Bezel", (w - 0.06, h - 0.06, 0.01), (0, 0, 0.033), M["graphite"])
    box("Screen", (w - 0.1, h - 0.1, 0.005), (0, 0, 0.043), M["screen"])
    box("Trim", (w * 0.96, 0.025, 0.02), (0, -h / 2 - 0.035, 0.02), M["ny"])
    box("Speaker", (w * 0.5, 0.05, 0.05), (0, -h / 2 - 0.09, 0.0), M["graphite"], 0.01)
    for i in range(12):
        box("Grille", (0.012, 0.03, 0.004), (-w * 0.22 + i * w * 0.04, -h / 2 - 0.09, 0.026), M["dsteel"])
    z_letter("Logo", w / 2 - 0.12, -h / 2 + 0.025, 0.04, 0.035, M["yellow"], depth=0.004)
    for x in (-w / 3, w / 3):
        box("Mount", (0.3, 0.4, 0.02), (x, 0, -0.11), M["dsteel"])
        box("Arm", (0.06, 0.06, 0.07), (x, 0, -0.075), M["dsteel"])
    cyl("Conduit", 0.016, 0.4, (w / 2 - 0.2, h / 2 + 0.2, -0.09), M["dsteel"], verts=8)


def z_logo():
    box("Halo", (1.16, 1.36, 0.01), (0, 0, -0.03), M["ny"])
    box("Plate", (1.1, 1.3, 0.04), (0, 0, -0.005), M["black"], 0.012)
    for x, y, sx, sy in ((0, 0.6, 0.98, 0.02), (0, -0.6, 0.98, 0.02), (-0.5, 0, 0.02, 1.18), (0.5, 0, 0.02, 1.18)):
        box("Rim", (sx, sy, 0.01), (x, y, 0.018), M["brass"])
    z_letter("Z", 0, 0.08, 0.04, 0.8, M["yellow"])
    text("Name", "KORPORACJA", (0, -0.44, 0.02), 0.085, M["nw"])
    for x in (-0.47, 0.47):
        for y in (-0.57, 0.57):
            cyl("Bolt", 0.015, 0.02, (x, y, 0.02), M["steel"], axis="z", verts=8)


def locker():
    box("Plinth", (0.86, 0.08, 0.54), (0, 0.04, -0.03), M["graphite"])
    box("Body", (0.88, 1.88, 0.55), (0, 1.02, -0.025), M["dsteel"], 0.012)
    box("Crown", (0.9, 0.05, 0.56), (0, 1.975, -0.02), M["graphite"], 0.008)
    box("Seam", (0.008, 1.84, 0.01), (0, 1.02, 0.25), M["black"])
    for s in (-1, 1):
        x = s * 0.22
        for y, hh in ((1.38, 0.7), (0.6, 0.62)):
            for dy in (-hh / 2, hh / 2):
                box("Panel", (0.32, 0.012, 0.006), (x, y + dy, 0.252), M["graphite"])
            for dx in (-0.16, 0.16):
                box("Panel", (0.012, hh, 0.006), (x + dx, y, 0.252), M["graphite"])
        for i in range(5):
            box("Vent", (0.28, 0.018, 0.01), (x, 1.88 - i * 0.04, 0.252), M["black"])
        box("Handle", (0.025, 0.22, 0.03), (s * 0.05, 1.0, 0.262), M["steel"], 0.006)
        for y in (0.4, 1.7):
            box("Hinge", (0.02, 0.08, 0.02), (s * 0.43, y, 0.24), M["steel"])
    tbox("HazardBand", (0.86, 0.1, 0.01), (0, 0.14, 0.252), "hazard", 0.4)
    box("Keypad", (0.13, 0.18, 0.02), (0.3, 1.15, 0.26), M["black"], 0.004)
    for r in range(4):
        for c in range(3):
            box("Key", (0.026, 0.022, 0.006), (0.27 + c * 0.03, 1.18 - r * 0.03, 0.272), M["steel"])
    box("KeypadLight", (0.09, 0.015, 0.005), (0.3, 1.225, 0.272), M["led_y"])
    box("Sticker", (0.12, 0.06, 0.002), (0.3, 0.98, 0.251), M["yellow"])
    text("StickerText", "KOD", (0.3, 0.98, 0.2525), 0.03, M["black"], depth=0.001)
    text("Label", "OCHRONA", (-0.22, 1.62, 0.255), 0.07, M["yellow"], depth=0.004)


def sofa():
    """Two-seat leather sofa with a side table and a lamp, all inside the 2.2 x 0.9 footprint."""
    sx = -0.2  # sofa centre; the side table takes the right end
    for x in (sx - 0.7, sx + 0.7):
        for z in (-0.33, 0.33):
            cyl("Leg", 0.018, 0.1, (x, 0.05, z), M["brass"], verts=8)
        cyl("Runner", 0.016, 0.7, (x, 0.012, 0), M["brass"], axis="z", verts=8)
    box("Base", (1.66, 0.2, 0.82), (sx, 0.2, -0.01), M["fabric_d"], 0.03)
    box("BackFrame", (1.66, 0.5, 0.16), (sx, 0.55, -0.36), M["leather_w"], 0.04)
    for x in (sx - 0.82, sx + 0.82):
        box("Arm", (0.12, 0.42, 0.84), (x, 0.36, -0.01), M["leather_w"], 0.05)
    for x in (sx - 0.37, sx + 0.37):
        box("Seat", (0.72, 0.15, 0.62), (x, 0.375, 0.08), M["leather_w"], 0.05)
        box("BackCushion", (0.72, 0.42, 0.15), (x, 0.68, -0.25), M["leather_w"], 0.06, rot=(-10, 0, 0))
        for bx in (-0.2, 0, 0.2):
            ball("Tuft", 0.014, (x + bx, 0.7, -0.17), M["fabric_d"], 1)
    box("Pillow", (0.4, 0.36, 0.12), (sx - 0.45, 0.6, -0.1), M["ochre"], 0.05, rot=(-15, 0, 12))
    box("Throw", (0.5, 0.02, 0.62), (sx + 0.35, 0.46, 0.08), M["fabric"], 0.008, rot=(0, 0, -3))
    tx = 0.88
    cyl("TableTop", 0.2, 0.03, (tx, 0.55, 0.05), M["wood"], verts=24, bevel=0.005)
    cyl("TableStem", 0.02, 0.53, (tx, 0.27, 0.05), M["brass"], verts=8)
    cyl("TableFoot", 0.14, 0.015, (tx, 0.008, 0.05), M["brass"], verts=20)
    cyl("LampBase", 0.06, 0.04, (tx + 0.05, 0.585, -0.05), M["brass"], verts=16)
    cyl("LampStem", 0.008, 0.3, (tx + 0.05, 0.75, -0.05), M["brass"], verts=6)
    cyl("LampShade", 0.13, 0.17, (tx + 0.05, 0.95, -0.05), M["warm"], verts=20, r2=0.09)
    for i, c in enumerate(("leather_w", "graphite", "ochre")):
        box("Book", (0.16, 0.03, 0.22), (tx - 0.06, 0.58 + i * 0.03, 0.12), M[c], 0.004, rot=(0, 10 + i * 12, 0))
    cyl("Mug", 0.035, 0.08, (tx + 0.1, 0.605, 0.16), M["white"], verts=12)


def plant():
    rng = random.Random(7)
    cyl("Pot", 0.25, 0.58, (0, 0.29, 0), M["black"], verts=24, bevel=0.01)
    cyl("PotFoot", 0.22, 0.04, (0, 0.02, 0), M["graphite"], verts=24)
    cyl("PotBand", 0.252, 0.03, (0, 0.47, 0), M["brass"], verts=24)
    cyl("PotGlow", 0.253, 0.008, (0, 0.06, 0), M["ny"], verts=24)
    cyl("Soil", 0.235, 0.02, (0, 0.57, 0), M["soil"], verts=24)
    for i in range(10):
        ball("Pebble", 0.025, (rng.uniform(-0.17, 0.17), 0.585, rng.uniform(-0.17, 0.17)), M["matte"], 1, scale=(1, 0.6, 1))
    box("HiddenBadge", (0.054, 0.086, 0.004), (0.12, 0.62, 0.1), M["white"], rot=(-20, 30, 0))
    for i in range(12):
        a = i * 2.4
        tilt = math.radians(rng.uniform(15, 50))
        length = rng.uniform(0.45, 1.0)
        hr = min(0.16, math.sin(tilt) * length)
        tip = (math.cos(a) * hr, 0.58 + math.cos(tilt) * length, math.sin(a) * hr)
        cable("Stem", [(0, 0.58, 0), (tip[0] * 0.4, (0.58 + tip[1]) / 2 + 0.05, tip[2] * 0.4), tip], 0.008, M["leaf"], res=4)
        ball("Leaf", 1.0, tip, M["leaf2" if i % 2 else "leaf"], 1, scale=(0.1, 0.014, 0.14),
             rot=(rng.uniform(20, 45), 90 - math.degrees(a), 0))


def turnstile():
    for s in (-1, 1):
        x = s * 0.42
        box("Cabinet", (0.18, 0.96, 1.3), (x, 0.48, 0), M["steel"], 0.02)
        box("CabTop", (0.2, 0.04, 1.32), (x, 0.98, 0), M["black"], 0.01)
        box("CabLight", (0.012, 0.012, 1.26), (x - s * 0.095, 0.97, 0), M["ny"])
        box("Inner", (0.006, 0.5, 1.1), (x - s * 0.092, 0.6, 0), M["black"])
        box("OuterBand", (0.004, 0.05, 1.2), (x + s * 0.092, 0.78, 0), M["ochre"])
        for e in (-1, 1):
            box("EndCap", (0.18, 0.9, 0.012), (x, 0.47, e * 0.656), M["graphite"])
            box("Arrow", (0.05, 0.05, 0.004), (x, 0.82, e * 0.664), M["led_c"] if e > 0 else M["ledr"], rot=(0, 0, 45))
    box("Reader", (0.14, 0.03, 0.18), (0.42, 1.015, -0.35), M["black"], 0.006)
    box("ReaderGlow", (0.1, 0.004, 0.12), (0.42, 1.032, -0.35), M["led_c"])
    box("FloorPlate", (1.04, 0.006, 1.4), (0, 0.003, 0), M["graphite"])
    for name, s in (("FlapL", -1), ("FlapR", 1)):
        f = join([box(name + "Glass", (0.3, 0.5, 0.02), (s * 0.17, 0.8, 0), M["glass"]),
                  box(name + "Edge", (0.012, 0.48, 0.024), (s * 0.026, 0.8, 0), M["ny"])], name)
        set_origin(f, (s * 0.32, 0.8, 0))
    cyl("Pole", 0.035, 2.2, (0.42, 1.1, -0.7), M["dsteel"], verts=12)
    cyl("PoleCable", 0.008, 2.1, (0.455, 1.05, -0.72), M["rubber"], verts=6)
    box("CamMount", (0.05, 0.05, 0.12), (0.42, 2.17, -0.64), M["dsteel"])
    box("Camera", (0.12, 0.12, 0.26), (0.42, 2.2, -0.56), M["white"], 0.02)
    box("CamHood", (0.14, 0.012, 0.3), (0.42, 2.27, -0.54), M["graphite"])
    cyl("Lens", 0.04, 0.03, (0.42, 2.2, -0.42), M["black"], axis="z", verts=16)
    torus("LensRing", 0.042, 0.006, (0.42, 2.2, -0.41), M["nc"], axis="z", seg=16, mseg=4)


def phone_wall():
    apps = ["Z-FEED", "Z-BET", "Z-DATE", "Z-NEWS", "Z-PLAY", "Z-FIT"]
    box("Halo", (3.1, 1.8, 0.01), (0, 1.45, -0.075), M["nt"])
    box("Back", (3.06, 1.76, 0.04), (0, 1.45, -0.05), M["graphite"])
    box("Panel", (3.0, 1.7, 0.08), (0, 1.45, -0.01), M["panel"], 0.02)
    box("Header", (3.0, 0.16, 0.06), (0, 2.38, -0.01), M["ochre"], 0.01)
    text("HeaderText", "Z-PHONE  /  ŚCIANA POWIADOMIEŃ", (-1.42, 2.38, 0.022), 0.085, M["black"], align="LEFT")
    for i in range(6):
        col, row = i % 3, i // 3
        x, y = -1.0 + col * 0.55, 1.75 - row * 0.65
        box("Slot", (0.34, 0.52, 0.04), (x, y, 0.035), M["black"], 0.01)
        box("SlotInner", (0.26, 0.42, 0.01), (x, y + 0.02, 0.056), M["graphite"])
        torus("Rfid", 0.05, 0.006, (x, y + 0.02, 0.062), M["dsteel"], axis="z", seg=16, mseg=4)
        box("Ledge", (0.28, 0.03, 0.05), (x, y - 0.2, 0.07), M["dsteel"])
        text("App", apps[i], (x, y + 0.29, 0.032), 0.04, M["paint_w"])
        box(f"SlotLight{i}", (0.3, 0.03, 0.01), (x, y - 0.3, 0.06), M["led_c"])
    box("ScreenFrame", (0.95, 0.65, 0.05), (1.0, 1.55, 0.05), M["black"], 0.008)
    box("Screen", (0.9, 0.6, 0.005), (1.0, 1.55, 0.078), M["screen"])
    text("ScreenLabel", "RAPORT", (1.0, 1.93, 0.032), 0.04, M["paint_w"])
    box("Lockbox", (0.6, 0.4, 0.04), (1.0, 0.92, 0.035), M["steel"], 0.006)
    box("LockPad", (0.08, 0.1, 0.01), (1.2, 0.92, 0.06), M["black"])
    box("LockLed", (0.03, 0.008, 0.004), (1.2, 0.99, 0.066), M["ledr"])
    box("Base", (3.0, 0.5, 0.26), (0, 0.3, 0.0), M["graphite"], 0.015)
    box("BaseKick", (2.96, 0.05, 0.22), (0, 0.025, -0.01), M["black"])
    box("BaseLight", (2.9, 0.01, 0.01), (0, 0.05, 0.125), M["nt"])
    for x in (-0.75, 0, 0.75):
        box("BaseSeam", (0.01, 0.46, 0.005), (x, 0.3, 0.131), M["black"])
    for x in (-1.4, 1.4):
        cable("PanelCable", [(x, 0.6, -0.04), (x, 0.4, 0.05), (x + 0.05, 0.05, 0.12)], 0.012, M["cable"])


def lab_table():
    box("Top", (1.8, 0.04, 0.8), (0, 0.9, 0), M["white"], 0.01)
    box("TopEdge", (1.8, 0.012, 0.012), (0, 0.876, 0.39), M["nt"])
    box("Apron", (1.7, 0.08, 0.7), (0, 0.84, 0), M["matte"])
    for x in (-0.84, 0.84):
        for z in (-0.34, 0.34):
            box("Leg", (0.05, 0.8, 0.05), (x, 0.4, z), M["steel"])
            box("Foot", (0.07, 0.02, 0.07), (x, 0.01, z), M["rubber"])
        box("Stretcher", (0.04, 0.04, 0.68), (x, 0.18, 0), M["steel"])
    box("Shelf", (1.66, 0.02, 0.66), (0, 0.2, 0), M["dsteel"])
    for x, w, c in ((-0.5, 0.4, "ochre"), (0.05, 0.36, "white"), (0.55, 0.42, "graphite")):
        box("Crate", (w, 0.2, 0.3), (x, 0.31, -0.08), M[c], 0.01)
    box("CrateLabel", (0.12, 0.06, 0.002), (-0.5, 0.33, 0.071), M["black"])
    for i in range(6):
        x = -0.6 + i * 0.24
        box(f"Phone{i}", (0.09, 0.012, 0.17), (x, 0.932, 0.1), M["black"], 0.004)
        box(f"PhoneGlow{i}", (0.08, 0.002, 0.15), (x, 0.939, 0.1), M["display"])
        box(f"PhoneIcon{i}", (0.03, 0.001, 0.03), (x, 0.9405, 0.08), M[("led_y", "led_c", "led_w", "led_c", "led_w", "led_y")[i]])
        cable(f"PhoneCable{i}", [(x, 0.926, 0.19), (x * 0.7, 0.924, 0.26), (0.0, 0.924, 0.31)], 0.004, M["cable_w"])
    box("Hub", (0.3, 0.04, 0.08), (0, 0.94, 0.33), M["graphite"], 0.006)
    for k in range(6):
        box("HubLed", (0.012, 0.006, 0.004), (-0.1 + k * 0.04, 0.95, 0.371), M["led_c" if k % 2 else "ledr"])
    # microscope
    box("MicBase", (0.18, 0.03, 0.22), (-0.62, 0.935, -0.2), M["white"], 0.008)
    box("MicArm", (0.05, 0.28, 0.06), (-0.62, 1.07, -0.27), M["white"], 0.01, rot=(-10, 0, 0))
    box("MicStage", (0.14, 0.015, 0.12), (-0.62, 1.0, -0.17), M["graphite"])
    cyl("MicHead", 0.035, 0.18, (-0.62, 1.16, -0.18), M["graphite"], verts=12)
    cyl("MicEye", 0.018, 0.08, (-0.62, 1.27, -0.2), M["black"], verts=10)
    # vial rack and beaker
    box("VialRack", (0.32, 0.05, 0.1), (0.55, 0.945, -0.22), M["white"], 0.006)
    for k in range(5):
        cyl("Vial", 0.016, 0.14, (0.42 + k * 0.065, 1.0, -0.22), M["glass"], verts=10)
        cyl("VialLiquid", 0.013, 0.05 + 0.012 * k, (0.42 + k * 0.065, 0.955 + 0.006 * k, -0.22),
            M["liquid" if k % 2 else "ochre"], verts=8)
    cyl("Beaker", 0.05, 0.12, (0.15, 0.98, -0.25), M["glass"], verts=14)
    cyl("BeakerLiquid", 0.045, 0.05, (0.15, 0.945, -0.25), M["liquid"], verts=12)
    # tablet on a stand showing data
    box("TabletStand", (0.12, 0.08, 0.06), (-0.18, 0.96, -0.26), M["graphite"])
    box("Tablet", (0.26, 0.18, 0.012), (-0.18, 1.06, -0.25), M["black"], 0.004, rot=(-15, 0, 0))
    box("TabletGlow", (0.23, 0.15, 0.002), (-0.18, 1.06, -0.243), M["display"], rot=(-15, 0, 0))
    box("Clipboard", (0.2, 0.01, 0.28), (0.74, 0.925, 0.05), M["dsteel"], rot=(0, -12, 0))
    box("ClipPaper", (0.18, 0.004, 0.24), (0.74, 0.932, 0.06), M["white"], rot=(0, -12, 0))
    cable("TableCable", [(0.15, 0.88, -0.38), (0.1, 0.5, -0.38), (0.0, 0.02, -0.39)], 0.01, M["cable"])


def car():
    """Z-Drive: a white open-top two-seater on show, dash screen facing the camera."""
    box("Under", (1.7, 0.2, 2.3), (0, 0.3, 0), M["graphite"], 0.05)
    box("Shell", (1.72, 0.38, 2.5), (0, 0.6, 0), M["white"], 0.12, seg=3)
    box("Nose", (1.7, 0.2, 0.95), (0, 0.86, 0.75), M["white"], 0.09, rot=(7, 0, 0), seg=3)
    box("Stripe", (0.32, 0.01, 0.94), (0, 0.965, 0.763), M["ochre"], rot=(7, 0, 0))
    box("Tail", (1.7, 0.26, 0.6), (0, 0.9, -0.95), M["white"], 0.09, seg=3)
    box("TailStripe", (0.32, 0.01, 0.6), (0, 1.035, -0.95), M["ochre"])
    for s in (-1, 1):
        box("Sill", (0.14, 0.18, 1.0), (s * 0.78, 0.88, -0.25), M["white"], 0.05)
        box("SideSkirt", (0.16, 0.16, 0.8), (s * 0.92, 0.42, 0), M["graphite"], 0.03)
        box("UnderGlow", (0.01, 0.01, 1.6), (s * 0.86, 0.22, 0), M["nc"])
        box("Mirror", (0.12, 0.07, 0.05), (s * 0.93, 1.05, 0.22), M["black"], 0.01)
        text("SideText", "Z-DRIVE", (s * 0.861, 0.6, -0.1), 0.14, M["black"], yaw=90 * s)
        for z in (-0.82, 0.82):
            x = s * 0.95
            cyl("Tyre", 0.34, 0.24, (x, 0.34, z), M["rubber"], axis="x", verts=28, bevel=0.03)
            cyl("Rim", 0.24, 0.245, (x, 0.34, z), M["dsteel"], axis="x", verts=24)
            cyl("Disc", 0.16, 0.25, (x, 0.34, z), M["steel"], axis="x", verts=20)
            torus("RimRing", 0.22, 0.012, (x + s * 0.12, 0.34, z), M["ochre"], axis="x", seg=24, mseg=4)
            for k in range(5):
                box("Spoke", (0.012, 0.4, 0.045), (x + s * 0.123, 0.34, z), M["steel"], rot=(k * 36, 0, 0))
            box("Fender", (0.26, 0.1, 0.84), (s * 0.96, 0.73, z), M["white"], 0.04)
    box("Windshield", (1.6, 0.3, 0.02), (0, 1.07, 0.3), M["glass"], rot=(-50, 0, 0))
    box("WindFrame", (1.64, 0.03, 0.04), (0, 1.18, 0.2), M["black"])
    box("Dash", (1.5, 0.16, 0.28), (0, 0.98, 0.08), M["black"], 0.03)
    box("ScreenBezel", (0.6, 0.3, 0.03), (0.3, 1.154, 0.003), M["black"], 0.006, rot=(-20, 0, 0))
    box("Screen", (0.55, 0.26, 0.005), (0.3, 1.16, 0.02), M["screen"], rot=(-20, 0, 0))
    for k in range(3):
        cyl("Knob", 0.035, 0.035, (-0.05 - k * 0.1, 1.075, 0.12), M["graphite"], verts=16)
        box("KnobMark", (0.008, 0.004, 0.025), (-0.05 - k * 0.1, 1.094, 0.135), M["ny"])
    torus("Wheel", 0.16, 0.022, (-0.42, 1.12, -0.14), M["graphite"], axis="z", rot=(-25, 0, 0), seg=24, mseg=6)
    cable("Column", [(-0.42, 1.0, 0.0), (-0.42, 1.12, -0.14)], 0.02, M["graphite"])
    for s in (-1, 1):
        x = s * 0.4
        box("Seat", (0.5, 0.12, 0.5), (x, 0.86, -0.5), M["black"], 0.04)
        box("SeatBack", (0.5, 0.62, 0.12), (x, 1.15, -0.8), M["black"], 0.04, rot=(-12, 0, 0))
        box("SeatStripe", (0.08, 0.55, 0.01), (x, 1.15, -0.736), M["ochre"], rot=(-12, 0, 0))
        box("Headrest", (0.3, 0.14, 0.1), (x, 1.53, -0.88), M["black"], 0.03)
    box("Binder", (0.24, 0.05, 0.31), (0.4, 0.945, -0.48), M["ochre"], 0.005, rot=(0, 15, 0))
    text("BinderText", "INSTRUKCJA", (0.4, 0.972, -0.48), 0.03, M["black"], flat=True, yaw=15, depth=0.002)
    box("Spoiler", (1.7, 0.035, 0.26), (0, 1.2, -1.1), M["black"], 0.01, rot=(-6, 0, 0))
    for x in (-0.6, 0.6):
        box("Strut", (0.04, 0.17, 0.08), (x, 1.1, -1.08), M["dsteel"])
    f = join([box("HeadlightBar", (1.4, 0.04, 0.025), (0, 0.72, 1.242), M["led_w"]),
              box("HeadlightL", (0.28, 0.07, 0.02), (-0.6, 0.78, 1.225), M["led_w"]),
              box("HeadlightR", (0.28, 0.07, 0.02), (0.6, 0.78, 1.225), M["led_w"])], "Headlights")
    f.name = "Headlights"
    box("TailLight", (1.5, 0.035, 0.02), (0, 0.88, -1.255), M["red"])
    box("Grille", (0.9, 0.12, 0.02), (0, 0.52, 1.245), M["black"])
    z_letter("Badge", 0, 0.55, 1.262, 0.12, M["yellow"], depth=0.01)


def shelf():
    rng = random.Random(11)
    box("Plinth", (2.56, 0.12, 0.52), (0, 0.06, 0), M["graphite"])
    box("PlinthLight", (2.5, 0.01, 0.01), (0, 0.11, 0.262), M["ny"])
    box("BackPanel", (2.5, 1.95, 0.03), (0, 1.1, -0.255), M["graphite"])
    for x in (-1.245, 1.245):
        box("Upright", (0.05, 2.05, 0.56), (x, 1.03, 0), M["dsteel"])
        box("UprightLight", (0.012, 1.9, 0.012), (x, 1.03, 0.282), M["nt"])
    for s in range(4):
        y = 0.3 + s * 0.48
        box("Shelf", (2.44, 0.03, 0.48), (0, y, -0.01), M["steel"])
        box("Rail", (2.44, 0.04, 0.01), (0, y - 0.01, 0.235), M["ochre"])
        x = -1.12
        while x < 1.08:
            kind = (s + int((x + 2) * 3)) % 3 if s < 3 else 0
            col = rng.choice(("white", "white", "ochre", "black"))
            if s == 0 or kind == 0:  # boxes
                w, h = rng.uniform(0.14, 0.22), rng.uniform(0.18, 0.32)
                box("Product", (w, h, 0.2), (x + w / 2, y + 0.015 + h / 2, 0.0), M[col], 0.006)
                box("ProductBand", (w + 0.002, h * 0.2, 0.202), (x + w / 2, y + 0.015 + h * 0.6, 0.0),
                    M["ochre" if col != "ochre" else "black"])
                box("QR", (0.03, 0.03, 0.002), (x + w / 2, y + 0.015 + h * 0.3, 0.102), M["black"])
                x += w + 0.02
            elif kind == 1:  # cans
                for z in (-0.06, 0.07):
                    cyl("Can", 0.035, 0.12, (x + 0.035, y + 0.075, z), M[col], verts=10)
                    cyl("CanTop", 0.03, 0.004, (x + 0.035, y + 0.137, z), M["steel"], verts=10)
                x += 0.08
            else:  # bottles
                cyl("Bottle", 0.035, 0.18, (x + 0.035, y + 0.105, 0.0), M[col], verts=10)
                cyl("Neck", 0.015, 0.06, (x + 0.035, y + 0.225, 0.0), M[col], verts=8)
                cyl("Cap", 0.017, 0.02, (x + 0.035, y + 0.265, 0.0), M["ochre" if col != "ochre" else "black"], verts=8)
                x += 0.085
        for k in range(6):
            box("Tag", (0.07, 0.025, 0.003), (-1.0 + k * 0.4, y - 0.01, 0.242), M["white"])
    box("Header", (2.6, 0.26, 0.05), (0, 2.2, 0.0), M["black"], 0.01)
    z_letter("HeaderZ", -1.05, 2.2, 0.03, 0.16, M["yellow"], depth=0.01)
    text("HeaderText", "Z-MART", (-0.2, 2.2, 0.03), 0.15, M["yellow"])
    text("HeaderSub", "ABONAMENT AKTYWNY", (0.85, 2.2, 0.03), 0.045, M["nw"])


def checkout():
    box("Counter", (1.26, 0.8, 0.62), (0, 0.42, 0.01), M["white"], 0.02)
    box("Kick", (1.22, 0.06, 0.58), (0, 0.03, 0.01), M["graphite"])
    box("Band", (1.262, 0.06, 0.622), (0, 0.66, 0.01), M["ochre"])
    box("KickLight", (1.2, 0.01, 0.01), (0, 0.062, 0.32), M["ny"])
    box("Top", (1.3, 0.04, 0.68), (0, 0.85, 0.0), M["black"], 0.008)
    box("ScannerGlass", (0.3, 0.01, 0.3), (-0.3, 0.875, 0.02), M["glass"])
    box("ScannerLine", (0.26, 0.004, 0.01), (-0.3, 0.882, 0.02), M["led_c"])
    box("ScanTower", (0.34, 0.24, 0.06), (-0.3, 0.99, -0.2), M["graphite"], 0.01)
    box("ScanWindow", (0.26, 0.14, 0.004), (-0.3, 0.98, -0.169), M["dglass"])
    box("ScanTowerLine", (0.22, 0.004, 0.004), (-0.3, 0.98, -0.166), M["led_c"])
    box("Printer", (0.22, 0.14, 0.22), (0.35, 0.94, -0.15), M["graphite"], 0.01)
    box("Paper", (0.08, 0.002, 0.18), (0.35, 1.012, -0.02), M["white"], rot=(-10, 0, 0))
    box("Terminal", (0.09, 0.03, 0.16), (0.05, 0.9, 0.18), M["black"], 0.005, rot=(-15, 0, 0))
    box("TerminalGlow", (0.07, 0.002, 0.06), (0.05, 0.918, 0.15), M["display"], rot=(-15, 0, 0))
    box("ScreenPole", (0.04, 0.2, 0.04), (0.35, 1.08, -0.22), M["dsteel"])
    box("ScreenFrame", (0.32, 0.22, 0.03), (0.35, 1.15, -0.2), M["black"], rot=(-15, 0, 0))
    box("Screen", (0.29, 0.19, 0.004), (0.35, 1.15, -0.183), M["screen"], rot=(-15, 0, 0))
    cyl("LanePole", 0.02, 1.0, (-0.55, 1.37, -0.28), M["dsteel"], verts=8)
    box("LaneSign", (0.18, 0.18, 0.05), (-0.55, 1.95, -0.28), M["yellow"])
    text("LaneText", "1", (-0.55, 1.95, -0.25), 0.12, M["black"])
    box("Basket", (0.3, 0.12, 0.22), (-0.42, 0.93, 0.17), M["ochre"], 0.01)
    box("BasketInside", (0.27, 0.01, 0.19), (-0.42, 0.98, 0.17), M["graphite"])


def kiosk():
    box("Base", (0.56, 0.05, 0.42), (0, 0.025, 0), M["dsteel"], 0.01)
    box("Totem", (0.5, 1.75, 0.3), (0, 0.9, 0), M["black"], 0.03)
    for x in (-0.245, 0.245):
        box("EdgeLight", (0.012, 1.55, 0.012), (x, 0.92, 0.146), M["ny"])
    box("Bezel", (0.44, 0.74, 0.004), (0, 1.25, 0.151), M["graphite"])
    box("Screen", (0.4, 0.68, 0.005), (0, 1.25, 0.1545), M["screen"])
    z_letter("TopZ", 0, 1.68, 0.155, 0.1, M["yellow"], depth=0.01)
    text("Brand", "Z-MART", (0, 0.84, 0.152), 0.05, M["nw"])
    box("CardSlot", (0.12, 0.02, 0.01), (0, 0.74, 0.152), M["graphite"])
    box("CardLed", (0.08, 0.004, 0.004), (0, 0.755, 0.156), M["led_c"])
    box("Receipt", (0.1, 0.008, 0.01), (0, 0.66, 0.152), M["graphite"])
    for k in range(6):
        box("SpeakerSlot", (0.2, 0.008, 0.004), (0, 0.5 - k * 0.02, 0.151), M["graphite"])
    cyl("Cam", 0.012, 0.01, (0, 1.64, 0.152), M["graphite"], axis="z", verts=10)


def scanner():
    box("Foot", (0.44, 0.04, 0.44), (0, 0.02, 0), M["graphite"], 0.01)
    box("Pillar", (0.3, 1.16, 0.3), (0, 0.62, 0), M["dsteel"], 0.03)
    box("Groove", (0.08, 0.9, 0.005), (0, 0.6, 0.151), M["black"])
    box("Head", (0.38, 0.1, 0.36), (0, 1.23, 0.0), M["dsteel"], 0.02, rot=(25, 0, 0))
    box("Plate", (0.32, 0.03, 0.3), (0, 1.28, 0.02), M["black"], rot=(25, 0, 0))
    parts = [box("RingE", (0.27, 0.006, 0.015), (0, 0, z), M["led_c"]) for z in (-0.12, 0.12)]
    parts += [box("RingE", (0.015, 0.006, 0.25), (x, 0, 0), M["led_c"]) for x in (-0.13, 0.13)]
    parts += [box("Palm", (0.1, 0.006, 0.008), (0, 0, z), M["led_c"]) for z in (0.07, 0.0)]
    parts += [box("Palm", (0.008, 0.006, 0.07), (x, 0, 0.035), M["led_c"]) for x in (-0.05, 0.05)]
    parts += [box("Finger", (0.008, 0.006, 0.07), (x, 0, -0.035 - (0.01 if x in (-0.012, 0.012) else 0)), M["led_c"])
              for x in (-0.036, -0.012, 0.012, 0.036)]
    parts.append(box("Thumb", (0.008, 0.006, 0.05), (-0.07, 0, 0.03), M["led_c"], rot=(0, 35, 0)))
    ring = join(parts, "Ring")
    ring.rotation_euler = (math.radians(25), 0, 0)
    ring.location = bl(0, 1.297, 0.028)
    cyl("Conduit", 0.02, 1.1, (0.12, 0.58, -0.16), M["graphite"], verts=8)


def rack():
    box("Cabinet", (0.68, 2.2, 0.96), (0, 1.1, -0.02), M["black"], 0.01)
    for x in (-0.32, 0.32):
        box("Post", (0.04, 2.16, 0.04), (x, 1.1, 0.47), M["graphite"])
    for y in (0.06, 2.15):
        box("Rail", (0.68, 0.06, 0.04), (0, y, 0.47), M["graphite"])
    rng = random.Random(13)
    leds = ["led_c", "led_c", "led_c", "led_y", "led_w", "ledr", "led_c"]
    y = 0.1
    for hh in (0.09, 0.09, 0.18, 0.36, 0.09, 0.18, 0.09, 0.09, 0.27, 0.09, 0.18, 0.09):
        box("Unit", (0.6, hh - 0.01, 0.02), (0, y + hh / 2, 0.46), M["graphite"])
        for x in (-0.28, 0.28):
            box("UnitHandle", (0.014, hh * 0.6, 0.02), (x, y + hh / 2, 0.475), M["steel"])
        if hh >= 0.18:
            n = int(hh / 0.09)
            for r in range(n):
                for c in range(8):
                    box("Bay", (0.045, 0.07, 0.004), (-0.2 + c * 0.052, y + 0.045 + r * 0.09, 0.472), M["dsteel"])
                    if rng.random() < 0.6:
                        box("BayLed", (0.008, 0.008, 0.003), (-0.212 + c * 0.052, y + 0.015 + r * 0.09, 0.475),
                            M[rng.choice(leds)])
        else:
            box("UnitVent", (0.36, 0.03, 0.003), (-0.05, y + hh / 2, 0.471), M["black"])
            for k in range(3):
                box("UnitLed", (0.012, 0.01, 0.004), (0.17 + k * 0.025, y + hh / 2, 0.472), M[rng.choice(leds)])
        y += hh + 0.006
    box("Status", (0.5, 0.012, 0.004), (0, 2.08, 0.472), M["led_c"])
    box("TopVent", (0.6, 0.012, 0.86), (0, 2.205, -0.02), M["graphite"])
    for x in (-0.34, 0.34):
        for k in range(10):
            box("SideVent", (0.004, 0.02, 0.6), (x * 1.005, 0.4 + k * 0.16, -0.02), M["graphite"])
    for k in range(4):
        cable("TopCable", [(-0.15 + k * 0.1, 2.2, -0.3), (-0.15 + k * 0.1, 2.28, -0.35), (-0.15 + k * 0.1, 2.21, -0.45)],
              0.015, M[("cable", "cable_c", "cable", "cable_y")[k]], res=4)


def core_tower():
    cyl("Plinth", 0.98, 0.12, (0, 0.06, 0), M["dsteel"], verts=8, bevel=0.01)
    uvw(cyl("PlinthHaz", 0.9, 0.1, (0, 0.17, 0), M["hazard"], verts=8), 0.4)
    cyl("Base", 0.82, 0.18, (0, 0.31, 0), M["graphite"], verts=8, bevel=0.015)
    for k in range(8):
        a = k * math.tau / 8 + math.tau / 16
        box("BaseVent", (0.3, 0.08, 0.01), (math.cos(a) * 0.765, 0.3, math.sin(a) * 0.765), M["black"],
            rot=(0, 90 - math.degrees(a), 0))
    torus("BaseGlow", 0.62, 0.02, (0, 0.41, 0), M["nc"], seg=40, mseg=4)
    cyl("Core", 0.42, 2.6, (0, 1.6, 0), M["core"], verts=32)
    cyl("Containment", 0.52, 2.36, (0, 1.6, 0), M["glass"], verts=32)
    for y in (0.9, 1.7, 2.5):
        torus("CoreRing", 0.56, 0.035, (0, y, 0), M["nc"], seg=40, mseg=6)
        cyl("Clamp", 0.6, 0.06, (0, y + 0.07, 0), M["dsteel"], verts=32)
    for i in range(6):
        a = i * math.pi / 3
        box("Pillar", (0.1, 2.9, 0.1), (math.cos(a) * 0.72, 1.45, math.sin(a) * 0.72), M["steel"],
            rot=(0, -math.degrees(a), 0))
        box("PillarTag", (0.06, 0.08, 0.012), (math.cos(a) * 0.775, 1.2, math.sin(a) * 0.775), M["yellow"],
            rot=(0, 90 - math.degrees(a), 0))
    for i in range(4):
        a = math.radians(45 + i * 90)
        cx, cz = math.cos(a) * 0.86, math.sin(a) * 0.86
        cable("CoolPipe", [(cx, 0.4, cz), (cx, 2.7, cz), (cx * 0.98, 2.88, cz * 0.98), (cx * 0.8, 2.9, cz * 0.8)], 0.05,
              M["pipe_t"], res=6, sides=2)
        for y in (0.7, 1.5, 2.3):
            cyl("PipeFlange", 0.07, 0.04, (cx, y, cz), M["dsteel"], verts=12)
    cyl("Cap", 0.9, 0.2, (0, 3.0, 0), M["dsteel"], verts=8, bevel=0.02)
    torus("CapGlow", 0.84, 0.018, (0, 2.89, 0), M["nc"], seg=40, mseg=4)
    for k in range(16):
        a = k * math.tau / 16
        box("Fin", (0.4, 0.15, 0.03), (math.cos(a) * 0.55, 3.17, math.sin(a) * 0.55), M["graphite"],
            rot=(0, -math.degrees(a), 0))
    for k in range(6):
        a = k * math.tau / 6 + 0.3
        cable("CoreCable", [(math.cos(a) * 0.35, 3.1, math.sin(a) * 0.35), (math.cos(a) * 0.45, 3.5, math.sin(a) * 0.45),
                            (math.cos(a) * 0.8, 3.9, math.sin(a) * 0.8)], 0.045, M["cable" if k % 3 else "cable_c"],
              res=6, sides=2)


def console():
    box("Desk", (1.16, 0.76, 0.56), (0, 0.4, -0.04), M["black"], 0.02)
    box("Kick", (1.1, 0.06, 0.5), (0, 0.03, -0.04), M["graphite"])
    box("KickLight", (1.08, 0.01, 0.01), (0, 0.062, 0.215), M["nc"])
    for x in (-0.58, 0.58):
        box("Side", (0.04, 0.84, 0.62), (x, 0.42, -0.02), M["graphite"], 0.01)
        box("SideLight", (0.012, 0.7, 0.012), (x, 0.45, 0.29), M["nc"])
    box("Panel", (1.15, 0.05, 0.5), (0, 0.85, 0.05), M["graphite"], rot=(20, 0, 0))
    box("Screen", (0.7, 0.003, 0.32), (-0.15, 0.885, 0.04), M["screen"], rot=(20, 0, 0))
    box("Keyboard", (0.5, 0.012, 0.1), (-0.15, 0.8154 + 0.006, 0.2184 + 0.002), M["black"], rot=(20, 0, 0))
    cyl("Button", 0.08, 0.06, (0.4, 0.9, 0.08), M["yellow"], verts=24)
    torus("ButtonGuard", 0.1, 0.012, (0.4, 0.89, 0.08), M["steel"], seg=24, mseg=4)
    text("ButtonText", "AKCEPTUJĘ", (0.4, 0.823, 0.22), 0.035, M["nw"], flat=True, pitch=20, depth=0.003)
    for k in range(4):
        box("PanelLed", (0.015, 0.006, 0.015), (0.25 + k * 0.05, 0.84, -0.1), M["ledr" if k == 0 else "led_c"],
            rot=(20, 0, 0))
    cable("Mic", [(0.15, 0.86, -0.12), (0.12, 1.1, -0.1), (0.05, 1.15, 0.0)], 0.006, M["graphite"], res=6)
    ball("MicTip", 0.015, (0.05, 1.15, 0.0), M["led_c"], 1)
    for k in range(4):
        cable("BackCable", [(-0.3 + k * 0.15, 0.3, -0.33), (-0.3 + k * 0.15, 0.08, -0.34), (-0.35 + k * 0.12, 0.01, -0.3)],
              0.015, M[("cable", "cable_c", "cable", "cable_y")[k]], res=4)


def service_cabinet():
    d = M["dsteel"]
    box("Back", (1.3, 2.0, 0.04), (0, 1.0, -0.32), d)
    for x in (-0.625, 0.625):
        box("Side", (0.04, 2.0, 0.62), (x, 1.0, -0.03), d, 0.008)
        for k in range(6):
            box("SideVent", (0.004, 0.02, 0.3), (x * 1.036, 1.4 + k * 0.05, -0.03), M["black"])
    box("Top", (1.3, 0.05, 0.64), (0, 1.975, -0.03), d, 0.008)
    box("Bottom", (1.22, 0.08, 0.6), (0, 0.04, -0.03), M["graphite"])
    box("Inside", (1.22, 1.88, 0.01), (0, 1.0, -0.295), M["graphite"])
    for y in (0.6, 1.3):
        box("InnerShelf", (1.2, 0.02, 0.5), (0, y, -0.06), d)
    box("Toolbox", (0.4, 0.18, 0.22), (-0.3, 0.71, -0.1), M["ochre"], 0.01)
    torus("CableCoil", 0.12, 0.03, (0.3, 0.65, -0.1), M["cable"], seg=20, mseg=6)
    for x in (-0.15, 0.15):  # the two keys hang inside
        box("KeyHook", (0.02, 0.05, 0.04), (x, 1.7, -0.27), M["steel"])
        box("Key", (0.02, 0.08, 0.004), (x, 1.63, -0.25), M["steel"])
        box("KeyTag", (0.05, 0.03, 0.004), (x, 1.57, -0.25), M["yellow"])
    for name, s in (("DoorL", -1), ("DoorR", 1)):
        xc = s * 0.315
        parts = [box(name + "Slab", (0.62, 1.9, 0.03), (xc, 1.0, 0.3), M["steel"], 0.006),
                 uvw(box(name + "Haz", (0.62, 0.12, 0.004), (xc, 1.7, 0.317), M["hazard"]), 0.4),
                 box(name + "Handle", (0.025, 0.25, 0.02), (s * 0.05, 1.0, 0.325), d)]
        for k in range(5):
            parts.append(box(name + "Vent", (0.4, 0.02, 0.004), (xc, 0.3 + k * 0.05, 0.317), M["black"]))
        for y, hh in ((1.15, 0.8), (0.4, 0.06)):
            parts.append(box(name + "Rib", (0.5, 0.012, 0.006), (xc, y + hh / 2, 0.318), d))
        if s > 0:
            parts.append(text("Tag", "K//", (xc, 1.2, 0.317), 0.14, M["paint_c"], depth=0.002))
        else:
            parts.append(box("Hasp", (0.06, 0.08, 0.02), (-0.03, 1.15, 0.325), M["steel"]))
        door_ = join(parts, name)
        set_origin(door_, (s * 0.625, 1.0, 0.3))
    box("WarnPlate", (0.22, 0.22, 0.004), (0.647, 1.0, -0.03), M["yellow"], rot=(0, 90, 0))


def bomb():
    box("Case", (0.94, 0.42, 0.54), (0, 0.23, 0), M["olive"], 0.03)
    box("Lid", (0.96, 0.07, 0.56), (0, 0.475, 0), M["olive"], 0.02, rot=(0, 1.5, 0))
    for y in (0.12, 0.3):
        box("Rib", (0.95, 0.03, 0.55), (0, y, 0), M["olive2"])
    for x in (-0.46, 0.46):
        for z in (-0.26, 0.26):
            box("Corner", (0.06, 0.5, 0.06), (x, 0.25, z), M["dsteel"], 0.01)
        box("Handle", (0.02, 0.04, 0.2), (x * 1.06, 0.38, 0), M["dsteel"])
    tbox("Tape", (0.86, 0.08, 0.004), (0, 0.37, 0.272), "hazard", 0.4)
    text("Stencil", "NIE OTWIERAĆ", (0, 0.17, 0.274), 0.05, M["paint_w"], depth=0.002)
    box("Dent", (0.2, 0.08, 0.004), (0.3, 0.1, 0.271), M["olive2"], rot=(0, 0, 8))
    for i in range(3):
        cyl(f"Slot{i}", 0.05, 0.03, (-0.25 + i * 0.25, 0.515, -0.08), M["black"], verts=20)
        torus(f"SlotRing{i}", 0.065, 0.012, (-0.25 + i * 0.25, 0.515, -0.08), M["graphite"], seg=24, mseg=6)
    box("DisplayBezel", (0.36, 0.02, 0.14), (-0.1, 0.52, 0.17), M["black"])
    box("Display", (0.32, 0.01, 0.1), (-0.1, 0.528, 0.17), M["lcd"])
    box("Keypad", (0.16, 0.02, 0.13), (0.22, 0.52, 0.17), M["black"])
    for r in range(4):
        for c in range(3):
            box("Key", (0.03, 0.008, 0.022), (0.185 + c * 0.035, 0.532, 0.125 + r * 0.03), M["steel"])
    for x in (-0.4, 0.4):
        cyl("KeySwitch", 0.035, 0.04, (x, 0.53, 0.15), M["steel"], verts=16)
        box("KeySlot", (0.004, 0.003, 0.03), (x, 0.551, 0.15), M["black"])
    for x in (-0.33, 0.05):
        box("DuctTape", (0.08, 0.006, 0.58), (x, 0.513, 0), M["tape"], rot=(0, 4, 0))
    for k, (x, m) in enumerate(((-0.25, "cable_y"), (0.0, "cable_c"), (0.25, "cable_w"))):
        cable("Wire", [(0.2, 0.53, 0.1), (0.1 + k * 0.05, 0.56, 0.02), (x, 0.535, -0.03)], 0.007, M[m], res=6)
    text("Mark", "N-07", (-0.473, 0.3, 0), 0.06, M["paint_w"], yaw=-90, depth=0.002)


def emitter():
    for i in range(3):
        a = i * 120
        box("TripodLeg", (0.03, 0.55, 0.03), (0.09 * math.sin(math.radians(a)), 0.26, 0.09 * math.cos(math.radians(a))),
            M["dsteel"], rot=(-18, a, 0))
    cyl("Post", 0.025, 0.45, (0, 0.7, 0), M["dsteel"], verts=10)
    cyl("Collar", 0.04, 0.04, (0, 0.5, 0), M["graphite"], verts=10)
    box("Body", (0.22, 0.2, 0.3), (0, 1.0, -0.01), M["black"], 0.02)
    for k in range(6):
        box("Fin", (0.2, 0.04, 0.008), (0, 1.12, -0.12 + k * 0.045), M["graphite"])
    cyl("Barrel", 0.055, 0.05, (0, 1.0, 0.165), M["dsteel"], axis="z", verts=16)
    cyl("Lens", 0.045, 0.01, (0, 1.0, 0.193), M["led_c"], axis="z", verts=16)
    box("Sticker", (0.002, 0.04, 0.08), (0.111, 1.0, 0.0), M["yellow"])
    box("Led", (0.02, 0.012, 0.004), (0.06, 1.07, 0.141), M["ledr"])
    cable("PowerCable", [(0, 0.95, -0.16), (0.05, 0.6, -0.19), (0.12, 0.01, -0.18)], 0.01, M["cable"])


def mirror():
    cyl("Foot", 0.16, 0.035, (0, 0.0175, 0), M["dsteel"], verts=24)
    for k in range(24):
        a = k * math.tau / 24
        box("Tick", (0.004, 0.004, 0.03 if k % 6 else 0.05), (math.cos(a) * 0.13, 0.037, math.sin(a) * 0.13),
            M["yellow" if k % 6 == 0 else "steel"], rot=(0, 90 - math.degrees(a), 0))
    cyl("Post", 0.022, 0.78, (0, 0.42, 0), M["dsteel"], verts=10)
    cyl("Collar", 0.035, 0.05, (0, 0.82, 0), M["graphite"], verts=10)
    box("ForkBase", (0.4, 0.03, 0.04), (0, 0.86, 0), M["graphite"])
    for x in (-0.18, 0.18):
        box("ForkArm", (0.025, 0.24, 0.035), (x, 0.98, 0), M["graphite"])
        cyl("Knob", 0.02, 0.03, (x, 1.05, 0), M["steel"], axis="x", verts=10)
    box("MirrorFrame", (0.33, 0.3, 0.025), (0, 1.05, -0.01), M["black"], 0.006)
    box("Mirror", (0.3, 0.27, 0.006), (0, 1.05, 0.005), M["chrome"])


def sensor_stand():
    for z in (-0.72, 0.72):
        box("Foot", (0.48, 0.05, 0.14), (0, 0.025, z), M["dsteel"], 0.01)
    for z in (-0.78, 0.78):
        box("Post", (0.08, 1.7, 0.08), (0.0, 0.875, z), M["dsteel"])
    box("Board", (0.1, 1.45, 1.6), (0.06, 0.95, 0), M["graphite"], 0.015)
    tbox("BoardHaz", (0.004, 0.06, 1.6), (0.112, 1.64, 0), "hazard", 0.4)
    box("Duct", (0.05, 1.4, 0.06), (-0.02, 0.95, 0), M["dsteel"])
    for i in range(5):
        y, z = 1.4 - i * 0.25, -0.6 + i * 0.3
        torus("Bezel", 0.105, 0.015, (0.12, y, z), M["steel"], axis="x", seg=20, mseg=4)
        cyl("Housing", 0.1, 0.02, (0.12, y, z), M["black"], axis="x", verts=20)
        cyl(f"Sensor{i}", 0.09, 0.03, (0.16, y, z), M["black"], axis="x", verts=20)
        text("Label", f"0{i + 1}", (0.112, y - 0.15, z), 0.05, M["nw"], yaw=90, depth=0.002)
        box("Led", (0.004, 0.02, 0.05), (0.112, y + 0.13, z), M["graphite"])


def door():
    box("Door", (1.3, 2.35, 0.06), (0, 1.175, 0), M["dsteel"], 0.01)
    for s in (-1, 1):
        box("PanelTop", (1.1, 0.9, 0.004), (-0.05, 1.75, s * 0.031), M["graphite"])
        box("PanelBot", (1.1, 0.7, 0.004), (-0.05, 0.65, s * 0.031), M["graphite"])
        uvw(box("Haz", (1.3, 0.14, 0.004), (0, 0.1, s * 0.0325), M["hazard"]), 0.4)
        box("Pull", (0.03, 0.3, 0.012), (0.5, 1.1, s * 0.037), M["steel"])
        box("Plate", (0.3, 0.08, 0.004), (-0.05, 1.3, s * 0.0335), M["yellow"])
    box("Slit", (0.1, 0.5, 0.066), (0.4, 1.5, 0), M["black"])
    box("DoorEdge", (0.03, 2.3, 0.07), (0.63, 1.175, 0), M["nc"])


def rod():
    cyl("Rod", 0.025, 0.24, (0, 0.13, 0), M["core"], verts=12)
    cyl("Tube", 0.04, 0.24, (0, 0.13, 0), M["glass"], verts=16)
    cyl("CapTop", 0.05, 0.03, (0, 0.265, 0), M["steel"], verts=16)
    cyl("CapBottom", 0.05, 0.03, (0, -0.005, 0), M["steel"], verts=16)
    torus("Tag", 0.045, 0.006, (0, 0.02, 0), M["graphite"], seg=16, mseg=4)


def badge():
    box("Card", (0.086, 0.054, 0.004), (0, 0, 0), M["white"])
    box("Stripe", (0.086, 0.012, 0.005), (0, 0.018, 0), M["yellow"])
    box("Photo", (0.022, 0.026, 0.005), (-0.028, -0.006, 0), M["graphite"])
    box("QR", (0.022, 0.022, 0.005), (0.028, -0.008, 0), M["black"])


# ---------- characters (pivots LegL/LegR at the hips, ArmL/ArmR at the shoulders, Head at the neck) ----------

def leg(x, boot_h=0.18, knee=True, holster=False, cargo=False, bulk=1.0):
    s, b = (1 if x > 0 else -1), bulk
    parts = [box("Hip", (0.18 * b, 0.2, 0.2 * b), (x, 0.77, 0), M["pants"], 0.06, seg=2),
             box("Thigh", (0.16 * b, 0.24, 0.18 * b), (x, 0.58, 0.005), M["pants"], 0.055, seg=2),
             box("Shin", (0.14 * b, 0.34, 0.155 * b), (x, 0.3, -0.005), M["pants"], 0.05, seg=2),
             box("Boot", (0.155 * b, boot_h, 0.2), (x, 0.04 + boot_h / 2, 0.01), M["boots"], 0.045, seg=2),
             box("Toe", (0.15 * b, 0.1, 0.13), (x, 0.07, 0.11), M["boots"], 0.045, seg=2),
             box("Sole", (0.17 * b, 0.045, 0.31), (x, 0.0225, 0.035), M["rubber"], 0.015),
             box("BootCuff", (0.165 * b, 0.04, 0.21), (x, 0.04 + boot_h, 0.01), M["leather"], 0.015)]
    for k in range(3):
        parts.append(box("Lace", (0.09, 0.012, 0.012), (x, 0.11 + k * 0.035, 0.122), M["webbing"]))
    if knee:
        parts.append(box("Knee", (0.13 * b, 0.13, 0.05), (x, 0.46, 0.085 * b), M["armor"], 0.025, seg=2))
    if cargo:
        parts.append(box("Cargo", (0.04, 0.15, 0.13), (x + s * 0.09 * b, 0.6, 0.0), M["pants"], 0.015))
        parts.append(box("CargoFlap", (0.045, 0.04, 0.135), (x + s * 0.092 * b, 0.665, 0.0), M["webbing"], 0.01))
    if holster:
        parts += [box("Holster", (0.06, 0.18, 0.14), (x + s * 0.11, 0.62, 0.0), M["webbing"], 0.02),
                  box("LegStrap", (0.18, 0.03, 0.19), (x, 0.58, 0.005), M["webbing"])]
    pivot(f"Leg{'R' if x > 0 else 'L'}", (x, 0.88, 0), *parts)


def arm(x, sleeve="jacket", pad="jacket", pad_size=1.0, glove="glove", device=False, bracer=False):
    s = 1 if x > 0 else -1
    parts = [ball("Shoulder", 0.075, (x, 1.43, 0), M[sleeve], 1),
             box("Upper", (0.125, 0.3, 0.13), (x, 1.28, 0), M[sleeve], 0.055, seg=2),
             box("Fore", (0.11, 0.27, 0.115), (x, 1.0, 0.012), M[sleeve], 0.05, seg=2),
             box("Cuff", (0.12, 0.05, 0.125), (x, 0.875, 0.012), M["leather"], 0.02),
             box("Hand", (0.085, 0.1, 0.1), (x, 0.8, 0.015), M[glove], 0.035, seg=2),
             box("Fingers", (0.08, 0.07, 0.065), (x, 0.735, 0.03), M[glove], 0.028, seg=2),
             box("Thumb", (0.03, 0.06, 0.035), (x - s * 0.048, 0.79, 0.05), M[glove], 0.012)]
    if pad:
        p = pad_size
        parts.append(box("ShoulderPad", (0.17 * p, 0.08 * p, 0.18 * p), (x + s * 0.015, 1.48 + 0.02 * (p - 1), 0),
                         M[pad], 0.035, rot=(0, 0, -s * 14), seg=2))
        if p > 1.2:
            parts.append(box("PadPlate", (0.14 * p, 0.03, 0.15 * p), (x + s * 0.035, 1.41, 0), M[pad], 0.012,
                             rot=(0, 0, -s * 28)))
            parts.append(box("PadStrap", (0.13, 0.025, 0.14), (x, 1.36, 0), M["webbing"]))
    if device:
        parts += [box("WristComp", (0.06, 0.05, 0.1), (x + s * 0.055, 0.95, 0.012), M["graphite"], 0.012),
                  box("WristGlow", (0.004, 0.03, 0.065), (x + s * 0.086, 0.95, 0.012), M["led_c"])]
    if bracer:
        parts.append(box("Bracer", (0.125, 0.15, 0.135), (x, 0.97, 0.012), M["armor"], 0.03, seg=2))
    pivot(f"Arm{'R' if x > 0 else 'L'}", (x, 1.46, 0), *parts)


def head_base():
    """Neck, head, jaw, nose, brows and ears; returns the parts for the Head pivot."""
    return [cyl("Neck", 0.055, 0.12, (0, 1.6, 0), M["skin"], verts=12),
            ball("HeadMesh", 0.118, (0, 1.695, 0.0), M["skin"], 2, scale=(0.95, 1.08, 1.0)),
            box("Jaw", (0.15, 0.08, 0.14), (0, 1.63, 0.03), M["skin"], 0.035, seg=2),
            box("Nose", (0.026, 0.045, 0.04), (0, 1.68, 0.118), M["skin"], 0.012),
            box("Brow", (0.17, 0.018, 0.03), (0, 1.735, 0.105), M["hair"], 0.006),
            box("EarL", (0.022, 0.05, 0.04), (-0.112, 1.69, -0.005), M["skin"], 0.01),
            box("EarR", (0.022, 0.05, 0.04), (0.112, 1.69, -0.005), M["skin"], 0.01)]


def torso(width=0.44, jacket_len=0.1):
    """Pelvis, V-shaped torso and collar base shared by the three nomads."""
    box("Pelvis", (0.36, 0.16, 0.22), (0, 0.9, 0), M["pants"], 0.05, seg=2)
    box("Waist", (0.38, 0.24, 0.23), (0, 1.1, 0), M["jacket"], 0.07, seg=2)
    box("Chest", (width, 0.3, 0.26), (0, 1.33, 0.005), M["jacket"], 0.09, seg=2)
    box("Hem", (0.4, jacket_len, 0.25), (0, 1.0 - jacket_len / 2 + 0.05, 0), M["jacket"], 0.04, seg=2)


def npc():
    """Nomad 1: hooded scout with goggles, scarf, layered jacket and a big pack with an antenna."""
    leg(-0.11, holster=True)
    leg(0.11, cargo=True)
    torso()
    box("Belt", (0.38, 0.06, 0.24), (0, 0.96, 0), M["leather"], 0.02)
    box("Buckle", (0.06, 0.045, 0.01), (0, 0.96, 0.123), M["steel"])
    for x in (-0.14, 0.14):
        box("Pouch", (0.08, 0.09, 0.05), (x, 0.94, 0.12), M["webbing"], 0.02)
    box("SidePouch", (0.05, 0.1, 0.1), (0.2, 0.93, -0.02), M["webbing"], 0.02)
    box("Flap", (0.2, 0.4, 0.014), (0.07, 1.22, 0.135), M["jacket"], 0.006)
    box("Zip", (0.012, 0.38, 0.008), (-0.03, 1.21, 0.137), M["steel"])
    for x in (-0.1, 0.1):
        box("Strap", (0.05, 0.48, 0.014), (x, 1.24, 0.142), M["webbing"])
    box("ChestStrap", (0.3, 0.035, 0.014), (0, 1.33, 0.148), M["webbing"])
    box("ChestLight", (0.04, 0.02, 0.01), (-0.1, 1.33, 0.155), M["led_c"])
    box("CollarBack", (0.3, 0.15, 0.06), (0, 1.54, -0.09), M["jacket"], 0.025, rot=(10, 0, 0))
    for s in (-1, 1):
        box("CollarSide", (0.06, 0.13, 0.17), (s * 0.13, 1.53, -0.01), M["jacket"], 0.025, rot=(0, 0, -s * 10))
        box("PackStrap", (0.05, 0.06, 0.3), (s * 0.12, 1.47, -0.05), M["webbing"])
    box("Scarf", (0.22, 0.08, 0.2), (0, 1.51, 0.02), M["cloth"], 0.035, seg=2)
    box("Pack", (0.34, 0.42, 0.16), (0, 1.2, -0.21), M["canvas"], 0.05, seg=2)
    box("PackFlap", (0.35, 0.14, 0.17), (0, 1.38, -0.215), M["canvas2"], 0.035)
    for x in (-0.1, 0.1):
        box("PackPocket", (0.12, 0.14, 0.05), (x, 1.08, -0.3), M["canvas2"], 0.02)
    box("PackStripe", (0.35, 0.03, 0.01), (0, 1.25, -0.293), M["ochre"])
    cyl("Bedroll", 0.07, 0.4, (0, 1.47, -0.22), M["cloth"], axis="x", verts=12)
    for x in (-0.13, 0.13):
        cyl("BedrollTie", 0.073, 0.02, (x, 1.47, -0.22), M["leather"], axis="x", verts=12)
    cyl("Antenna", 0.007, 0.55, (0.13, 1.68, -0.27), M["dsteel"], verts=6)
    ball("AntennaTip", 0.016, (0.13, 1.96, -0.27), M["led_c"], 1)
    arm(-0.3, device=True)
    arm(0.3)
    parts = head_base()
    parts += [box("Visor", (0.22, 0.045, 0.03), (0, 1.715, 0.11), M["visor"], 0.014),
              box("GoggleFrame", (0.25, 0.07, 0.03), (0, 1.715, 0.096), M["graphite"], 0.018),
              torus("GoggleStrap", 0.118, 0.01, (0, 1.715, 0.0), M["graphite"], seg=20, mseg=4),
              box("MouthScarf", (0.19, 0.07, 0.07), (0, 1.625, 0.09), M["cloth"], 0.03),
              ball("Hood", 0.158, (0, 1.725, -0.075), M["jacket"], 2, scale=(1.0, 1.1, 1.04)),
              torus("HoodRim", 0.13, 0.028, (0, 1.71, 0.055), M["jacket"], axis="z", seg=20, mseg=6, rot=(-12, 0, 0))]
    pivot("Head", (0, 1.58, 0), *parts)


def npc2():
    """Nomad 2: long coat, tall boots, dyed mohawk, a monocle visor and one heavy pauldron."""
    leg(-0.11, boot_h=0.42, knee=False)
    leg(0.11, boot_h=0.42, knee=False)
    torso(width=0.42)
    box("Sash", (0.4, 0.07, 0.25), (0, 0.98, 0), M["leather"], 0.02)
    box("Buckle", (0.07, 0.05, 0.01), (0.05, 0.98, 0.128), M["brass"])
    box("CoatBack", (0.42, 0.58, 0.04), (0, 0.68, -0.17), M["jacket"], 0.018, rot=(14, 0, 0))
    for s in (-1, 1):
        box("CoatSide", (0.04, 0.56, 0.28), (s * 0.225, 0.68, -0.03), M["jacket"], 0.018, rot=(0, 0, s * 9))
        box("CoatTail", (0.13, 0.24, 0.035), (s * 0.15, 0.85, 0.13), M["jacket"], 0.012, rot=(-6, 0, s * 6))
        box("Lapel", (0.1, 0.24, 0.022), (s * 0.08, 1.37, 0.14), M["jacket"], 0.01, rot=(0, 0, s * 18))
        for y in (1.06, 1.16, 1.26):
            cyl("Button", 0.012, 0.01, (s * 0.06, y, 0.125), M["brass"], axis="z", verts=8)
    box("CollarBack", (0.42, 0.22, 0.06), (0, 1.6, -0.1), M["jacket"], 0.025, rot=(18, 0, 0))
    box("Satchel", (0.22, 0.2, 0.08), (-0.17, 1.0, -0.17), M["leather"], 0.03, seg=2)
    box("SatchelStrap", (0.05, 0.62, 0.014), (0.0, 1.26, 0.14), M["leather"], rot=(0, 0, 38))
    arm(-0.3, pad="armor", pad_size=1.55, bracer=True)
    arm(0.3, pad=None, device=True)
    parts = head_base()
    parts += [box("Visor", (0.1, 0.04, 0.02), (0.045, 1.705, 0.113), M["visor"], 0.008),
              box("MonocleRim", (0.12, 0.06, 0.02), (0.045, 1.705, 0.1), M["dsteel"], 0.012),
              box("Implant", (0.02, 0.07, 0.09), (0.111, 1.7, 0.02), M["steel"], 0.006),
              ball("Stubble", 0.12, (0, 1.715, -0.012), M["hair"], 2, scale=(0.96, 0.95, 1.02))]
    for k in range(7):
        h = 0.12 + 0.05 * math.sin(k * 0.6 + 0.4)
        parts.append(box("Mohawk", (0.035, h, 0.055), (0, 1.79 + h / 2 - 0.02, 0.09 - k * 0.04), M["hair_m"], 0.012,
                         rot=(-15 + k * 6, 0, 0)))
    for k in range(3):
        parts.append(torus("Piercing", 0.012, 0.003, (-0.12, 1.675 + k * 0.02, 0.0), M["steel"], axis="x", seg=8, mseg=2))
    pivot("Head", (0, 1.58, 0), *parts)


def npc3():
    """Nomad 3: heavy armour vest, cap, respirator, big pads and a radio pack."""
    leg(-0.115, boot_h=0.24, cargo=True, bulk=1.1)
    leg(0.115, boot_h=0.24, cargo=True, bulk=1.1)
    torso()
    box("Belt", (0.42, 0.07, 0.27), (0, 0.97, 0), M["webbing"], 0.02)
    for x in (-0.16, -0.05, 0.06):
        box("MagPouch", (0.09, 0.12, 0.06), (x, 0.97, 0.14), M["webbing"], 0.02)
    box("VestFront", (0.44, 0.38, 0.07), (0, 1.27, 0.12), M["armor"], 0.04, seg=2)
    box("VestBack", (0.44, 0.42, 0.07), (0, 1.25, -0.12), M["armor"], 0.04, seg=2)
    for s in (-1, 1):
        box("VestSide", (0.05, 0.3, 0.2), (s * 0.225, 1.2, 0), M["armor"], 0.02)
    box("VestInsert", (0.24, 0.16, 0.01), (0, 1.32, 0.158), M["jacket"], 0.004)
    box("VestStripe", (0.3, 0.025, 0.01), (0, 1.17, 0.158), M["ochre"])
    for x in (-0.12, 0.0, 0.12):
        box("VestPouch", (0.1, 0.1, 0.05), (x, 1.12, 0.165), M["webbing"], 0.02)
    box("NeckGuard", (0.34, 0.1, 0.28), (0, 1.5, -0.01), M["armor"], 0.045, seg=2)
    box("RadioPack", (0.3, 0.36, 0.14), (0, 1.22, -0.22), M["graphite"], 0.04, seg=2)
    box("RadioFace", (0.2, 0.1, 0.01), (0, 1.3, -0.292), M["dsteel"])
    box("RadioLed", (0.03, 0.015, 0.006), (0.06, 1.3, -0.298), M["ledr"])
    cyl("Whip", 0.006, 0.8, (-0.1, 1.75, -0.24), M["black"], verts=6)
    ball("WhipTip", 0.014, (-0.1, 2.15, -0.24), M["ledr"], 1)
    arm(-0.31, pad="armor", pad_size=1.45, bracer=True)
    arm(0.31, pad="armor", pad_size=1.45, bracer=True)
    parts = head_base()
    parts += [box("Visor", (0.24, 0.05, 0.03), (0, 1.72, 0.106), M["visor"], 0.016),
              box("VisorFrame", (0.26, 0.075, 0.03), (0, 1.72, 0.092), M["dsteel"], 0.018),
              box("Respirator", (0.13, 0.09, 0.07), (0, 1.625, 0.105), M["rubber"], 0.035, seg=2),
              ball("CapCrown", 0.13, (0, 1.765, -0.005), M["jacket"], 2, scale=(1.0, 0.62, 1.06)),
              box("CapBrim", (0.2, 0.018, 0.13), (0, 1.77, 0.165), M["jacket"], 0.009, rot=(-6, 0, 0)),
              box("CapButton", (0.03, 0.015, 0.03), (0, 1.846, -0.005), M["graphite"])]
    for s in (-1, 1):
        parts.append(cyl("Filter", 0.032, 0.05, (s * 0.075, 1.61, 0.13), M["dsteel"], axis="x", verts=12))
    pivot("Head", (0, 1.58, 0), *parts)


# ---------- Kruk's van (front +x, origin at the ground centre) ----------

def van():
    rng = random.Random(17)
    box("Chassis", (4.9, 0.25, 1.8), (0, 0.42, 0), M["graphite"], 0.03)
    box("LowerBody", (5.0, 0.72, 2.0), (0, 0.84, 0), M["van"], 0.07)
    box("Cargo", (3.65, 1.08, 2.0), (-0.68, 1.72, 0), M["van"], 0.06)
    box("Cab", (0.9, 0.74, 1.96), (1.55, 1.56, 0), M["van2"], 0.08)
    box("Hood", (0.5, 0.2, 1.92), (2.3, 1.28, 0), M["van2"], 0.05, rot=(0, 0, -6))
    box("Windshield", (0.04, 0.68, 1.78), (2.125, 1.6, 0), M["black"], rot=(0, 0, 22))
    for k in range(6):
        box("Grille", (0.03, 0.66, 0.025), (2.16, 1.6, -0.75 + k * 0.3), M["dsteel"], rot=(0, 0, 22))
    for s in (-1, 1):
        z = s * 1.0
        box("APillar", (0.07, 0.7, 0.08), (2.12, 1.6, s * 0.94), M["van2"], rot=(0, 0, 22))
        box("SideWindow", (0.62, 0.4, 0.02), (1.6, 1.63, z * 0.99), M["black"])
        for k in range(4):
            box("WinBar", (0.025, 0.42, 0.03), (1.37 + k * 0.15, 1.63, z * 1.0), M["dsteel"])
        box("Mirror", (0.08, 0.2, 0.14), (2.15, 1.62, s * 1.08), M["black"], 0.02)
        box("Step", (0.6, 0.04, 0.12), (1.75, 0.5, s * 1.03), M["dsteel"])
        tbox("HazardBand", (4.6, 0.12, 0.01), (-0.1, 0.98, s * 1.006), "hazard", 0.4)
        for i, (xa, w, mat) in enumerate(((-2.4, 1.1, "primer"), (-1.25, 0.9, "rust"), (-0.3, 1.2, "primer"))):
            y = 1.72 + rng.uniform(-0.08, 0.08)
            box("Armor", (w, 0.8, 0.03), (xa + w / 2, y, s * 1.018), M[mat], 0.01, rot=(0, 0, rng.uniform(-2.5, 2.5)))
            for k in range(int(w / 0.18)):
                for yy in (y - 0.36, y + 0.36):
                    cyl("Rivet", 0.015, 0.012, (xa + 0.08 + k * 0.18, yy, s * 1.036), M["steel"], axis="z", verts=6)
        box("Skirt", (4.7, 0.18, 0.03), (0, 0.55, s * 1.012), M["mud"])
        for k in range(9):
            hgt = rng.uniform(0.08, 0.28)
            box("MudSplash", (rng.uniform(0.2, 0.5), hgt, 0.01), (-2.2 + k * 0.55, 0.62 + hgt / 2, s * 1.016), M["mud"])
        for x in (-1.65, 1.75):
            cyl("Tyre", 0.38, 0.26, (x, 0.38, s * 0.95), M["rubber"], axis="z", verts=24, bevel=0.03)
            cyl("Hub", 0.22, 0.27, (x, 0.38, s * 0.95), M["dsteel"], axis="z", verts=12)
            cyl("HubCap", 0.08, 0.28, (x, 0.38, s * 0.95), M["ochre"], axis="z", verts=8)
            box("Arch", (0.95, 0.12, 0.12), (x, 0.82, s * 1.02), M["mud"], 0.03)
            box("Flap", (0.22, 0.3, 0.02), (x - 0.5, 0.32, s * 0.95), M["rubber"])
        text("Paint", "KRUK", (-1.0, 1.95, s * 1.04), 0.32, M["paint_w"], yaw=0 if s > 0 else 180, depth=0.004,
             pitch=0)
        for k in range(4):
            box("Tally", (0.025, 0.22, 0.004), (0.35 + k * 0.07, 1.45, s * 1.038), M["paint_w"], rot=(0, 0, 8))
        box("TallyX", (0.32, 0.025, 0.004), (0.45, 1.45, s * 1.038), M["paint_w"], rot=(0, 0, 25))
        box("Scratch", (0.6, 0.012, 0.004), (-2.0, 1.4, s * 1.036), M["paint_c"], rot=(0, 0, -12))
    box("Bumper", (0.18, 0.22, 2.06), (2.5, 0.62, 0), M["dsteel"], 0.02)
    for z in (-0.6, 0.6):
        cable("BullBar", [(2.52, 0.5, z), (2.6, 0.9, z), (2.55, 1.2, z)], 0.035, M["steel"], res=4)
    cable("BullBarTop", [(2.6, 0.9, -0.75), (2.6, 0.9, 0.75)], 0.035, M["steel"])
    box("Plate", (0.01, 0.12, 0.4), (2.6, 0.56, 0), M["paint_w"])
    f = join([box("LampL", (0.04, 0.16, 0.26), (2.49, 1.05, -0.72), M["led_w"]),
              box("LampR", (0.04, 0.16, 0.26), (2.49, 1.05, 0.72), M["led_w"]),
              box("BarLamp", (0.04, 0.08, 0.9), (2.63, 0.96, 0), M["led_w"])], "Headlights")
    f.name = "Headlights"
    for z in (-0.85, 0.85):
        box("TailLight", (0.03, 0.3, 0.14), (-2.51, 1.0, z), M["tail"])
    box("RearBumper", (0.16, 0.2, 2.02), (-2.5, 0.6, 0), M["dsteel"])
    box("RearSeam", (0.01, 1.0, 0.012), (-2.505, 1.7, 0), M["black"])
    cyl("SpareTyre", 0.35, 0.22, (-2.62, 1.6, 0.45), M["rubber"], axis="x", verts=24)
    cyl("SpareHub", 0.2, 0.23, (-2.62, 1.6, 0.45), M["dsteel"], axis="x", verts=12)
    for k in range(6):
        box("LadderRung", (0.03, 0.03, 0.36), (-2.55, 0.8 + k * 0.28, -0.6), M["steel"])
    for z in (-0.78, -0.42):
        box("LadderRail", (0.03, 1.6, 0.03), (-2.56, 1.5, z), M["steel"])
    cable("Exhaust", [(-2.0, 0.35, -0.7), (-2.55, 0.32, -0.7), (-2.62, 0.36, -0.7)], 0.04, M["rust"], res=4)
    # roof rack with cargo, antennas, dish and the (unlit) light bar
    for z in (-0.92, 0.92):
        box("RackRail", (3.8, 0.05, 0.05), (-0.6, 2.42, z), M["dsteel"])
        for x in (-2.4, -1.2, 0.0, 1.2):
            box("RackPost", (0.05, 0.16, 0.05), (x, 2.32, z), M["dsteel"])
    for x in (-2.3, -1.6, -0.9, -0.2, 0.5, 1.2):
        box("RackBar", (0.05, 0.05, 1.84), (x, 2.42, 0), M["dsteel"])
    box("Crate", (0.7, 0.32, 0.55), (-1.9, 2.6, -0.45), M["olive"], 0.02)
    box("Crate", (0.55, 0.26, 0.45), (-1.85, 2.88, -0.45), M["olive2"], 0.02, rot=(0, 8, 0))
    for k in range(3):
        box("Jerrycan", (0.18, 0.34, 0.3), (-0.95 + k * 0.22, 2.62, 0.55), M[("ochre", "van2", "ochre")[k]], 0.02)
    cyl("Tarp", 0.17, 1.3, (0.3, 2.6, -0.2), M["canvas"], axis="z", verts=12)
    cyl("RoofTyre", 0.34, 0.2, (-0.55, 2.55, -0.25), M["rubber"], verts=20)
    box("LightBarBase", (0.12, 0.08, 1.6), (1.15, 2.5, 0), M["black"])
    box("LightBar", (0.04, 0.06, 1.5), (1.21, 2.5, 0), M["nc"])
    for x, z, h in ((-2.3, 0.85, 0.8), (-2.1, 0.85, 0.55), (0.9, -0.85, 0.7)):
        cyl("Antenna", 0.008, h, (x, 2.45 + h / 2, z), M["black"], verts=6)
        ball("AntennaTip", 0.018, (x, 2.45 + h, z), M["ledr"], 1)
    cyl("DishMast", 0.03, 0.4, (-1.4, 2.65, 0.55), M["dsteel"], verts=8)
    cyl("Dish", 0.32, 0.12, (-1.35, 2.9, 0.55), M["white"], verts=20, r2=0.06)
    cyl("DishFeed", 0.012, 0.3, (-1.35, 3.05, 0.55), M["dsteel"], verts=6)
    for k in range(4):
        cable("Strap", [(-2.3 + k * 0.6, 2.45, -0.75), (-2.3 + k * 0.6, 2.8, -0.3), (-2.3 + k * 0.6, 2.45, 0.1)],
              0.012, M["webbing"], res=4)


PROPS = {
    "desk": desk, "monitor": monitor, "phone_dock": phone_dock, "wall_screen": wall_screen,
    "wall_screen_wide": lambda: wall_screen(3.6, 1.2, "nt"), "z_logo": z_logo, "locker": locker, "sofa": sofa,
    "plant": plant, "turnstile": turnstile, "phone_wall": phone_wall, "lab_table": lab_table, "car": car,
    "shelf": shelf, "checkout": checkout, "kiosk": kiosk, "scanner": scanner, "rack": rack,
    "core_tower": core_tower, "console": console, "service_cabinet": service_cabinet, "bomb": bomb,
    "emitter": emitter, "mirror": mirror, "sensor_stand": sensor_stand, "door": door, "npc": npc, "npc2": npc2,
    "npc3": npc3, "rod": rod, "badge": badge, "van": van,
}


def main():
    global FONT
    bpy.ops.wm.read_factory_settings(use_empty=True)
    FONT = bpy.data.fonts.load(str(ROOT / "godot" / "fonts" / "ChakraPetch-Bold.ttf"))
    mats()
    for r in LAYOUT["rooms"]:
        if not ONLY or f"room_{r['id']}" in ONLY:
            ROOMS[r["id"]]()
            export(f"room_{r['id']}")
    used = {p["model"] for p in LAYOUT["props"]} | {"bomb", "door", "npc", "npc2", "npc3", "rod", "badge", "van"}
    missing = used - PROPS.keys()
    assert not missing, f"layout uses models with no builder: {missing}"
    for name in sorted(used):
        if not ONLY or name in ONLY:
            PROPS[name]()
            export(name)


main()
