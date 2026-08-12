bl_info = {
    "name": "Rubik's Cube Easy Animator",
    "author": "OpenAI",
    "version": (0, 2, 0),
    "blender": (4, 0, 0),
    "location": "3D View > Sidebar > Rubik",
    "description": "One-click Rubik's Cube creation, assembly animation, manual turns, scramble, and solving",
    "category": "Animation",
}

import bpy
import math
import random
from mathutils import Matrix, Vector
from bpy.props import BoolProperty, EnumProperty, FloatVectorProperty, IntProperty, StringProperty

# ============================================================
# MODEL SETTINGS
# ============================================================

CUBIE_SIZE = 2.0
GAP = 0.12
SPACING = CUBIE_SIZE + GAP

STICKER_SIZE = 1.68
STICKER_THICKNESS = 0.08
STICKER_OFFSET = 0.015

CUBIE_BEVEL = 0.14
STICKER_BEVEL = 0.025

FACE_COLORS = {
    "+X": (0.80, 0.02, 0.02),   # red
    "-X": (1.00, 0.22, 0.02),   # orange
    "+Y": (0.02, 0.55, 0.08),   # green
    "-Y": (0.02, 0.12, 0.80),   # blue
    "+Z": (0.95, 0.95, 0.95),   # white
    "-Z": (1.00, 0.78, 0.02),   # yellow
}

# +X=R, -X=L, +Z=U, -Z=D, -Y=F, +Y=B
# tuple: axis, layer, clockwise-quarter sign
MOVE_DEFS = {
    "U": ("Z", +1, -1),
    "D": ("Z", -1, +1),
    "R": ("X", +1, -1),
    "L": ("X", -1, +1),
    "F": ("Y", -1, +1),
    "B": ("Y", +1, -1),
}
AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2}


# ============================================================
# GENERIC HELPERS
# ============================================================

def get_history(scene):
    return [m for m in scene.rc_history.split() if m]


def set_history(scene, moves):
    scene.rc_history = " ".join(moves)


def append_history(scene, move):
    history = get_history(scene)
    history.append(move)
    set_history(scene, history)


def parse_move(move):
    face = move[0].upper()
    if face not in MOVE_DEFS:
        raise ValueError(f"Unsupported move: {move}")

    amount = 1
    suffix = move[1:]
    if suffix == "'":
        amount = -1
    elif suffix == "2":
        amount = 2
    elif suffix:
        raise ValueError(f"Unsupported move suffix: {move}")
    return face, amount


def inverse_move(move):
    face, amount = parse_move(move)
    if abs(amount) == 2:
        return face + "2"
    return face + ("'" if amount == 1 else "")


def simplify_moves(moves):
    stack = []
    for move in moves:
        face, amount = parse_move(move)
        q = amount % 4
        if stack and stack[-1][0] == face:
            old_face, old_q = stack.pop()
            q = (old_q + q) % 4
            if q:
                stack.append((face, q))
        else:
            stack.append((face, q))

    out = []
    for face, q in stack:
        if q == 1:
            out.append(face)
        elif q == 2:
            out.append(face + "2")
        elif q == 3:
            out.append(face + "'")
    return out


def rotate_grid_position(pos, axis, angle):
    v = Vector((float(pos[0]), float(pos[1]), float(pos[2])))
    rot3 = Matrix.Rotation(angle, 3, axis)
    out = rot3 @ v
    return [int(round(out.x)), int(round(out.y)), int(round(out.z))]


def rubik_collection():
    return bpy.data.collections.get("RubiksCube")


def rubik_root():
    return bpy.data.objects.get("RubiksCube_ROOT")


def rubik_cubies():
    collection = rubik_collection()
    if collection is None:
        return []
    return [obj for obj in collection.objects if obj.get("rc_is_cubie", False)]


def remove_existing_cube():
    collection = rubik_collection()
    if collection is None:
        return

    # Delete only objects belonging to our RubiksCube collection.
    # Other scene objects are untouched.
    for obj in list(collection.objects):
        bpy.data.objects.remove(obj, do_unlink=True)

    bpy.data.collections.remove(collection)


def make_material(name, color, roughness=0.32, metallic=0.0):
    material = bpy.data.materials.get(name)
    if material is None:
        material = bpy.data.materials.new(name=name)

    material.use_nodes = True
    material.diffuse_color = (*color, 1.0)

    principled = material.node_tree.nodes.get("Principled BSDF")
    if principled is not None:
        principled.inputs["Base Color"].default_value = (*color, 1.0)
        principled.inputs["Roughness"].default_value = roughness
        principled.inputs["Metallic"].default_value = metallic
    return material


def move_to_collection(obj, collection):
    for source_collection in list(obj.users_collection):
        source_collection.objects.unlink(obj)
    collection.objects.link(obj)


def parent_keep_world(obj, parent):
    world = obj.matrix_world.copy()
    obj.parent = parent
    obj.matrix_parent_inverse = parent.matrix_world.inverted()
    obj.matrix_world = world


def add_box(name, location, dimensions, material, bevel_width, collection, parent=None):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=location)
    obj = bpy.context.object
    obj.name = name
    obj.dimensions = dimensions

    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

    if material is not None:
        obj.data.materials.append(material)

    bevel = obj.modifiers.new(name="Rounded Edges", type="BEVEL")
    bevel.width = bevel_width
    bevel.segments = 4
    bevel.limit_method = "ANGLE"

    move_to_collection(obj, collection)
    if parent is not None:
        parent_keep_world(obj, parent)
    return obj


def point_at(obj, target=(0.0, 0.0, 0.0)):
    direction = Vector(target) - obj.location
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def set_key_interpolation(obj, data_paths, interpolation="BEZIER"):
    if not obj.animation_data or not obj.animation_data.action:
        return
    for fcurve in obj.animation_data.action.fcurves:
        if fcurve.data_path in data_paths:
            for point in fcurve.keyframe_points:
                point.interpolation = interpolation


def try_play_animation(context, start_frame, end_frame):
    scene = context.scene
    scene.frame_start = min(scene.frame_start, start_frame)
    scene.frame_end = max(scene.frame_end, end_frame)
    scene.frame_set(start_frame)
    try:
        if not context.screen.is_animation_playing:
            bpy.ops.screen.animation_play()
    except Exception:
        pass


# ============================================================
# CUBE CREATION + BUILD ANIMATION
# ============================================================

def cubie_for_sticker(face, a, b, cubie_map):
    if face == "+X":
        key = (1, a, b)
    elif face == "-X":
        key = (-1, a, b)
    elif face == "+Y":
        key = (a, 1, b)
    elif face == "-Y":
        key = (a, -1, b)
    elif face == "+Z":
        key = (a, b, 1)
    elif face == "-Z":
        key = (a, b, -1)
    else:
        raise ValueError(face)
    return cubie_map[key]


def sticker_location_and_dimensions(face, a, b):
    outer_center = SPACING
    surface_offset = CUBIE_SIZE / 2 + STICKER_THICKNESS / 2 + STICKER_OFFSET

    if face == "+X":
        return (
            (outer_center + surface_offset, a * SPACING, b * SPACING),
            (STICKER_THICKNESS, STICKER_SIZE, STICKER_SIZE),
        )
    if face == "-X":
        return (
            (-outer_center - surface_offset, a * SPACING, b * SPACING),
            (STICKER_THICKNESS, STICKER_SIZE, STICKER_SIZE),
        )
    if face == "+Y":
        return (
            (a * SPACING, outer_center + surface_offset, b * SPACING),
            (STICKER_SIZE, STICKER_THICKNESS, STICKER_SIZE),
        )
    if face == "-Y":
        return (
            (a * SPACING, -outer_center - surface_offset, b * SPACING),
            (STICKER_SIZE, STICKER_THICKNESS, STICKER_SIZE),
        )
    if face == "+Z":
        return (
            (a * SPACING, b * SPACING, outer_center + surface_offset),
            (STICKER_SIZE, STICKER_SIZE, STICKER_THICKNESS),
        )
    if face == "-Z":
        return (
            (a * SPACING, b * SPACING, -outer_center - surface_offset),
            (STICKER_SIZE, STICKER_SIZE, STICKER_THICKNESS),
        )
    raise ValueError(face)


def add_studio(collection, materials):
    floor = add_box(
        "Rubik_Floor",
        (0.0, 0.0, -3.35),
        (24.0, 24.0, 0.25),
        materials["floor"],
        0.08,
        collection,
    )
    floor["rc_studio"] = True

    camera_data = bpy.data.cameras.new("Rubik_Camera")
    camera = bpy.data.objects.new("Rubik_Camera", camera_data)
    collection.objects.link(camera)
    camera.location = (10.5, -12.5, 9.0)
    camera.data.lens = 52
    point_at(camera)
    camera["rc_studio"] = True
    bpy.context.scene.camera = camera

    light_specs = [
        ("Rubik_Key", (6.0, -7.0, 10.0), 1300.0, 5.0),
        ("Rubik_Fill", (-7.0, -2.0, 5.0), 850.0, 4.0),
        ("Rubik_Rim", (3.0, 7.0, 8.0), 1000.0, 3.5),
    ]
    for name, location, energy, size in light_specs:
        data = bpy.data.lights.new(name=name, type="AREA")
        data.energy = energy
        data.shape = "DISK"
        data.size = size
        light = bpy.data.objects.new(name, data)
        collection.objects.link(light)
        light.location = location
        point_at(light)
        light["rc_studio"] = True

    scene = bpy.context.scene
    scene.render.resolution_x = 1920
    scene.render.resolution_y = 1080
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = "//rubiks_cube.png"

    try:
        scene.render.engine = "BLENDER_EEVEE_NEXT"
    except Exception:
        pass

    if scene.world is None:
        scene.world = bpy.data.worlds.new("World")
    scene.world.use_nodes = True
    bg = scene.world.node_tree.nodes.get("Background")
    if bg is not None:
        bg.inputs["Color"].default_value = (0.025, 0.025, 0.035, 1.0)
        bg.inputs["Strength"].default_value = 0.25


def assembly_start_position(target, z_layer):
    target = Vector(target)
    if z_layer == -1:
        return target + Vector((0.0, 0.0, -6.0))
    if z_layer == 1:
        return target + Vector((0.0, 0.0, 6.0))

    radial = Vector((target.x, target.y, 0.0))
    if radial.length < 0.01:
        return target + Vector((0.0, -6.0, 0.0))
    radial.normalize()
    return target + radial * 6.0


def animate_assembly(scene, cubie_map):
    scene.frame_start = 1
    section_frames = max(4, scene.rc_build_section_frames)
    gap = max(0, scene.rc_build_gap_frames)

    # Three clear construction sections: bottom, middle, top.
    section_starts = {
        -1: 1,
        0: 1 + section_frames + gap,
        1: 1 + 2 * (section_frames + gap),
    }

    for logical, cubie in cubie_map.items():
        x, y, z = logical
        target = Vector((x * SPACING, y * SPACING, z * SPACING))
        start = assembly_start_position(target, z)
        section_start = section_starts[z]
        end = section_start + section_frames

        cubie.rotation_mode = "QUATERNION"

        cubie.location = start
        cubie.scale = (0.08, 0.08, 0.08)
        cubie.keyframe_insert(data_path="location", frame=max(1, section_start - 1))
        cubie.keyframe_insert(data_path="scale", frame=max(1, section_start - 1))

        cubie.location = target
        cubie.scale = (1.0, 1.0, 1.0)
        cubie.keyframe_insert(data_path="location", frame=end)
        cubie.keyframe_insert(data_path="scale", frame=end)

        set_key_interpolation(cubie, {"location", "scale"}, "BEZIER")

    final_frame = section_starts[1] + section_frames
    scene.rc_build_end_frame = final_frame
    scene.frame_end = max(scene.frame_end, final_frame + 12)
    return final_frame


def create_cube(scene):
    remove_existing_cube()

    collection = bpy.data.collections.new("RubiksCube")
    scene.collection.children.link(collection)

    root = bpy.data.objects.new("RubiksCube_ROOT", None)
    root.empty_display_type = "PLAIN_AXES"
    root.empty_display_size = 1.0
    root["rc_root"] = True
    collection.objects.link(root)

    materials = {
        "black": make_material("M_Cubie_Black", (0.008, 0.008, 0.012), roughness=0.24),
        "floor": make_material("M_Floor", (0.055, 0.060, 0.075), roughness=0.42),
    }
    for face, color in FACE_COLORS.items():
        materials[face] = make_material(f"M_Sticker_{face}", color, roughness=0.25)

    cubie_map = {}
    for x in range(-1, 2):
        for y in range(-1, 2):
            for z in range(-1, 2):
                loc = (x * SPACING, y * SPACING, z * SPACING)
                cubie = add_box(
                    f"Cubie_{x:+d}_{y:+d}_{z:+d}",
                    loc,
                    (CUBIE_SIZE, CUBIE_SIZE, CUBIE_SIZE),
                    materials["black"],
                    CUBIE_BEVEL,
                    collection,
                    parent=root,
                )
                cubie["rc_is_cubie"] = True
                cubie["rc_pos"] = [x, y, z]
                cubie["rc_home"] = [x, y, z]
                cubie.rotation_mode = "QUATERNION"
                cubie_map[(x, y, z)] = cubie

    # Each sticker is parented to the physical cubie it belongs to.
    for a in range(-1, 2):
        for b in range(-1, 2):
            for face in FACE_COLORS:
                location, dimensions = sticker_location_and_dimensions(face, a, b)
                parent = cubie_for_sticker(face, a, b, cubie_map)
                sticker = add_box(
                    f"Sticker_{face}_{a:+d}_{b:+d}",
                    location,
                    dimensions,
                    materials[face],
                    STICKER_BEVEL,
                    collection,
                    parent=parent,
                )
                sticker["rc_is_sticker"] = True
                sticker["rc_face"] = face

    if scene.rc_add_studio:
        add_studio(collection, materials)

    scene.rc_center = (0.0, 0.0, 0.0)
    set_history(scene, [])
    final_frame = animate_assembly(scene, cubie_map)

    bpy.ops.object.select_all(action="DESELECT")
    root.select_set(True)
    bpy.context.view_layer.objects.active = root
    return final_frame


# ============================================================
# TURN ANIMATION
# ============================================================

def keyframe_world_matrix(obj, matrix_world, frame):
    obj.rotation_mode = "QUATERNION"
    obj.matrix_world = matrix_world
    obj.keyframe_insert(data_path="location", frame=frame)
    obj.keyframe_insert(data_path="rotation_quaternion", frame=frame)


def schedule_move(scene, move, start_frame, record=True, turn_frames=None, pause_frames=None):
    cubies = rubik_cubies()
    if len(cubies) != 27:
        raise RuntimeError("Create the cube first with Create / Reset Cube.")

    face, amount = parse_move(move)
    axis, layer, clockwise_sign = MOVE_DEFS[face]
    axis_i = AXIS_INDEX[axis]

    quarter_frames = max(1, turn_frames if turn_frames is not None else scene.rc_turn_frames)
    pause = max(0, pause_frames if pause_frames is not None else scene.rc_pause_frames)
    duration = quarter_frames * (2 if abs(amount) == 2 else 1)
    end_frame = start_frame + duration

    # Evaluate the exact state at the start of this move before reading matrices.
    scene.frame_set(start_frame)

    selected = [
        obj for obj in cubies
        if obj.get("rc_pos") is not None and int(obj["rc_pos"][axis_i]) == layer
    ]
    if len(selected) != 9:
        raise RuntimeError(f"Expected 9 cubies for {face}, found {len(selected)}.")

    angle = math.radians(90.0 * clockwise_sign * amount)
    center = Vector(scene.rc_center)
    to_center = Matrix.Translation(-center)
    from_center = Matrix.Translation(center)
    starts = {obj.name: obj.matrix_world.copy() for obj in selected}

    # Sample each frame so positions follow a circular arc rather than a chord.
    for step in range(duration + 1):
        t = step / duration
        eased = t * t * (3.0 - 2.0 * t)
        partial = angle * eased
        rotation = Matrix.Rotation(partial, 4, axis)
        about_center = from_center @ rotation @ to_center
        frame = start_frame + step
        for obj in selected:
            keyframe_world_matrix(obj, about_center @ starts[obj.name], frame)

    for obj in selected:
        obj["rc_pos"] = rotate_grid_position(obj["rc_pos"], axis, angle)

    if record:
        append_history(scene, move)

    return end_frame + pause


def schedule_sequence(scene, moves, start_frame, record, turn_frames=None, pause_frames=None):
    cursor = start_frame
    for move in moves:
        cursor = schedule_move(
            scene,
            move,
            cursor,
            record=record,
            turn_frames=turn_frames,
            pause_frames=pause_frames,
        )
    return cursor


def next_animation_frame(scene):
    # Keep user-created motion after the build animation unless they move the playhead later.
    return max(int(scene.frame_current), int(scene.rc_build_end_frame) + 8)


# ============================================================
# OPERATORS
# ============================================================

class RC_OT_create_cube(bpy.types.Operator):
    bl_idname = "rubik.create_cube"
    bl_label = "Create / Reset Cube"
    bl_description = "Create a complete solver-ready Rubik's Cube without touching other scene objects"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        final_frame = create_cube(context.scene)
        if context.scene.rc_auto_play_build:
            try_play_animation(context, 1, final_frame)
        else:
            context.scene.frame_set(1)
        self.report({"INFO"}, "Cube created. The build animation assembles bottom, middle, then top.")
        return {"FINISHED"}


class RC_OT_play_build(bpy.types.Operator):
    bl_idname = "rubik.play_build"
    bl_label = "Play Build"
    bl_description = "Replay the cube construction animation"

    def execute(self, context):
        if not rubik_root():
            self.report({"ERROR"}, "Create the cube first.")
            return {"CANCELLED"}
        try_play_animation(context, 1, int(context.scene.rc_build_end_frame))
        return {"FINISHED"}


class RC_OT_turn(bpy.types.Operator):
    bl_idname = "rubik.turn"
    bl_label = "Turn Rubik Face"
    bl_options = {"REGISTER", "UNDO"}

    move: StringProperty()

    def execute(self, context):
        start = next_animation_frame(context.scene)
        try:
            end = schedule_move(context.scene, self.move, start, record=True)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        try_play_animation(context, start, end)
        return {"FINISHED"}


class RC_OT_scramble(bpy.types.Operator):
    bl_idname = "rubik.scramble"
    bl_label = "Scramble"
    bl_description = "Create and animate a random scramble"

    def execute(self, context):
        if not rubik_root():
            self.report({"ERROR"}, "Create the cube first.")
            return {"CANCELLED"}

        faces = list(MOVE_DEFS.keys())
        suffixes = ["", "'", "2"]
        moves = []
        previous = None
        for _ in range(context.scene.rc_scramble_moves):
            face = random.choice([f for f in faces if f != previous])
            previous = face
            moves.append(face + random.choice(suffixes))

        start = next_animation_frame(context.scene)
        try:
            end = schedule_sequence(context.scene, moves, start, record=True)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        try_play_animation(context, start, end)
        self.report({"INFO"}, "Scramble: " + " ".join(moves))
        return {"FINISHED"}


class RC_OT_solve(bpy.types.Operator):
    bl_idname = "rubik.solve"
    bl_label = "Solve Rubik's Cube"
    bl_description = "Solve the recorded scramble and animate the solution"

    method: EnumProperty(
        name="Solving Method",
        items=[
            ("TEACHING", "Teaching / Exact", "Reverse every recorded move. Most transparent for following the solution."),
            ("FAST", "Fast / Simplified", "Cancel adjacent redundant turns before solving."),
        ],
        default="FAST",
    )
    speed: EnumProperty(
        name="Playback Speed",
        items=[
            ("TUTORIAL", "Tutorial", "Slower turns with a longer pause"),
            ("HUMAN", "Human", "Quick but still easy to follow"),
            ("FAST", "Fast", "Short demonstration speed"),
        ],
        default="HUMAN",
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=380)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "method")
        layout.prop(self, "speed")
        layout.separator()
        layout.label(text=f"Recorded moves: {len(get_history(context.scene))}")

    def execute(self, context):
        history = get_history(context.scene)
        if not history:
            self.report({"WARNING"}, "There are no recorded moves to solve.")
            return {"CANCELLED"}

        solution = [inverse_move(m) for m in reversed(history)]
        if self.method == "FAST":
            solution = simplify_moves(solution)

        speed_settings = {
            "TUTORIAL": (16, 5),
            "HUMAN": (10, 3),
            "FAST": (6, 1),
        }
        turn_frames, pause_frames = speed_settings[self.speed]

        start = next_animation_frame(context.scene)
        try:
            end = schedule_sequence(
                context.scene,
                solution,
                start,
                record=False,
                turn_frames=turn_frames,
                pause_frames=pause_frames,
            )
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        set_history(context.scene, [])
        try_play_animation(context, start, end)
        self.report({"INFO"}, "Solution: " + (" ".join(solution) if solution else "Already solved"))
        return {"FINISHED"}


class RC_OT_clear_history(bpy.types.Operator):
    bl_idname = "rubik.clear_history"
    bl_label = "Clear Move History"

    def execute(self, context):
        set_history(context.scene, [])
        return {"FINISHED"}


# ============================================================
# UI
# ============================================================

class RC_PT_panel(bpy.types.Panel):
    bl_label = "Rubik's Cube"
    bl_idname = "RC_PT_easy_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Rubik"

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        create = layout.box()
        create.label(text="1. Cube")
        create.operator("rubik.create_cube", text="Create / Reset Cube", icon="CUBE")
        row = create.row(align=True)
        row.operator("rubik.play_build", text="Replay Build", icon="PLAY")
        row.prop(scene, "rc_auto_play_build", text="Auto Play")

        manual = layout.box()
        manual.label(text="2. Manual Turns")
        for faces in (("U", "D"), ("R", "L"), ("F", "B")):
            row = manual.row(align=True)
            for face in faces:
                for token, label in ((face, face), (face + "'", face + "′"), (face + "2", face + "2")):
                    op = row.operator("rubik.turn", text=label)
                    op.move = token

        solve = layout.box()
        solve.label(text="3. Scramble / Solve")
        solve.prop(scene, "rc_scramble_moves")
        row = solve.row(align=True)
        row.operator("rubik.scramble", icon="FILE_REFRESH")
        row.operator("rubik.solve", icon="PLAY")
        solve.label(text=f"Recorded: {len(get_history(scene))} moves")

        animation = layout.box()
        animation.label(text="Animation")
        animation.prop(scene, "rc_turn_frames")
        animation.prop(scene, "rc_pause_frames")

        advanced = layout.box()
        advanced.label(text="Build Options")
        advanced.prop(scene, "rc_add_studio")
        advanced.prop(scene, "rc_build_section_frames")
        advanced.prop(scene, "rc_build_gap_frames")


CLASSES = (
    RC_OT_create_cube,
    RC_OT_play_build,
    RC_OT_turn,
    RC_OT_scramble,
    RC_OT_solve,
    RC_OT_clear_history,
    RC_PT_panel,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)

    bpy.types.Scene.rc_turn_frames = IntProperty(
        name="Frames per 90°",
        description="Manual turn duration",
        default=10,
        min=1,
        max=120,
    )
    bpy.types.Scene.rc_pause_frames = IntProperty(
        name="Pause Between Moves",
        default=3,
        min=0,
        max=120,
    )
    bpy.types.Scene.rc_scramble_moves = IntProperty(
        name="Scramble Moves",
        default=20,
        min=1,
        max=100,
    )
    bpy.types.Scene.rc_build_section_frames = IntProperty(
        name="Build Section Frames",
        description="How long each horizontal cube section takes to assemble",
        default=18,
        min=4,
        max=120,
    )
    bpy.types.Scene.rc_build_gap_frames = IntProperty(
        name="Gap Between Sections",
        description="Pause between bottom, middle, and top construction sections",
        default=4,
        min=0,
        max=60,
    )
    bpy.types.Scene.rc_add_studio = BoolProperty(
        name="Add Camera + Lights",
        default=True,
    )
    bpy.types.Scene.rc_auto_play_build = BoolProperty(
        name="Auto Play",
        description="Automatically play the assembly animation after creating the cube",
        default=True,
    )
    bpy.types.Scene.rc_history = StringProperty(default="")
    bpy.types.Scene.rc_center = FloatVectorProperty(size=3, default=(0.0, 0.0, 0.0))
    bpy.types.Scene.rc_build_end_frame = IntProperty(default=64, min=1)


def unregister():
    for prop in (
        "rc_build_end_frame",
        "rc_center",
        "rc_history",
        "rc_auto_play_build",
        "rc_add_studio",
        "rc_build_gap_frames",
        "rc_build_section_frames",
        "rc_scramble_moves",
        "rc_pause_frames",
        "rc_turn_frames",
    ):
        if hasattr(bpy.types.Scene, prop):
            delattr(bpy.types.Scene, prop)

    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
