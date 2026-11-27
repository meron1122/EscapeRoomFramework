"""Builds ZETA's head (the hologram face of Z Corporation's AI) from code and writes simulator/godot/assets/zeta.glb.

Run from the repo root, never with a window:
    /opt/homebrew/bin/blender --background --factory-startup --python simulator/blender/build_zeta.py

How it is made: the skull and face are one signed distance field (blended ellipsoids and capsules, with the lips and the
brow as a low relief on top, mirrored in x). Her left half is sampled on a cube sphere of rays that is denser toward the
face, evened out on the surface, decimated (sparing the eyes, the nose and the mouth) and made whole by a mirror modifier,
so the two halves are identical. The skin is shaded with the field's own normals. Neck, collar, halo and the iris rings are
lathed profiles. Plain materials, no textures, no armature.

Nodes in the glb (Godot axes: x right, y up, she faces +z; origin at the centre of the collar's base):
    Body          neck and projector collar (static), material Mask
    Head          skull and face, material Mask; pivot at the top of the neck. Its underside is a ball around the pivot
                  that the neck enters, so no turn or nod opens a gap
      EyeL, EyeR  iris: two rings and a dot on the eyeball, material EyeGlow (nothing else uses it). L is her left, +x
      Mark        the Z on the forehead, material MarkGlow
    Halo          the ring round the head, tilt baked into the mesh, origin at the centre of the head; material HaloGlow.
                  A child of the root: it clears the head at rest however it is spun, but does not follow the head's nod
"""
import math
from pathlib import Path

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Vector

OUT = Path(__file__).resolve().parents[1] / "godot" / "assets" / "zeta.glb"
CYAN = "#00f0ff"
# Blender axes: x her left, -y front, z up
G = -0.250  # the plane of the brow
PIVOT = Vector((0.0, -0.030, 0.345))  # top of the neck
CENTRE = Vector((0.0, G + 0.168, 0.550))  # centre of the head: the halo turns about it
NECK_R = 0.074  # the neck where it enters the head
HALO_R, HALO_TILT = 0.200, 20.0
EYE = np.array([0.068, G + 0.046, 0.480])  # centre of her left eyeball
EYE_R = np.array([0.040, 0.030, 0.030])  # wider than deep, so the open lids never show past it
EYE_W, LID_UP, LID_LOW = 0.037, 0.0128, 0.0132  # half width of the opening, its height above and below the centre
IRIS = 0.0136  # the upper lid rests on it: a calm look
MOUTH_Z, MOUTH_W, SMILE = 0.349, 0.037, 0.0028  # the line between the lips: its height in the middle, half its width, the lift of its corners
MARK_Z = 0.566  # height of the middle of the forehead mark
HEAD_TRIS = 12000
SKIN = "skin"  # vertex layer: 1 where a vertex takes its normal from the field


# ---------- signed distance fields (numpy, points as (N, 3)) ----------

def ell(p, c, r):
    q = (p - np.asarray(c)) / np.asarray(r)
    k0 = np.linalg.norm(q, axis=1)
    k1 = np.linalg.norm(q / np.asarray(r), axis=1)
    return k0 * (k0 - 1.0) / np.maximum(k1, 1e-9)


def cone(p, a, b, ra, rb):
    """A capsule from a to b whose radius runs from ra to rb."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    ab = b - a
    h = np.clip(((p - a) @ ab) / (ab @ ab), 0.0, 1.0)
    return np.linalg.norm(p - a - h[:, None] * ab, axis=1) - (ra + (rb - ra) * h)


def chain(p, pts):
    """A string of capsules through (point, radius) pairs."""
    return np.min([cone(p, a, b, ra, rb) for (a, ra), (b, rb) in zip(pts, pts[1:])], axis=0)


def smin(a, b, k):
    h = np.clip(0.5 + 0.5 * (b - a) / k, 0.0, 1.0)
    return b + (a - b) * h - k * h * (1.0 - h)


def smax(a, b, k):
    return -smin(-a, -b, k)


def gradient(field, pts, h=0.002):
    """Unit normals of a field; the wide step keeps creases finer than the mesh out of the shading."""
    g = np.zeros_like(pts)
    for k in range(3):
        e = np.zeros(3)
        e[k] = h
        g[:, k] = field(pts + e) - field(pts - e)
    return g / np.maximum(np.linalg.norm(g, axis=1), 1e-12)[:, None]


def lid_arcs(p):
    """Front view of her left eye: x and z from its centre (outer corner canted up), and the two lid circles (centre z, radius)."""
    x = p[:, 0] - EYE[0]
    z = p[:, 2] - EYE[2] - 0.06 * x
    up = (EYE_W ** 2 + LID_UP ** 2) / (2 * LID_UP)
    low = (EYE_W ** 2 + LID_LOW ** 2) / (2 * LID_LOW)
    return x, z, (LID_UP - up, up), (low - LID_LOW, low)


def bell(u):
    return np.exp(-u * u)


def relief(p):
    """The low relief of the face as a height over the front view (x >= 0): lips, philtrum, brow, the root of the nose."""
    x, y, z = p[:, 0], p[:, 1], p[:, 2]
    u = np.clip(x / MOUTH_W, 0.0, 1.0)
    span = np.sqrt(1.0 - u * u)  # 1 in the middle of the mouth, 0 at its corner
    line = MOUTH_Z + SMILE * u * u  # the line between the lips, corners a little up: the fixed smile
    upper = 0.0140 * span ** 1.1 - 0.0026 * bell(x / 0.0065)  # height of the upper lip, dipped in the middle
    lower = 0.0175 * span
    t = np.where(z >= line, (z - line) / np.maximum(upper, 1e-5), (line - z) / np.maximum(lower, 1e-5))
    h = np.where(z >= line, 0.0056, 0.0068) * span ** 0.6 * np.sin(math.pi * np.clip(t, 0.0, 1.0) ** 0.6) ** 1.2  # a cushion, fullest near the line
    h -= 0.0030 * bell((z - line) / 0.0019) * np.clip((MOUTH_W + 0.004 - x) / 0.004, 0.0, 1.0)  # the closed mouth
    h -= 0.0020 * bell(np.hypot(x - MOUTH_W - 0.001, z - MOUTH_Z - SMILE) / 0.005)  # its corner, tucked in
    h -= 0.0022 * bell(x / 0.0045) * bell((z - MOUTH_Z - 0.028) / 0.011)  # philtrum
    arch = np.interp(x, (0.012, 0.040, 0.070, 0.100, 0.128), (0.5150, 0.5215, 0.5255, 0.5215, 0.5110))
    h += 0.0038 * bell((z - arch) / 0.0055) * np.clip((x - 0.006) / 0.014, 0.0, 1.0) * np.clip((0.130 - x) / 0.015, 0.0, 1.0)  # brow: a calm, high arch
    h -= 0.0030 * bell(x / 0.018) * bell((z - 0.487) / 0.014)  # the root of the nose lies back a little
    return h * np.clip((G + 0.075 - y) / 0.03, 0.0, 1.0)  # the front of the head only


def head_sdf(p):
    """Skull and face, symmetric in x. Crown z = 0.70, chin z = 0.26, eye line z = 0.48; the face looks along -y and
    G is the plane of the brow, the front of the forehead."""
    p = np.column_stack([np.abs(p[:, 0]), p[:, 1], p[:, 2]])
    d = ell(p, (0, G + 0.176, 0.548), (0.147, 0.176, 0.152))  # cranium
    d = smin(d, ell(p, (0, G + 0.155, 0.372), (0.110, 0.165, 0.112)), 0.05)  # the lower head, back to the jaw's corner
    d = smin(d, ell(p, (0, G + 0.100, 0.435), (0.115, 0.105, 0.178)), 0.04)  # the oval of the face
    d = smin(d, ell(p, (0.062, G + 0.046, 0.440), (0.040, 0.042, 0.032)), 0.04)  # cheekbone
    d = smin(d, ell(p, (0.058, G + 0.042, 0.395), (0.040, 0.040, 0.046)), 0.04)  # cheek: full, no hollow under the bone
    d = smin(d, ell(p, (0, G + 0.024, 0.291), (0.040, 0.030, 0.032)), 0.035)  # chin
    d = smin(d, ell(p, (0, G + 0.056, 0.356), (0.060, 0.054, 0.052)), 0.03)  # the arch of the teeth, which the lips lie on
    # under the jaw: a floor that is level under the chin and climbs to the jaw's corner, for a clear jaw line
    floor = 0.256 + 0.16 * (p[:, 1] - G - 0.02) + 0.62 * (np.sqrt(p[:, 0] ** 2 + 0.04 ** 2) - 0.04) - p[:, 2]
    d = smax(d, floor / 1.19, 0.02)
    smooth = d  # the face before the eyes are cut in

    d = smax(d, -ell(p, (0.068, G - 0.016, 0.488), (0.042, 0.043, 0.025)), 0.02)  # eye socket
    nose = chain(p, (((0, G + 0.012, 0.487), 0.0075), ((0, G - 0.015, 0.445), 0.0072), ((0, G - 0.033, 0.418), 0.008)))  # bridge
    nose = smin(nose, ell(p, (0, G - 0.035, 0.405), (0.0125, 0.013, 0.012)), 0.010)  # tip
    nose = smin(nose, cone(p, (0, G - 0.036, 0.3995), (0, G - 0.010, 0.395), 0.0065, 0.0065), 0.008)  # columella
    nose = smin(nose, ell(p, (0.0185, G - 0.010, 0.400), (0.0105, 0.015, 0.0105)), 0.008)  # wing
    d = smin(d, nose, 0.022)
    d = smax(d, -ell(p, (0.0110, G - 0.026, 0.3885), (0.0055, 0.0095, 0.0055)), 0.006)  # nostril: a soft dimple
    d = smax(d, -ell(p, (0, G - 0.012, 0.320), (0.028, 0.014, 0.011)), 0.015)  # crease above the chin
    d = d - relief(p)

    # eye: a lid dome over the ball, opened along an almond, the ball showing through
    x, z, (uz, ur), (lz, lr) = lid_arcs(p)
    d = smin(d, smax(ell(p, EYE, EYE_R + 0.004), smooth - 0.004, 0.006), 0.010)  # the dome keeps to the face at the temple
    opening = np.maximum(np.hypot(x, z - uz) - ur, np.hypot(x, z - lz) - lr)
    d = smax(d, -np.maximum(opening, p[:, 1] - EYE[1]), 0.004)
    d = smin(d, ell(p, EYE, EYE_R), 0.002)
    fold = bell((np.hypot(x, z - uz) - ur - 0.0075) / 0.0026) * np.clip((EYE_W + 0.003 - np.abs(x)) / 0.008, 0.0, 1.0) * np.clip(z / 0.005, 0.0, 1.0)
    d = d + 0.0017 * fold * np.clip((EYE[1] - p[:, 1]) / 0.01, 0.0, 1.0)  # the fold of the upper lid

    d = smin(d, ell(p, (0.118, G + 0.200, 0.445), (0.014, 0.022, 0.038)), 0.008)  # ear: small and simple
    d = smax(d, -ell(p, (0.131, G + 0.193, 0.447), (0.006, 0.011, 0.021)), 0.006)
    return smin(d, np.linalg.norm(p - np.array(PIVOT), axis=1) - (NECK_R + 0.004), 0.015)  # the ball the neck enters


# ---------- meshing ----------

def ray_ball(n, pull):
    """Unit directions of a cube sphere (n quads along an edge), crowded toward `pull`, and its quads."""
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=2.0)
    bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=n - 1, use_grid_fill=True)
    dirs = np.array([v.co[:] for v in bm.verts])
    dirs[np.abs(dirs) < 1e-6] = 0.0
    dirs = np.tan(dirs * (math.pi / 4))
    dirs = dirs / np.linalg.norm(dirs, axis=1)[:, None] + np.asarray(pull)
    quads = [[v.index for v in f.verts] for f in bm.faces]
    bm.free()
    return dirs / np.linalg.norm(dirs, axis=1)[:, None], quads


def cast(sdf, origins, dirs, reach, steps=120):
    """The outermost point of the solid on every ray."""
    lo, hi = np.zeros(len(dirs)), np.full(len(dirs), reach)
    todo = np.arange(len(dirs))
    for t in np.linspace(reach, 0.0, steps):
        if not len(todo):
            break
        inside = sdf(origins[todo] + dirs[todo] * t) < 0
        lo[todo[inside]] = t
        hi[todo[~inside]] = t
        todo = todo[~inside]
    for _ in range(24):
        mid = (lo + hi) / 2
        inside = sdf(origins + dirs * mid[:, None]) < 0
        lo, hi = np.where(inside, mid, lo), np.where(inside, hi, mid)
    return origins + dirs * ((lo + hi) / 2)[:, None]


def relaxed(sdf, verts, quads, rounds=12):
    """Evens out the spacing of a half mesh on the field's surface: the rays leave it sparse on the walls they graze."""
    edges = np.unique(np.sort([(q[i], q[i - 1]) for q in quads for i in range(4)], axis=1), axis=0)
    count = np.bincount(edges.ravel(), minlength=len(verts))[:, None]
    seam = verts[:, 0] < 1e-9
    for _ in range(rounds):
        total = np.zeros_like(verts)
        np.add.at(total, edges[:, 0], verts[edges[:, 1]])
        np.add.at(total, edges[:, 1], verts[edges[:, 0]])
        verts = (verts + total / count) / 2
        verts[seam, 0] = 0.0  # the mirror image pulls the centre line back
        for _ in range(2):  # and back onto the surface, down the field's slope
            g = np.column_stack([sdf(verts + e) - sdf(verts - e) for e in np.eye(3) * 0.001]) / 0.002
            verts = verts - sdf(verts)[:, None] * g / (g * g).sum(axis=1)[:, None]
    return verts


def worth(p):
    """How much a point of the skin matters to the eye, 0 to 1: the decimation spares the eyes, the nose and the mouth."""
    x, y, z = np.abs(p[:, 0]), p[:, 1], p[:, 2]
    eyes = bell(np.hypot((x - EYE[0]) / 0.050, (z - EYE[2] - 0.008) / 0.034))
    mouth = bell(np.hypot(x / 0.058, (z - MOUTH_Z) / 0.024))
    nose = bell(np.hypot(x / 0.036, (z - 0.415) / 0.040))
    ear = 0.35 * bell(np.hypot((y - G - 0.2) / 0.03, (z - 0.445) / 0.045)) * (x > 0.09)
    return np.maximum(np.maximum.reduce([eyes, mouth, nose]) * np.clip((G + 0.09 - y) / 0.03, 0.0, 1.0), ear)


def skin_mesh(sdf, origin, n, pull, reach, tris):
    """Her left half of the field's surface, decimated, then mirrored: one closed, exactly symmetric mesh."""
    dirs, quads = ray_ball(n, pull)
    quads = [q for q in quads if all(dirs[i][0] > -1e-9 for i in q)]
    used = sorted({i for q in quads for i in q})
    remap = {old: new for new, old in enumerate(used)}
    quads = [[remap[i] for i in q] for q in quads]
    verts = relaxed(sdf, cast(sdf, np.broadcast_to(np.asarray(origin, float), (len(used), 3)), dirs[used], reach), quads)
    me = bpy.data.meshes.new("half")
    me.from_pydata(verts.tolist(), [], quads)
    ob = bpy.data.objects.new("half", me)
    bpy.context.collection.objects.link(ob)
    spare = ob.vertex_groups.new(name="spare")
    for i, w in enumerate(worth(verts)):
        spare.add([i], float(w), "REPLACE")
    mod = ob.modifiers.new("decimate", "DECIMATE")
    mod.ratio, mod.use_collapse_triangulate = tris / (4.0 * len(quads)), True
    mod.vertex_group, mod.invert_vertex_group, mod.vertex_group_factor = "spare", True, 0.003  # the factor weighs worth against error of shape
    half = bpy.data.meshes.new_from_object(ob.evaluated_get(bpy.context.evaluated_depsgraph_get()))
    bm = bmesh.new()
    bm.from_mesh(half)
    bm.verts.layers.deform.remove(bm.verts.layers.deform.active)  # the weights have done their work
    for v in bm.verts:
        if v.is_boundary:  # the open edge is the centre line; the decimation lets it wander a little
            v.co.x = 0.0
    bm.to_mesh(half)
    bm.free()
    ob.modifiers.clear()
    ob.vertex_groups.clear()
    ob.data = half
    bpy.data.meshes.remove(me)
    mod = ob.modifiers.new("mirror", "MIRROR")
    mod.use_axis, mod.use_mirror_merge, mod.merge_threshold = (True, False, False), True, 1e-6
    whole = bpy.data.meshes.new_from_object(ob.evaluated_get(bpy.context.evaluated_depsgraph_get()))
    bpy.data.objects.remove(ob)
    bpy.data.meshes.remove(half)
    return whole


def revolve(bm, profile, segs, place, closed=False):
    """Lathes a profile of (radius, height) pairs, listed counter-clockwise (radius to the right, height up) so the faces
    look outward; place(radius, angle, height) puts a point in space. A point on the axis becomes one vertex."""
    layer = bm.verts.layers.int[SKIN]
    rows = []
    for r, h in profile:
        rows.append([bm.verts.new(place(r, 2 * math.pi * k / segs, h)) for k in range(segs if r > 0 else 1)])
        for v in rows[-1]:
            v[layer] = 0
    for a, b in zip(rows, rows[1:] + rows[:1] if closed else rows[1:]):
        for k in range(segs):
            ring = [a[k % len(a)], a[(k + 1) % len(a)], b[(k + 1) % len(b)], b[k % len(b)]]
            bm.faces.new(list(dict.fromkeys(ring))).smooth = True


def upright(centre=(0, 0, 0), lean=0.0):
    """place() for a lathe about the vertical through `centre`; lean shifts it in y per unit of height."""
    return lambda r, a, h: Vector(centre) + Vector((r * math.cos(a), r * math.sin(a) + lean * h, h))


def linear(hex_colour):
    c = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    return [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c] + [1.0]


def material(name, colour, rough, glow=None):
    m = bpy.data.materials.new(name)
    m.use_backface_culling = True  # exported as single sided
    p = m.node_tree.nodes["Principled BSDF"]
    p.inputs["Base Color"].default_value = linear(colour)
    p.inputs["Roughness"].default_value = rough
    p.inputs["Metallic"].default_value = 0.0
    if glow:
        p.inputs["Emission Color"].default_value = linear(glow)
        p.inputs["Emission Strength"].default_value = 1.0
    return m


def new_bm():
    bm = bmesh.new()
    bm.verts.layers.int.new(SKIN)
    return bm


def finish(name, bm, mat, origin=Vector(), parent=None, field=None):
    """Turns a bmesh (in world coordinates) into an object with its origin at `origin`."""
    for e in bm.edges:  # hard edges stay hard under smooth shading
        if len(e.link_faces) == 2 and e.calc_face_angle() > math.radians(40):
            e.smooth = False
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    me.materials.append(mat)
    if field:  # shade the decimated surface with the field's own normals
        normals = np.array([v.normal[:] for v in me.vertices])
        skin = np.array([a.value for a in me.attributes[SKIN].data], bool)
        normals[skin] = gradient(field, np.array([v.co[:] for v in me.vertices])[skin])
        me.normals_split_custom_set_from_vertices(normals.tolist())
    me.attributes.remove(me.attributes[SKIN])
    me.transform(Matrix.Translation(-Vector(origin)))
    ob = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(ob)
    ob.parent = parent
    ob.location = Vector(origin) - (parent.location if parent else Vector())  # one level deep, nothing rotated or scaled
    return ob


# ---------- the parts ----------

def build_body(mask):
    bm = new_bm()
    neck = ((0.0, 0.088), (0.03, 0.079), (0.07, 0.0715), (0.13, 0.067), (0.20, 0.0655), (0.26, 0.067), (0.31, 0.0705), (PIVOT.z, NECK_R))
    revolve(bm, [(r, z) for z, r in neck], 32, upright(lean=PIVOT.y / PIVOT.z))
    revolve(bm, [(0, 0), (0.124, 0), (0.124, 0.012), (0.078, 0.012)], 48, upright())  # the projector collar closes the cut
    return finish("Body", bm, mask)


def build_head(mask):
    me = skin_mesh(head_sdf, (0, G + 0.18, 0.48), 128, (0, -0.48, -0.12), 0.37, HEAD_TRIS)
    bm = new_bm()
    bm.from_mesh(me)
    bpy.data.meshes.remove(me)
    for v in bm.verts:
        v[bm.verts.layers.int[SKIN]] = 1
    for f in bm.faces:
        f.smooth = True
    return finish("Head", bm, mask, PIVOT, field=head_sdf)


def build_eye(name, side, glow, head):
    """The iris: two rings and a dot, lying on the eyeball and standing a little proud of it."""
    centre = EYE * (side, 1, 1)
    lift = 0.0012

    def on_ball(r, a, h):
        x, z = r * math.cos(a), r * math.sin(a)
        rx, ry, rz = EYE_R + h
        return Vector((centre[0] + x, centre[1] - ry * math.sqrt(1 - (x / rx) ** 2 - (z / rz) ** 2), centre[2] + z))

    bm = new_bm()
    for outer, inner, segs in ((IRIS, IRIS - 0.0030, 32), (0.0078, 0.0056, 24), (0.0028, 0, 16)):
        revolve(bm, [(outer, -0.0005), (outer, lift), (inner, lift)] + [(inner, -0.0005)] * (inner > 0), segs, on_ball)
    return finish(name, bm, glow, centre - (0, EYE_R[1], 0), head)


def build_mark(glow, head):
    """The corporation's Z: three straight strokes, pressed onto the forehead from the front and raised a little."""
    a, b, t, w = 0.0145, 0.0170, 0.0062, 0.0088  # half width, half height, thickness of the bars, width of the diagonal
    tl, tr, low_in, low_r, br, bl, up_in, up_l = (-a, b), (a, b), (-a + w, -b + t), (a, -b + t), (a, -b), (-a, -b), (a - w, b - t), (-a, b - t)
    outline = [tl, up_l, up_in, bl, br, low_r, low_in, tr]  # counter-clockwise as she is seen from the front
    flat = np.array([(u, 0.0, MARK_Z + v) for u, v in outline])
    skin = cast(head_sdf, flat, np.broadcast_to((0.0, -1.0, 0.0), flat.shape), 0.3)
    bm = new_bm()
    front = {q: bm.verts.new(s - (0, 0.0022, 0)) for q, s in zip(outline, skin)}
    back = {q: bm.verts.new(s + (0, 0.002, 0)) for q, s in zip(outline, skin)}
    for quad in ((up_l, up_in, tr, tl), (bl, low_in, tr, up_in), (bl, br, low_r, low_in)):  # top bar, diagonal, bottom bar
        bm.faces.new([front[q] for q in quad])
    for q, r in zip(outline, outline[1:] + outline[:1]):
        bm.faces.new([front[q], back[q], back[r], front[r]])
    return finish("Mark", bm, glow, (0, skin[:, 1].mean(), MARK_Z), head)


def build_halo(glow):
    """One thin ring round the head, high in front and low behind. The tilt is in the mesh, so a turn about the vertical swings it round."""
    tilt = Matrix.Rotation(math.radians(-HALO_TILT), 4, "X")
    section = [(HALO_R + 0.0032 * math.cos(a), 0.0055 * math.sin(a)) for a in (math.radians(60 * k) for k in range(6))]
    bm = new_bm()
    revolve(bm, section, 96, lambda r, a, h: CENTRE + tilt @ Vector((r * math.cos(a), r * math.sin(a), h)), closed=True)
    return finish("Halo", bm, glow, CENTRE)


def turn(pts, degrees):
    """Points of the head after it turns about the vertical through its pivot."""
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    return (pts - np.array(PIVOT)) @ np.array(((c, s, 0), (-s, c, 0), (0, 0, 1))) + np.array(PIVOT)


def halo_gap(pts, any_turn):
    """Least distance from points to the halo's centre line as built, or to the band it sweeps when turned about the vertical."""
    rel = pts - np.array(CENTRE)
    if any_turn:
        arc = np.radians(np.linspace(-HALO_TILT, HALO_TILT, 41))
        return np.hypot(np.hypot(rel[:, 0], rel[:, 1])[:, None] - HALO_R * np.cos(arc), rel[:, 2][:, None] - HALO_R * np.sin(arc)).min()
    a = np.linspace(0, 2 * math.pi, 180, endpoint=False)
    t = math.radians(HALO_TILT)
    ring = HALO_R * np.column_stack([np.cos(a), np.sin(a) * math.cos(t), -np.sin(a) * math.sin(t)])
    return min(np.linalg.norm(rel - q, axis=1).min() for q in ring)


def check():
    """What the game relies on: the nodes, their parents, the private glow materials, the symmetry, the size and the budget."""
    bpy.context.view_layer.update()
    ob = bpy.data.objects
    names = ("Body", "Head", "EyeL", "EyeR", "Mark", "Halo")
    assert sorted(o.name for o in ob) == sorted(names)
    assert [ob[n].parent and ob[n].parent.name for n in names] == [None, None, "Head", "Head", "Head", None]
    for mat, owners in (("Mask", ["Body", "Head"]), ("EyeGlow", ["EyeL", "EyeR"]), ("MarkGlow", ["Mark"]), ("HaloGlow", ["Halo"])):
        assert sorted(o.name for o in ob if mat in o.data.materials) == owners, mat
    head = np.array([v.co[:] for v in ob["Head"].data.vertices])
    assert {tuple(v) for v in np.round(head * (-1, 1, 1), 6)} == {tuple(v) for v in np.round(head, 6)}, "the head is not symmetric"
    skull = np.array([ob["Head"].matrix_world @ v.co for v in ob["Head"].data.vertices])
    still, swept = halo_gap(skull, False), halo_gap(skull, True)
    turned = min(halo_gap(turn(skull, a), False) for a in (-25, 25))
    print(f"halo: radius {HALO_R}; from its centre line to the head {still:.4f}, with the head turned 25 degrees {turned:.4f}, with the halo turned any way {swept:.4f}")
    assert min(still, turned, swept) > 0.008
    tris = sum(len(p.vertices) - 2 for o in ob for p in o.data.polygons)
    pts = np.array([o.matrix_world @ v.co for o in ob if o.name != "Halo" for v in o.data.vertices])
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    print(f"triangles {tris}; without the halo: width {hi[0] - lo[0]:.3f}, height {hi[2] - lo[2]:.3f}, depth {hi[1] - lo[1]:.3f}, base z {lo[2]:.4f}")
    assert tris < 15000, tris
    for o in ob:
        assert not o.data.validate(verbose=True), f"the mesh of {o.name} needed repair"
    assert abs(lo[2]) < 1e-4 and 0.69 < hi[2] < 0.71 and hi[0] - lo[0] < 0.5 and abs(hi[0] + lo[0]) < 1e-5


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    mask = material("Mask", "#c9ced2", 0.5)
    eye_glow, mark_glow, halo_glow = (material(n, "#02181a", 0.4, CYAN) for n in ("EyeGlow", "MarkGlow", "HaloGlow"))
    build_body(mask)
    head = build_head(mask)
    build_eye("EyeL", 1, eye_glow, head)
    build_eye("EyeR", -1, eye_glow, head)
    build_mark(mark_glow, head)
    build_halo(halo_glow)
    check()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.export_scene.gltf(filepath=str(OUT), export_format="GLB", use_selection=True, export_apply=True)
    print("exported", OUT, OUT.stat().st_size, "bytes")


main()
