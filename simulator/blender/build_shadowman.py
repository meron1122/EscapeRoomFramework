"""Builds the Shadowman bust (the game master's face) from code and writes simulator/godot/assets/shadowman.glb.

Run from the repo root, never with a window:
    /opt/homebrew/bin/blender --background --factory-startup --python simulator/blender/build_shadowman.py

How it is made: skull, ears and shoulders are signed distance fields (blended ellipsoids, cones and planes) sampled on a
dense ball of rays and then decimated, so the triangles go where the form bends; skull and shoulders are shaded with the
fields' own normals; horns and neck are swept rings. The paint of the logo (site/brand/shadowman.svg: black cap, brow, eye, circuit traces, red
eye, grin, collar) is cut into the mesh along the logo's outlines, in the logo's own coordinates as seen from the front: on
the pale half it is a material on the skull's own faces, the glowing eye and grin are copies of the faces under them, lifted
off the skin. Plain PBR materials, no textures, no armature.

Nodes in the glb (Godot axes: x right, y up, he faces +z; origin at the centre of the base):
    Body          shoulders, neck base, hood lying on them, grey collar band (static)
    Head          skull, face, ears, horns, neck; pivot at the top of the neck
      Eye         glowing red eye, material EyeGlow (nothing else uses it)
        Pupil     black slit
      Grin        red grin, material GrinGlow
"""
import math
from pathlib import Path

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Vector

OUT = Path(__file__).resolve().parents[1] / "godot" / "assets" / "shadowman.glb"
U = 0.72 / 586.0  # one unit of the logo in metres: the logo's bust is 586 units tall, ours 0.72
PIVOT = Vector((0.0, 0.035, 0.27))  # top of the neck (Blender axes: x right, -y front, z up)
NECK_R = 0.075
HEAD_TRIS, EAR_TRIS, BODY_TRIS = 8000, 240, 2000
LIFT = 0.0015  # the glowing eye and grin float this far above the skin, enough not to z-fight


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


def plane(p, a, b, c):
    """Distance above the plane through three points (positive on the side of (b-a) x (c-a))."""
    a = np.asarray(a, float)
    n = np.cross(np.asarray(b) - a, np.asarray(c) - a)
    return (p - a) @ (n / np.linalg.norm(n))


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


def head_sdf(p):
    """The skull and face, symmetric in x. Front of the face is at y = -0.18, chin z = 0.155, crown z = 0.625."""
    p = np.column_stack([np.abs(p[:, 0]), p[:, 1], p[:, 2]])
    d = ell(p, (0, 0.012, 0.468), (0.158, 0.185, 0.157))  # cranium
    d = smin(d, ell(p, (0, -0.062, 0.335), (0.126, 0.118, 0.172)), 0.05)  # face
    d = smin(d, ell(p, (0, -0.075, 0.245), (0.104, 0.106, 0.094)), 0.045)  # jaw
    d = smin(d, ell(p, (0, -0.170, 0.186), (0.036, 0.026, 0.030)), 0.03)  # chin
    d = smin(d, ell(p, (0.100, -0.110, 0.348), (0.034, 0.042, 0.022)), 0.04)  # cheekbone
    d = smin(d, cone(p, (0.112, -0.090, 0.350), (0.140, -0.02, 0.362), 0.010, 0.006), 0.04)  # and its arch back to the ear
    d = smin(d, cone(p, (0.112, 0.0, 0.287), (0.030, -0.155, 0.176), 0.018, 0.016), 0.025)  # jawline
    d = smax(d, -ell(p, (0.110, -0.146, 0.268), (0.022, 0.045, 0.050)), 0.05)  # hollow cheek
    d = smin(d, ell(p, (0, -0.150, 0.230), (0.058, 0.042, 0.034)), 0.03)  # muzzle
    ridge = [(np.array([[(1 - t) ** 2, 2 * t * (1 - t), t * t]]) @ [(0, -0.172, 0.426), (0.090, -0.195, 0.452), (0.140, -0.065, 0.452)])[0] for t in np.linspace(0, 1, 7)]
    brow = np.min([cone(p, a, b, 0.018 - 0.013 * i / 6, 0.018 - 0.013 * (i + 1) / 6) for i, (a, b) in enumerate(zip(ridge, ridge[1:]))], axis=0)
    d = smin(d, brow, 0.022)  # brow ridge, low in the middle: angry
    d = smax(d, -ell(p, (0.072, -0.190, 0.390), (0.042, 0.045, 0.024)), 0.016)  # eye socket
    d = smin(d, ell(p, (0.072, -0.146, 0.389), (0.032, 0.016, 0.020)), 0.006)  # lid
    nose = cone(p, (0, -0.170, 0.415), (0, -0.238, 0.318), 0.009, 0.010)  # bridge
    nose = smin(nose, ell(p, (0, -0.208, 0.375), (0.009, 0.012, 0.020)), 0.012)  # hump
    nose = smin(nose, ell(p, (0, -0.238, 0.313), (0.011, 0.014, 0.012)), 0.010)  # tip
    nose = smin(nose, ell(p, (0.019, -0.203, 0.300), (0.010, 0.016, 0.010)), 0.010)  # wing
    d = smin(d, nose, 0.018)
    d = smin(d, ell(p, (0, -0.182, 0.207), (0.042, 0.012, 0.008)), 0.016)  # lower lip
    lips = ((0, -0.201, 0.2182), (0.0406, -0.188, 0.2193), (0.0615, -0.174, 0.2268), (0.081, -0.158, 0.236))  # under the painted mouth line
    d = smax(d, -np.min([cone(p, a, b, 0.004, 0.004) for a, b in zip(lips, lips[1:])], axis=0), 0.010)  # mouth
    d = smax(d, -ell(p, (0, -0.199, 0.203), (0.030, 0.012, 0.008)), 0.012)  # crease above the chin
    d = smax(d, -cone(p, (0.040, -0.196, 0.292), (0.074, -0.168, 0.236), 0.005, 0.004), 0.014)  # fold from nose to mouth corner
    # hard jaw: one plane under each side, meeting in a keel from the chin back to the throat
    d = smax(d, plane(p, (0, -0.165, 0.158), (0, -0.03, 0.205), (0.118, 0.01, 0.275)), 0.014)
    return d


EAR_AXIS = np.array([0.030, 0.050, 0.092])  # from the root of the ear to its point: up, back and out
EAR_FLAT = np.cross(EAR_AXIS, [0.5, 0.85, 0.0]) / np.linalg.norm(np.cross(EAR_AXIS, [0.5, 0.85, 0.0]))


def ear_sdf(p):
    """A pointed ear, a flat blade standing off the side of the skull (both sides at once)."""
    p = np.column_stack([np.abs(p[:, 0]), p[:, 1], p[:, 2]])
    root = np.array([0.150, 0.034, 0.385])
    d = smin(ell(p, (0.148, 0.030, 0.368), (0.020, 0.024, 0.036)), cone(p, root, root + EAR_AXIS, 0.022, 0.002), 0.015)
    d = smax(d, np.abs((p - root) @ EAR_FLAT) - 0.0045, 0.004)
    return d


def cowl_path():
    """Centre line and radius of the hood's roll: a loop round the neck, high and thick behind, down to a V on the chest."""
    out = []
    for a in np.linspace(-180, 180, 41):
        if abs(a) <= 90:
            x, y = 0.125 * math.sin(math.radians(a)), PIVOT.y + 0.125 * math.cos(math.radians(a))
            z, r = 0.195 - 0.010 * abs(a) / 90, 0.048 - 0.008 * abs(a) / 90
        else:
            t = (abs(a) - 90) / 90
            x, y = math.copysign(0.125 * (1 - t) ** 0.9, a), PIVOT.y - 0.170 * math.sin(t * math.pi / 2) ** 1.2
            z, r = 0.185 - 0.100 * t ** 1.3, 0.040 + (0.012 - 0.040) * t
        out.append(((x, y, z), r * (1 + 0.07 * math.cos(math.radians(a * 4)))))  # bunched cloth
    return out


COWL = cowl_path()


def body_sdf(p):
    """Shoulders of the hooded top with the hood lying on them, cut flat at z = 0."""
    q = np.column_stack([np.abs(p[:, 0]), p[:, 1], p[:, 2]])
    d = ell(p, (0, 0.005, -0.04), (0.300, 0.165, 0.200))  # chest and back
    d = smin(d, ell(q, (0.200, 0.005, 0.030), (0.105, 0.110, 0.095)), 0.06)  # shoulder caps
    d = smin(d, cone(q, (0, PIVOT.y, 0.165), (0.170, 0.020, 0.100), 0.05, 0.05), 0.06)  # slope from neck to shoulder
    d = smin(d, ell(p, (0, PIVOT.y, 0.09), (0.115, 0.110, 0.100)), 0.04)  # neck base
    roll = np.min([cone(p, a, b, ra, rb) for (a, ra), (b, rb) in zip(COWL, COWL[1:])], axis=0)
    d = smin(d, roll, 0.018)
    d = smin(d, ell(p, (0, 0.170, 0.110), (0.110, 0.055, 0.100)), 0.03)  # the hood's bag down the back
    return d


# ---------- meshing ----------

def ray_ball(n):
    """Directions of a cube sphere with n x n quads per face, evenly spread, and its quads."""
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=2.0)
    bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=n - 1, use_grid_fill=True)
    dirs = np.array([v.co[:] for v in bm.verts])
    dirs[np.abs(dirs) < 1e-6] = 0.0
    dirs = np.tan(dirs * (math.pi / 4))
    dirs /= np.linalg.norm(dirs, axis=1)[:, None]
    quads = [[v.index for v in f.verts] for f in bm.faces]
    bm.free()
    return dirs, quads


def cast(sdf, origin, dirs, reach, steps=80):
    """The outermost point of the solid on every ray from origin."""
    origin = np.broadcast_to(np.asarray(origin, float), dirs.shape)
    lo, hi = np.zeros(len(dirs)), np.full(len(dirs), reach)
    todo = np.arange(len(dirs))
    for t in np.linspace(reach, 0.0, steps):
        if not len(todo):
            break
        inside = sdf(origin[todo] + dirs[todo] * t) < 0
        lo[todo[inside]] = t
        hi[todo[~inside]] = t
        todo = todo[~inside]
    for _ in range(24):
        mid = (lo + hi) / 2
        inside = sdf(origin + dirs * mid[:, None]) < 0
        lo, hi = np.where(inside, mid, lo), np.where(inside, hi, mid)
    return origin + dirs * ((lo + hi) / 2)[:, None]


def shell(sdf, centre, n, reach, keep=lambda d: d[:, 0] > -2):
    """The surface of the solid as quads, only where keep(direction) is true for all four corners."""
    dirs, quads = ray_ball(n)
    ok = keep(dirs)
    quads = [q for q in quads if all(ok[i] for i in q)]
    used = sorted({i for q in quads for i in q})
    remap = {old: new for new, old in enumerate(used)}
    return cast(sdf, centre, dirs[used], reach), [[remap[i] for i in q] for q in quads]


def decimate(verts, faces, tris):
    """Collapses to about `tris` triangles: the mesh keeps its triangles where the form bends."""
    me = bpy.data.meshes.new("dense")
    me.from_pydata(verts.tolist(), [], faces)
    ob = bpy.data.objects.new("dense", me)
    bpy.context.collection.objects.link(ob)
    mod = ob.modifiers.new("decimate", "DECIMATE")
    mod.ratio = tris / (2.0 * len(faces))
    mod.use_collapse_triangulate = True
    out = bpy.data.meshes.new_from_object(ob.evaluated_get(bpy.context.evaluated_depsgraph_get()))
    v = np.array([p.co[:] for p in out.vertices])
    f = [list(p.vertices) for p in out.polygons]
    bpy.data.objects.remove(ob)
    bpy.data.meshes.remove(me)
    bpy.data.meshes.remove(out)
    return v, f


def rim(faces):
    """Indices of the vertices on an open edge."""
    count = {}
    for f in faces:
        for a, b in zip(f, f[1:] + f[:1]):
            count[(min(a, b), max(a, b))] = count.get((min(a, b), max(a, b)), 0) + 1
    return sorted({i for e, c in count.items() if c == 1 for i in e})


def mirrored(verts, faces):
    """A right half (x >= 0, open on x = 0) made whole: the seam stays an edge loop on the centre plane."""
    verts = verts.copy()
    seam = np.zeros(len(verts), bool)
    seam[rim(faces)] = True
    verts[seam, 0] = 0.0
    other = np.where(seam, np.arange(len(verts)), len(verts) + np.cumsum(~seam) - 1)
    left = verts[~seam] * np.array([-1.0, 1.0, 1.0])
    return np.vstack([verts, left]), faces + [[int(other[i]) for i in reversed(f)] for f in faces]


def new_bm():
    bm = bmesh.new()
    bm.verts.layers.int.new("skin")  # 1 where a vertex lies on a field surface and takes its normal from the field
    return bm


def emit(bm, verts, faces, mat, skin=False):
    """Adds faces to the bmesh; mat is one material index or one per face."""
    vs = [bm.verts.new(v) for v in verts]
    for v in vs:
        v[bm.verts.layers.int["skin"]] = int(skin)
    for k, f in enumerate(faces):
        face = bm.faces.new([vs[i] for i in f])
        face.material_index = mat if isinstance(mat, int) else mat[k]
        face.smooth = True
    return vs


def tube(points, radii, segs, mats, tip=True):
    """Rings around a polyline, frames carried along without twist; mats[i] paints the band after ring i."""
    points = [Vector(p) for p in points]
    side = Vector((0, -1, 0))
    verts, faces, face_mats = [], [], []
    for i, (c, r) in enumerate(zip(points, radii)):
        t = (points[min(i + 1, len(points) - 1)] - points[max(i - 1, 0)]).normalized()
        side = (side - t * side.dot(t)).normalized()
        up = t.cross(side)
        verts += [c + (side * math.cos(a) + up * math.sin(a)) * r for a in (2 * math.pi * k / segs for k in range(segs))]
    for i in range(len(points) - 1):
        for k in range(segs):
            a, b = i * segs + k, i * segs + (k + 1) % segs
            faces.append([a, b, b + segs, a + segs])
            face_mats.append(mats[i])
    if tip:
        verts.append(points[-1] + (points[-1] - points[-2]).normalized() * radii[-1] * 2.5)
        base = (len(points) - 1) * segs
        for k in range(segs):
            faces.append([base + k, base + (k + 1) % segs, len(verts) - 1])
            face_mats.append(mats[-1])
    return verts, faces, face_mats


# ---------- paint: closed outlines in logo coordinates, cut into the skull as seen from the front ----------

def cubic(p0, p1, p2, p3, n=8):
    t = np.linspace(0, 1, n + 1)[:, None]
    return (1 - t) ** 3 * np.array(p0) + 3 * (1 - t) ** 2 * t * np.array(p1) + 3 * (1 - t) * t * t * np.array(p2) + t ** 3 * np.array(p3)


def between(top, bot):
    """The outline of the area between two curves that run the same way."""
    return [tuple(q) for q in top] + [tuple(q) for q in bot][::-1]


def stroke(pts, width):
    """The outline of a line of constant width along a polyline, corners mitred."""
    pts = np.asarray(pts, float)
    seg = np.diff(pts, axis=0)
    seg /= np.linalg.norm(seg, axis=1)[:, None]
    nrm = np.column_stack([-seg[:, 1], seg[:, 0]])
    side = np.vstack([nrm[:1], nrm[:-1] + nrm[1:], nrm[-1:]])
    side /= np.linalg.norm(side, axis=1)[:, None]
    mitre = np.concatenate([[1.0], 1.0 / np.maximum(0.5, np.einsum("ij,ij->i", side[1:-1], nrm[:-1])), [1.0]])
    off = side * (mitre * width / 2)[:, None]
    return between(pts + off, pts - off)


def disc(x, y, r, n=10):
    return [(x + r * math.cos(2 * math.pi * k / n), y + r * math.sin(2 * math.pi * k / n)) for k in range(n)]


def at(curve, xs):
    """Logo y of a curve at the given x (the curve must run left to right), as points."""
    return np.column_stack([xs, np.interp(xs, curve[:, 0], curve[:, 1])])


def region(bm, outline, front=True):
    """Cuts a closed logo outline into the mesh, along y so that it is exact seen from the front, and returns the faces
    inside it. front: only what faces the viewer; otherwise right through to the back of the head."""
    # shortcut: projected straight along y, so paint near the silhouette stretches when seen from the side; fine for the
    # 25 degrees he turns, project along the surface normal instead if the camera ever goes round him
    poly = [((x - 300.0) * U, (600.0 - y) * U) for x, y in outline]

    def touches(face, b):
        xs, zs = [v.co.x for v in face.verts], [v.co.z for v in face.verts]
        return max(xs) > b[0] and min(xs) < b[1] and max(zs) > b[2] and min(zs) < b[3]

    def crossed(face, a, b):
        """Does the segment a-b (not just its line) pass through the face, seen from the front?"""
        d = (b[0] - a[0], b[1] - a[1])
        side = [(v.co.x - a[0]) * d[1] - (v.co.z - a[1]) * d[0] for v in face.verts]
        along = [((v.co.x - a[0]) * d[0] + (v.co.z - a[1]) * d[1]) / (d[0] ** 2 + d[1] ** 2) for v in face.verts]
        ts = [t0 + (t1 - t0) * s0 / (s0 - s1) for s0, s1, t0, t1 in zip(side, side[1:] + side[:1], along, along[1:] + along[:1]) if (s0 > 0) != (s1 > 0)]
        return bool(ts) and min(ts) < 1.02 and max(ts) > -0.02

    def inside(c):
        hit = False
        for (x0, z0), (x1, z1) in zip(poly, poly[1:] + poly[:1]):
            if (z0 > c.z) != (z1 > c.z) and c.x < x0 + (c.z - z0) * (x1 - x0) / (z1 - z0):
                hit = not hit
        return hit

    bm.normal_update()
    xs, zs = zip(*poly)
    whole = (min(xs), max(xs), min(zs), max(zs))
    local = {f for f in bm.faces if touches(f, whole) and (not front or (f.normal.y < 0.3 and f.calc_center_median().y < 0.04))}
    for a, b in zip(poly, poly[1:] + poly[:1]):
        if math.dist(a, b) < 1e-6:
            continue
        faces = [f for f in local if f.is_valid and crossed(f, a, b)]
        if not faces:
            continue
        geom = faces + list({e for f in faces for e in f.edges}) + list({v for f in faces for v in f.verts})
        cut = bmesh.ops.bisect_plane(bm, geom=geom, dist=1e-5, plane_co=(a[0], 0, a[1]), plane_no=(b[1] - a[1], 0, a[0] - b[0]))
        local |= {g for g in cut["geom"] if isinstance(g, bmesh.types.BMFace)}
    return [f for f in local if f.is_valid and inside(f.calc_center_median())]


def lifted(faces, lift):
    """A copy of skull faces floated above the skin, as (verts, faces)."""
    ids = {}
    for f in faces:
        for v in f.verts:
            ids.setdefault(v, len(ids))
    co = np.array([v.co[:] for v in ids])
    return co + gradient(head_sdf, co) * lift, [[ids[v] for v in f.verts] for f in faces]


def on_face(sx, sy):
    """The point of the face behind a logo point, and the normal there."""
    p = cast(head_sdf, ((sx - 300.0) * U, 0.02, (600.0 - sy) * U), np.array([[0.0, -1.0, 0.0]]), 0.30)
    return Vector(p[0]), Vector(gradient(head_sdf, p)[0])


def stud(sx, sy, radius, height, segs=10):
    """A small dome sitting on the face at a logo point, as (verts, faces)."""
    c, n = on_face(sx, sy)
    u = n.cross(Vector((0, 0, 1))).normalized()
    v = u.cross(n)
    verts = [c + (u * math.cos(a) + v * math.sin(a)) * radius * U * s + n * height * h
             for s, h in ((1.0, 0.0), (0.72, 0.7)) for a in (2 * math.pi * k / segs for k in range(segs))] + [c + n * height]
    faces = [[segs + k, segs + (k + 1) % segs, (k + 1) % segs, k] for k in range(segs)]
    return verts, faces + [[2 * segs, segs + (k + 1) % segs, segs + k] for k in range(segs)]


# ---------- materials ----------

def linear(hex_colour):
    c = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    return [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c] + [1.0]


def material(name, colour, rough, glow=None):
    m = bpy.data.materials.new(name)
    p = m.node_tree.nodes["Principled BSDF"]
    p.inputs["Base Color"].default_value = linear(colour)
    p.inputs["Roughness"].default_value = rough
    p.inputs["Metallic"].default_value = 0.0
    if glow:
        p.inputs["Emission Color"].default_value = linear(glow)
        p.inputs["Emission Strength"].default_value = 1.0
    return m


def finish(name, bm, mats, origin=Vector(), parent=None, field=None):
    """Turns a bmesh (in world coordinates) into an object with its origin at `origin`."""
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    for m in mats:
        me.materials.append(m)
    if field:  # shade the decimated surface with the field's own normals
        normals = np.array([v.normal[:] for v in me.vertices])
        skin = np.array([a.value for a in me.attributes["skin"].data], bool)
        normals[skin] = gradient(field, np.array([v.co[:] for v in me.vertices])[skin])
        me.normals_split_custom_set_from_vertices(normals.tolist())
    me.attributes.remove(me.attributes["skin"])
    me.transform(Matrix.Translation(-origin))
    ob = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(ob)
    ob.parent = parent
    ob.location = origin
    while parent:  # locations are relative to the parent; nothing here is rotated or scaled
        ob.location -= parent.location
        parent = parent.parent
    return ob


# ---------- the bust ----------

def build_head(M):
    mats = [M["Bone"], M["Shadow"], M["Ink"], M["NodeGlow"]]
    BONE, SHADOW, INK, NODE = range(4)

    # skull: his left half from the field, decimated, mirrored so the paint split is an edge loop on x = 0
    v, f = shell(head_sdf, (0, -0.03, 0.39), 72, 0.34, lambda d: d[:, 0] > -1e-6)
    v, f = mirrored(*decimate(v, f, HEAD_TRIS // 2))
    hidden = (np.hypot(v[:, 0], v[:, 1] - PIVOT.y) < NECK_R - 0.012) & (v[:, 2] < 0.36)  # inside the neck
    f = [t for t in f if not all(hidden[i] for i in t)]
    bm = new_bm()
    emit(bm, v, f, BONE, skin=True)
    bmesh.ops.delete(bm, geom=[w for w in bm.verts if not w.link_faces], context="VERTS")
    for face in bm.faces:
        face.material_index = SHADOW if face.calc_center_median().x > 0 else BONE

    def paint(outline, mat, front=True):
        for face in region(bm, outline, front):
            face.material_index = mat

    # the pale half (viewer's left), in the logo's coordinates and its order of painting
    paint([(300, 172), (277.5, 150.75), (252, 146), (210, 162), (173, 198), (100, 198), (100, -50), (300, -50)], SHADOW, front=False)  # black cap with a widow's peak
    for line in (cubic((214, 132), (236, 116), (262, 108), (292, 110)), cubic((200, 152), (224, 132), (256, 124), (290, 128)), cubic((192, 172), (210, 154), (232, 144), (256, 140))):
        paint(stroke(line, 3.0), BONE)  # the pale lines over the brow
    paint([(300, 266), (276, 350), (300, 368)], SHADOW)  # the pale side of the nose belongs to the dark
    paint(between(cubic((176, 232), (202, 218), (244, 230), (294, 262)), cubic((178, 250), (206, 244), (246, 252), (294, 276))), INK)  # brow
    low = np.vstack([cubic((184, 254), (184, 272), (190, 290), (202, 298), 5), cubic((202, 298), (234, 314), (280, 308), (290, 282))[1:]])
    paint(between(cubic((184, 254), (210, 250), (252, 264), (290, 282)), low), INK)  # the dark eye under its heavy lid ...
    slit = cubic((204, 280), (226, 290), (250, 292), (274, 286))
    gap = np.column_stack([slit[:, 0] * 0, 1.9 * np.sin(np.pi * np.linspace(0, 1, len(slit))) ** 0.6])
    paint(between(slit - gap, slit + gap), BONE)  # ... with a pale slit
    paint(between(cubic((234, 408), (258, 416), (280, 418), (300, 418)), cubic((234, 408), (254, 424), (276, 428), (300, 427))), INK)  # mouth
    nodes = (([(176, 206), (196, 206), (210, 220), (210, 236)], (176, 206), 4.5),
             ([(190, 318), (206, 318), (216, 330), (216, 352), (230, 366)], (230, 366), 4.5),
             ([(196, 190), (196, 177)], (196, 176), 3.5))
    for trace, node, r in nodes:  # circuit traces, each ending in a yellow node
        paint(stroke(trace, 3.0), INK)
        paint(disc(*node, r + 2.0), INK)

    # the dark half: outlines of the glowing eye, its slit pupil and the grin, cut in now and lifted off below
    pupil = [(346, 270), (358, 264), (354, 292), (344, 292)]
    region(bm, pupil)
    low = np.vstack([cubic((316, 280), (318, 288), (324, 294), (334, 296), 4), cubic((334, 296), (362, 296), (384, 276), (392, 246))[1:]])
    eye = lifted(region(bm, between(cubic((316, 280), (340, 276), (366, 264), (392, 246)), low)), LIFT)
    pupil = lifted(region(bm, pupil), LIFT * 2)
    top = cubic((300, 422), (332, 422), (362, 408), (388, 372), 40)
    low = np.vstack([cubic((388, 372), (384, 392), (366, 418), (336, 430), 30), cubic((336, 430), (322, 434), (310, 434), (300, 432), 20)])[::-1]
    ticks, half = (319.5, 337.5, 355.5), 3.4  # the black ticks of the logo, cut as notches: jagged teeth
    xs = np.unique(np.concatenate([np.linspace(300, 387.5, 12)] + [[c - half, c, c + half] for c in ticks]))
    notch = np.max([np.clip(1 - np.abs(xs - c) / half, 0, 1) for c in ticks], axis=0) * 0.92
    upper, lower = at(top, xs), at(low, xs)
    lower[:, 1] -= notch * (lower[:, 1] - upper[:, 1])
    grin = lifted(region(bm, between(upper, lower)), LIFT)
    bmesh.ops.dissolve_limit(bm, angle_limit=math.radians(0.5), verts=bm.verts[:], edges=bm.edges[:], delimit={"MATERIAL"})  # cuts that part nothing
    for w in bm.verts:
        w[bm.verts.layers.int["skin"]] = 1  # the cuts made new vertices

    for trace, node, r in nodes:
        emit(bm, *stud(*node, r, 0.0035), NODE)

    # neck: round about the pivot's axis, so it can turn inside the collar without a gap
    rings = [(0.06, 0.080), (0.13, 0.076), (0.19, 0.074), (0.25, 0.075), (0.31, 0.078), (0.35, 0.080)]
    nv, nf, _ = tube([(0, PIVOT.y, z) for z, _ in rings], [r for _, r in rings], 20, [0] * len(rings), tip=False)
    emit(bm, nv, nf, [BONE if sum(nv[i].x for i in face) < 0 else SHADOW for face in nf])

    # ears: their own small field, meshed from inside the blade
    for sign, mat in ((-1, BONE), (1, SHADOW)):
        emit(bm, *decimate(*shell(ear_sdf, (sign * 0.162, 0.054, 0.425), 14, 0.09), EAR_TRIS), mat)

    # horns: out of the top corners of the skull, sweeping out, then up and in; ridged like stacked rings
    ctrl = [np.array(c) for c in ((0.076, 0.012, 0.583), (0.140, 0.022, 0.615), (0.207, 0.036, 0.655), (0.188, 0.000, 0.716))]
    ts, scale, groove = [0.0], [1.0], [False]
    ridges = 7
    for j in range(ridges):
        for u_, s_ in ((0.0, 0.86), (0.2, 0.86), (0.4, 1.05), (0.75, 1.0)):
            ts.append(0.07 + 0.75 * (j + u_) / ridges)
            scale.append(s_)
            groove.append(u_ == 0.0 and j in (2, 4, 5))  # the three black rings of the logo
    ts += [0.86, 0.92, 0.97]
    scale += [0.95, 0.95, 0.95]
    groove += [False] * 3
    for sign in (-1, 1):
        pts = [(1 - t) ** 3 * ctrl[0] + 3 * (1 - t) ** 2 * t * ctrl[1] + 3 * (1 - t) * t * t * ctrl[2] + t ** 3 * ctrl[3] for t in ts]
        pts = [(sign * p[0], p[1], p[2]) for p in pts]
        radii = [(0.0285 * (1 - t) ** 0.85 + 0.0012) * s_ for t, s_ in zip(ts, scale)]
        emit(bm, *tube(pts, radii, 10, [INK if g and sign < 0 else (BONE if sign < 0 else SHADOW) for g in groove]))

    head = finish("Head", bm, mats, PIVOT, field=head_sdf)

    # the glow of the dark half: its own objects, so the game can drive their emission
    eye_centre = on_face(352, 274)[0]
    bm = new_bm()
    emit(bm, *eye, 0, skin=True)
    eye = finish("Eye", bm, [M["EyeGlow"]], eye_centre, head, head_sdf)
    bm = new_bm()
    emit(bm, *pupil, 0, skin=True)
    finish("Pupil", bm, [M["Ink"]], eye_centre, eye, head_sdf)
    bm = new_bm()
    emit(bm, *grin, 0, skin=True)
    finish("Grin", bm, [M["GrinGlow"]], on_face(340, 418)[0], head, head_sdf)
    return head


def build_body(M):
    mats = [M["Cloth"], M["Collar"]]
    CLOTH, COLLAR = range(2)
    v, f = decimate(*shell(body_sdf, (0, 0, 0), 48, 0.42, lambda d: d[:, 2] > -1e-6), BODY_TRIS)
    v[rim(f), 2] = 0.0
    bm = new_bm()
    emit(bm, v, f, CLOTH, skin=True)
    for face in bmesh.ops.contextual_create(bm, geom=[e for e in bm.edges if e.is_boundary])["faces"]:  # the base
        face.material_index = CLOTH
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])

    # collar: the grey V of the logo on the pale side, painted where the logo has it (the dark side stays cloth)
    band = between(cubic((226, 418), (232, 452), (258, 480), (300, 500)), cubic((196, 436), (214, 480), (250, 520), (300, 540)))
    for face in region(bm, band):
        face.material_index = COLLAR
    return finish("Body", bm, mats, field=body_sdf)


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    M = {
        "Bone": material("Bone", "#d8d3c7", 0.55),
        "Shadow": material("Shadow", "#0b0c0f", 0.36),
        "Ink": material("Ink", "#07080a", 0.60),
        "NodeGlow": material("NodeGlow", "#3a3400", 0.5, "#fcee0a"),
        "EyeGlow": material("EyeGlow", "#3a0508", 0.4, "#ff1e2d"),
        "GrinGlow": material("GrinGlow", "#3a0508", 0.4, "#ff1e2d"),
        "Cloth": material("Cloth", "#1c1f24", 0.85),
        "Collar": material("Collar", "#9a968c", 0.80),
    }
    build_body(M)
    build_head(M)
    check()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.export_scene.gltf(filepath=str(OUT), export_format="GLB", use_selection=True, export_apply=True)
    print("exported", OUT, OUT.stat().st_size, "bytes")


def check():
    """What the game relies on: the nodes, their parents, the private glow materials, the size and the budget."""
    bpy.context.view_layer.update()
    ob = bpy.data.objects
    assert [o.parent and o.parent.name for o in (ob["Body"], ob["Head"], ob["Eye"], ob["Pupil"], ob["Grin"])] == [None, None, "Head", "Eye", "Head"]
    for glow, owner in (("EyeGlow", "Eye"), ("GrinGlow", "Grin")):
        assert [o.name for o in ob if glow in o.data.materials] == [owner], glow
    tris = sum(len(p.vertices) - 2 for o in ob for p in o.data.polygons)
    pts = np.array([o.matrix_world @ v.co for o in ob for v in o.data.vertices])
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    print(f"triangles {tris}; width {hi[0] - lo[0]:.3f}, height {hi[2] - lo[2]:.3f}, depth {hi[1] - lo[1]:.3f}, base z {lo[2]:.4f}")
    assert tris < 15000, tris
    assert abs(lo[2]) < 1e-4 and 0.69 < hi[2] < 0.73 and 0.58 < hi[0] - lo[0] < 0.64 and abs(hi[0] + lo[0]) < 0.004


main()
