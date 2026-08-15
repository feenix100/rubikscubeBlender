bl_info = {
    "name": "Rubik's Cube Easy Animator",
    "author": "OpenAI",
    "version": (0, 3, 8),
    "blender": (4, 0, 0),
    "location": "3D View > Sidebar > Rubik",
    "description": "Rubik's Cube creation, animation, queued manual turns, CFOP training, and Two-Phase computer solving",
    "category": "Animation",
}

import bpy
import math
import random
import json
import time
import itertools
import collections
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
# LOGICAL CUBIE STATE + TWO-PHASE COMPUTER SOLVER
# ============================================================
# Cubie convention (standard Singmaster/Kociemba ordering):
# corners: URF, UFL, ULB, UBR, DFR, DLF, DBL, DRB
# edges:   UR, UF, UL, UB, DR, DF, DL, DB, FR, FL, BL, BR
#
# The Blender model and this state engine use the same U/R/F/D/L/B notation.
# This means the computer solver can work from every move made through the
# add-on, rather than merely reversing the most recent scramble.

SOLVER_FACE_ORDER = ("U", "R", "F", "D", "L", "B")
SOLVED_CUBIE_STATE = (
    list(range(8)), [0] * 8,
    list(range(12)), [0] * 12,
)

# One clockwise quarter-turn for each face. Each permutation is stored as
# "new position -> old position"; orientation arrays are the move deltas.
_BASE_MOVE_CUBES = {
    "U": ([3, 0, 1, 2, 4, 5, 6, 7], [0] * 8,
          [3, 0, 1, 2, 4, 5, 6, 7, 8, 9, 10, 11], [0] * 12),
    "R": ([4, 1, 2, 0, 7, 5, 6, 3], [2, 0, 0, 1, 1, 0, 0, 2],
          [8, 1, 2, 3, 11, 5, 6, 7, 4, 9, 10, 0], [0] * 12),
    "F": ([1, 5, 2, 3, 0, 4, 6, 7], [1, 2, 0, 0, 2, 1, 0, 0],
          [0, 9, 2, 3, 4, 8, 6, 7, 1, 5, 10, 11],
          [0, 1, 0, 0, 0, 1, 0, 0, 1, 1, 0, 0]),
    "D": ([0, 1, 2, 3, 5, 6, 7, 4], [0] * 8,
          [0, 1, 2, 3, 5, 6, 7, 4, 8, 9, 10, 11], [0] * 12),
    "L": ([0, 2, 6, 3, 4, 1, 5, 7], [0, 1, 2, 0, 0, 2, 1, 0],
          [0, 1, 10, 3, 4, 5, 9, 7, 8, 2, 6, 11], [0] * 12),
    "B": ([0, 1, 3, 7, 4, 5, 2, 6], [0, 0, 1, 2, 0, 0, 2, 1],
          [0, 1, 2, 11, 4, 5, 6, 10, 8, 9, 3, 7],
          [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 1, 1]),
}


def cubie_state_multiply(a, b):
    """Return state a followed by move/state b."""
    acp, aco, aep, aeo = a
    bcp, bco, bep, beo = b
    return (
        [acp[bcp[i]] for i in range(8)],
        [(aco[bcp[i]] + bco[i]) % 3 for i in range(8)],
        [aep[bep[i]] for i in range(12)],
        [(aeo[bep[i]] + beo[i]) % 2 for i in range(12)],
    )


def _build_solver_move_cubes():
    cubes = []
    names = []
    for face in SOLVER_FACE_ORDER:
        one = _BASE_MOVE_CUBES[face]
        two = cubie_state_multiply(one, one)
        three = cubie_state_multiply(two, one)
        cubes.extend((one, two, three))
        names.extend((face, face + "2", face + "'"))
    return cubes, names


SOLVER_MOVE_CUBES, SOLVER_MOVE_NAMES = _build_solver_move_cubes()
SOLVER_MOVE_INDEX = {name: i for i, name in enumerate(SOLVER_MOVE_NAMES)}
PHASE2_MOVE_INDICES = (
    SOLVER_MOVE_INDEX["U"], SOLVER_MOVE_INDEX["U2"], SOLVER_MOVE_INDEX["U'"],
    SOLVER_MOVE_INDEX["D"], SOLVER_MOVE_INDEX["D2"], SOLVER_MOVE_INDEX["D'"],
    SOLVER_MOVE_INDEX["R2"], SOLVER_MOVE_INDEX["L2"],
    SOLVER_MOVE_INDEX["F2"], SOLVER_MOVE_INDEX["B2"],
)


def state_after_moves(moves, initial=None):
    state = (
        list((initial or SOLVED_CUBIE_STATE)[0]),
        list((initial or SOLVED_CUBIE_STATE)[1]),
        list((initial or SOLVED_CUBIE_STATE)[2]),
        list((initial or SOLVED_CUBIE_STATE)[3]),
    )
    for move in moves:
        index = SOLVER_MOVE_INDEX.get(move)
        if index is None:
            raise ValueError(f"Unsupported solver move: {move}")
        state = cubie_state_multiply(state, SOLVER_MOVE_CUBES[index])
    return state


def scene_cubie_state(scene):
    return state_after_moves(get_history(scene))


def state_is_solved(state):
    cp, co, ep, eo = state
    return cp == list(range(8)) and co == [0] * 8 and ep == list(range(12)) and eo == [0] * 12


_SLICE_COMBINATIONS = list(itertools.combinations(range(12), 4))
_SLICE_COMBINATION_INDEX = {combo: i for i, combo in enumerate(_SLICE_COMBINATIONS)}
_TWO_PHASE_TABLES = None


def _twist_index(co):
    value = 0
    for i in range(7):
        value = 3 * value + co[i]
    return value


def _twist_decode(value):
    co = [0] * 8
    total = 0
    for i in range(6, -1, -1):
        co[i] = value % 3
        total += co[i]
        value //= 3
    co[7] = (-total) % 3
    return co


def _flip_index(eo):
    value = 0
    for i in range(11):
        value = 2 * value + eo[i]
    return value


def _flip_decode(value):
    eo = [0] * 12
    total = 0
    for i in range(10, -1, -1):
        eo[i] = value % 2
        total += eo[i]
        value //= 2
    eo[11] = total % 2
    return eo


def _slice_index(ep):
    positions = tuple(i for i, piece in enumerate(ep) if piece >= 8)
    return _SLICE_COMBINATION_INDEX[positions]


def _slice_decode(index):
    positions = set(_SLICE_COMBINATIONS[index])
    ep = []
    ud_piece = 0
    slice_piece = 8
    for position in range(12):
        if position in positions:
            ep.append(slice_piece)
            slice_piece += 1
        else:
            ep.append(ud_piece)
            ud_piece += 1
    return ep


def _rank_permutation(permutation):
    n = len(permutation)
    rank = 0
    for i in range(n):
        smaller = sum(1 for j in range(i + 1, n) if permutation[j] < permutation[i])
        rank = rank * (n - i) + smaller
    return rank


def _unrank_permutation(n, rank):
    digits = [0] * n
    for i in range(1, n + 1):
        digits[n - i] = rank % i
        rank //= i
    remaining = list(range(n))
    return [remaining.pop(digit) for digit in digits]


def _bfs_distances(size, move_table, goal, move_count):
    distances = bytearray([255]) * size
    distances[goal] = 0
    queue = collections.deque([goal])
    while queue:
        current = queue.popleft()
        next_distance = distances[current] + 1
        row = move_table[current]
        for move_index in range(move_count):
            nxt = row[move_index]
            if distances[nxt] == 255:
                distances[nxt] = next_distance
                queue.append(nxt)
    return distances


def _bfs_pair_distances(size_a, size_b, move_a, move_b, goal_a, goal_b, move_count):
    """Pattern database for a pair of coordinates, stored as one byte/state."""
    total_size = size_a * size_b
    distances = bytearray([255]) * total_size
    goal = goal_a * size_b + goal_b
    distances[goal] = 0
    queue = collections.deque([goal])
    while queue:
        combined = queue.popleft()
        coord_a = combined // size_b
        coord_b = combined - coord_a * size_b
        next_distance = distances[combined] + 1
        row_a = move_a[coord_a]
        row_b = move_b[coord_b]
        for move_index in range(move_count):
            nxt = row_a[move_index] * size_b + row_b[move_index]
            if distances[nxt] == 255:
                distances[nxt] = next_distance
                queue.append(nxt)
    return distances


def _get_two_phase_tables():
    """Build compact coordinate move/pruning tables lazily on first computer solve."""
    global _TWO_PHASE_TABLES
    if _TWO_PHASE_TABLES is not None:
        return _TWO_PHASE_TABLES

    twist_move = [[0] * 18 for _ in range(2187)]
    for value in range(2187):
        co = _twist_decode(value)
        for move_index, move_cube in enumerate(SOLVER_MOVE_CUBES):
            cp_map, co_delta, _, _ = move_cube
            next_co = [(co[cp_map[i]] + co_delta[i]) % 3 for i in range(8)]
            twist_move[value][move_index] = _twist_index(next_co)

    flip_move = [[0] * 18 for _ in range(2048)]
    for value in range(2048):
        eo = _flip_decode(value)
        for move_index, move_cube in enumerate(SOLVER_MOVE_CUBES):
            _, _, ep_map, eo_delta = move_cube
            next_eo = [(eo[ep_map[i]] + eo_delta[i]) % 2 for i in range(12)]
            flip_move[value][move_index] = _flip_index(next_eo)

    slice_move = [[0] * 18 for _ in range(495)]
    for value in range(495):
        ep = _slice_decode(value)
        for move_index, move_cube in enumerate(SOLVER_MOVE_CUBES):
            ep_map = move_cube[2]
            next_ep = [ep[ep_map[i]] for i in range(12)]
            slice_move[value][move_index] = _slice_index(next_ep)

    solved_slice = _slice_index(list(range(12)))
    twist_distance = _bfs_distances(2187, twist_move, 0, 18)
    flip_distance = _bfs_distances(2048, flip_move, 0, 18)
    slice_distance = _bfs_distances(495, slice_move, solved_slice, 18)

    corner_perm_move = [[0] * 10 for _ in range(40320)]
    edge_perm_move = [[0] * 10 for _ in range(40320)]
    for rank in range(40320):
        permutation = _unrank_permutation(8, rank)
        for phase_move_index, move_index in enumerate(PHASE2_MOVE_INDICES):
            move_cube = SOLVER_MOVE_CUBES[move_index]
            corner_map = move_cube[0]
            edge_map = move_cube[2]
            corner_next = [permutation[corner_map[i]] for i in range(8)]
            edge_next = [permutation[edge_map[i]] for i in range(8)]
            corner_perm_move[rank][phase_move_index] = _rank_permutation(corner_next)
            edge_perm_move[rank][phase_move_index] = _rank_permutation(edge_next)

    slice_perm_move = [[0] * 10 for _ in range(24)]
    for rank in range(24):
        permutation = _unrank_permutation(4, rank)
        ep = list(range(8)) + [piece + 8 for piece in permutation]
        for phase_move_index, move_index in enumerate(PHASE2_MOVE_INDICES):
            edge_map = SOLVER_MOVE_CUBES[move_index][2]
            next_ep = [ep[edge_map[i]] for i in range(12)]
            next_slice_perm = [piece - 8 for piece in next_ep[8:12]]
            slice_perm_move[rank][phase_move_index] = _rank_permutation(next_slice_perm)

    corner_distance = _bfs_distances(40320, corner_perm_move, 0, 10)
    edge_distance = _bfs_distances(40320, edge_perm_move, 0, 10)
    slice_perm_distance = _bfs_distances(24, slice_perm_move, 0, 10)

    # Strong pair-pattern databases make the actual IDA* search fast enough for
    # interactive use. They cost a few seconds only on the first computer solve
    # and are then cached for the rest of the Blender session.
    twist_slice_distance = _bfs_pair_distances(
        2187, 495, twist_move, slice_move, 0, solved_slice, 18
    )
    flip_slice_distance = _bfs_pair_distances(
        2048, 495, flip_move, slice_move, 0, solved_slice, 18
    )
    corner_slice_perm_distance = _bfs_pair_distances(
        40320, 24, corner_perm_move, slice_perm_move, 0, 0, 10
    )
    edge_slice_perm_distance = _bfs_pair_distances(
        40320, 24, edge_perm_move, slice_perm_move, 0, 0, 10
    )

    _TWO_PHASE_TABLES = {
        "twist_move": twist_move,
        "flip_move": flip_move,
        "slice_move": slice_move,
        "twist_distance": twist_distance,
        "flip_distance": flip_distance,
        "slice_distance": slice_distance,
        "solved_slice": solved_slice,
        "corner_perm_move": corner_perm_move,
        "edge_perm_move": edge_perm_move,
        "slice_perm_move": slice_perm_move,
        "corner_distance": corner_distance,
        "edge_distance": edge_distance,
        "slice_perm_distance": slice_perm_distance,
        "twist_slice_distance": twist_slice_distance,
        "flip_slice_distance": flip_slice_distance,
        "corner_slice_perm_distance": corner_slice_perm_distance,
        "edge_slice_perm_distance": edge_slice_perm_distance,
    }
    return _TWO_PHASE_TABLES


def _phase1_coordinates(state):
    _, co, ep, eo = state
    return _twist_index(co), _flip_index(eo), _slice_index(ep)


def _phase2_coordinates(state):
    cp, _, ep, _ = state
    return (
        _rank_permutation(cp),
        _rank_permutation(ep[:8]),
        _rank_permutation([piece - 8 for piece in ep[8:12]]),
    )


def solve_two_phase(state, timeout_seconds=20.0):
    """Return (move_names, phase1_length) using a compact two-phase IDA* search."""
    if state_is_solved(state):
        return [], 0

    tables = _get_two_phase_tables()
    started = time.perf_counter()
    phase1_path = []
    twist, flip, slice_coord = _phase1_coordinates(state)

    def timed_out():
        return time.perf_counter() - started > timeout_seconds

    def search_phase1(tw, fl, sl, depth, last_face):
        if timed_out():
            raise TimeoutError("Two-Phase solver exceeded its time limit.")
        heuristic = max(
            tables["twist_slice_distance"][tw * 495 + sl],
            tables["flip_slice_distance"][fl * 495 + sl],
        )
        if heuristic > depth:
            return False
        if depth == 0:
            return tw == 0 and fl == 0 and sl == tables["solved_slice"]

        for move_index in range(18):
            face = move_index // 3
            if face == last_face:
                continue
            # Opposite faces commute. Keep one canonical ordering to avoid
            # searching both U D and D U (and equivalent opposite pairs).
            if last_face >= 0 and face == (last_face + 3) % 6 and face < last_face:
                continue
            phase1_path.append(move_index)
            if search_phase1(
                tables["twist_move"][tw][move_index],
                tables["flip_move"][fl][move_index],
                tables["slice_move"][sl][move_index],
                depth - 1,
                face,
            ):
                return True
            phase1_path.pop()
        return False

    minimum = max(
        tables["twist_distance"][twist],
        tables["flip_distance"][flip],
        tables["slice_distance"][slice_coord],
    )
    found_phase1 = False
    for depth in range(minimum, 13):
        if search_phase1(twist, flip, slice_coord, depth, -1):
            found_phase1 = True
            break
    if not found_phase1:
        raise RuntimeError("Two-Phase phase 1 could not find a solution within depth 12.")

    phase1_state = state
    for move_index in phase1_path:
        phase1_state = cubie_state_multiply(phase1_state, SOLVER_MOVE_CUBES[move_index])

    corner_perm, edge_perm, slice_perm = _phase2_coordinates(phase1_state)
    phase2_path = []

    def search_phase2(cp_coord, ep_coord, sp_coord, depth, last_face):
        if timed_out():
            raise TimeoutError("Two-Phase solver exceeded its time limit.")
        heuristic = max(
            tables["corner_slice_perm_distance"][cp_coord * 24 + sp_coord],
            tables["edge_slice_perm_distance"][ep_coord * 24 + sp_coord],
        )
        if heuristic > depth:
            return False
        if depth == 0:
            return cp_coord == 0 and ep_coord == 0 and sp_coord == 0

        for phase_move_index, move_index in enumerate(PHASE2_MOVE_INDICES):
            face = move_index // 3
            if face == last_face:
                continue
            if last_face >= 0 and face == (last_face + 3) % 6 and face < last_face:
                continue
            phase2_path.append(move_index)
            if search_phase2(
                tables["corner_perm_move"][cp_coord][phase_move_index],
                tables["edge_perm_move"][ep_coord][phase_move_index],
                tables["slice_perm_move"][sp_coord][phase_move_index],
                depth - 1,
                face,
            ):
                return True
            phase2_path.pop()
        return False

    minimum = max(
        tables["corner_distance"][corner_perm],
        tables["edge_distance"][edge_perm],
        tables["slice_perm_distance"][slice_perm],
    )
    found_phase2 = False
    for depth in range(minimum, 19):
        if search_phase2(corner_perm, edge_perm, slice_perm, depth, -1):
            found_phase2 = True
            break
    if not found_phase2:
        raise RuntimeError("Two-Phase phase 2 could not find a solution within depth 18.")

    all_indices = phase1_path + phase2_path
    solution = [SOLVER_MOVE_NAMES[index] for index in all_indices]
    return solution, len(phase1_path)


# ============================================================
# CFOP HUMAN TRAINING SCRAMBLE
# ============================================================
# Section 4 deliberately creates a randomized *teachable* CFOP case. The
# stored forward plan is genuine staged CFOP training: Cross -> four simple
# F2L trigger cases -> 2-look OLL -> PLL. The computer Two-Phase button can
# independently solve the same resulting cube state.

CFOP_F2L_CASES = (
    ("F2L - Front Right", "Right trigger: pair and insert the front-right corner and edge.",
     ("U", "R", "U'", "R'")),
    ("F2L - Front Left", "Left trigger: pair and insert the front-left corner and edge.",
     ("U'", "L'", "U", "L")),
    ("F2L - Back Left", "Back-left trigger: solve the back-left F2L pair while preserving the cross.",
     ("U'", "B'", "U", "B")),
    ("F2L - Back Right", "Back-right trigger: solve the back-right F2L pair while preserving the cross.",
     ("U", "B", "U'", "B'")),
)

CFOP_OLL_EDGE_CASES = (
    ("OLL - Orient Edges", "First look of 2-look OLL: orient the last-layer edges.",
     ("F", "R", "U", "R'", "U'", "F'")),
)

CFOP_OLL_CORNER_CASES = (
    ("OLL - Orient Corners", "Sune: orient the last-layer corners after the edges are oriented.",
     ("R", "U", "R'", "U", "R", "U2", "R'")),
    ("OLL - Orient Corners", "Anti-Sune: another core 2-look OLL corner case.",
     ("R", "U2", "R'", "U'", "R", "U'", "R'")),
)

CFOP_PLL_CASES = (
    ("PLL - T Perm", "Permute the final layer with a T-perm while keeping orientation solved.",
     ("R", "U", "R'", "U'", "R'", "F", "R2", "U'", "R'", "U'", "R", "U", "R'", "F'")),
    ("PLL - Ua Perm", "Cycle three last-layer edges with a Ua permutation.",
     ("R", "U'", "R", "U", "R", "U", "R", "U'", "R'", "U'", "R2")),
    ("PLL - Ub Perm", "Cycle three last-layer edges in the opposite direction with a Ub permutation.",
     ("R2", "U", "R", "U", "R'", "U'", "R'", "U'", "R'", "U", "R'")),
    ("PLL - Jb Perm", "Permute a corner pair and edge pair using a Jb permutation.",
     ("R", "U", "R'", "F'", "R", "U", "R'", "U'", "R'", "F", "R2", "U'", "R'")),
)


def _random_cross_sequence(length=6):
    faces = list(MOVE_DEFS.keys())
    suffixes = ("", "'", "2")
    moves = []
    previous_face = None
    for _ in range(length):
        choices = [face for face in faces if face != previous_face]
        face = random.choice(choices)
        previous_face = face
        moves.append(face + random.choice(suffixes))
    return moves


def generate_cfop_training_plan():
    cross_moves = _random_cross_sequence(random.randint(5, 7))
    segments = [{
        "stage": "Cross",
        "text": "Build the four bottom cross edges. Speedsolvers try to plan most or all of this before the first turn.",
        "moves": cross_moves,
    }]

    # Randomize the order of the four simple F2L trigger cases while retaining
    # one case for each slot. Each trigger fixes the cross and the other three
    # solved F2L slots, which makes the generated lesson structurally stable.
    f2l_cases = list(CFOP_F2L_CASES)
    random.shuffle(f2l_cases)
    for stage, text, moves in f2l_cases:
        segments.append({"stage": stage, "text": text, "moves": list(moves)})

    edge_case = random.choice(CFOP_OLL_EDGE_CASES)
    corner_case = random.choice(CFOP_OLL_CORNER_CASES)
    pll_case = random.choice(CFOP_PLL_CASES)
    for stage, text, moves in (edge_case, corner_case, pll_case):
        segments.append({"stage": stage, "text": text, "moves": list(moves)})

    return segments


def flatten_plan(segments):
    return [move for segment in segments for move in segment.get("moves", [])]


def inverse_sequence(moves):
    return [inverse_move(move) for move in reversed(moves)]


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


def invalidate_cfop_plan(scene):
    scene.rc_cfop_plan = ""
    scene.rc_cfop_signature = ""


def cfop_plan_is_valid(scene):
    return bool(scene.rc_cfop_plan and scene.rc_cfop_signature == scene.rc_history)


def load_cfop_plan(scene):
    if not scene.rc_cfop_plan:
        return []
    try:
        value = json.loads(scene.rc_cfop_plan)
        return value if isinstance(value, list) else []
    except Exception:
        return []


def get_manual_queue(scene):
    return [m for m in scene.rc_manual_queue.split() if m]


def set_manual_queue(scene, moves):
    scene.rc_manual_queue = " ".join(moves)


def enqueue_manual_move(scene, move):
    moves = get_manual_queue(scene)
    moves.append(move)
    set_manual_queue(scene, moves)


def pop_manual_move(scene):
    moves = get_manual_queue(scene)
    if not moves:
        return None
    move = moves.pop(0)
    set_manual_queue(scene, moves)
    return move


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


def snap_rotation_to_cardinal(rotation):
    """Return the nearest exact cube orientation (signed permutation matrix)."""
    snapped = Matrix.Identity(3)
    used_axes = set()

    # Matrix columns are the object's local axes expressed in world space.
    # After legal Rubik turns each column must point exactly along one world axis.
    for col_index in range(3):
        column = Vector((
            rotation[0][col_index],
            rotation[1][col_index],
            rotation[2][col_index],
        ))
        candidates = sorted(range(3), key=lambda i: abs(column[i]), reverse=True)
        axis_index = next(i for i in candidates if i not in used_axes)
        used_axes.add(axis_index)
        sign = 1.0 if column[axis_index] >= 0.0 else -1.0

        for row_index in range(3):
            snapped[row_index][col_index] = 0.0
        snapped[axis_index][col_index] = sign

    # Legal Rubik turns are proper rotations. Numerical noise should never make
    # this negative, but correct the final column defensively if necessary.
    if snapped.determinant() < 0.0:
        for row_index in range(3):
            snapped[row_index][2] *= -1.0

    return snapped


def exact_cubie_matrix(obj, scene):
    """Build an exact world matrix from the cubie's logical grid state."""
    logical = obj.get("rc_pos")
    if logical is None:
        return obj.matrix_world.copy()

    rotation = snap_rotation_to_cardinal(obj.matrix_world.to_3x3())
    matrix = rotation.to_4x4()
    center = Vector(scene.rc_center)
    matrix.translation = center + Vector((
        int(logical[0]) * SPACING,
        int(logical[1]) * SPACING,
        int(logical[2]) * SPACING,
    ))
    return matrix


def _set_world_matrix_with_quaternion_continuity(obj, matrix_world):
    """Assign a world matrix without allowing an equivalent quaternion sign flip.

    q and -q describe the same orientation, but an animation curve can interpolate
    between them as a large apparent spin. Preserve the sign closest to the
    object's previously assigned quaternion so consecutive turn samples always
    follow the short, intended path.
    """
    obj.rotation_mode = "QUATERNION"
    previous = obj.rotation_quaternion.copy()
    obj.matrix_world = matrix_world
    current = obj.rotation_quaternion.copy()
    if previous.dot(current) < 0.0:
        obj.rotation_quaternion = (-current.w, -current.x, -current.y, -current.z)


def snap_cubie_to_grid(obj, scene, frame=None, keyframe=False):
    matrix = exact_cubie_matrix(obj, scene)
    _set_world_matrix_with_quaternion_continuity(obj, matrix)
    if keyframe and frame is not None:
        obj.keyframe_insert(data_path="location", frame=frame)
        obj.keyframe_insert(data_path="rotation_quaternion", frame=frame)


def snap_all_cubies(scene):
    for obj in rubik_cubies():
        snap_cubie_to_grid(obj, scene)


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


def make_face_label_material(name, color, emission_strength=1.5):
    """Create a readable material for fixed viewport face labels."""
    material = make_material(name, color, roughness=0.3, metallic=0.0)
    principled = material.node_tree.nodes.get("Principled BSDF") if material.use_nodes else None
    if principled is not None:
        emission = principled.inputs.get("Emission Color")
        if emission is None:
            emission = principled.inputs.get("Emission")
        strength = principled.inputs.get("Emission Strength")
        if emission is not None:
            emission.default_value = (*color, 1.0)
        if strength is not None:
            strength.default_value = emission_strength
    return material


def rubik_face_labels():
    collection = rubik_collection()
    if collection is None:
        return []
    return [obj for obj in collection.objects if obj.get("rc_is_face_label", False)]


def set_face_labels_visible(scene):
    visible = bool(getattr(scene, "rc_show_face_labels", True))
    for label in rubik_face_labels():
        label.hide_viewport = not visible
        # These are instructional viewport helpers, not part of the render.
        label.hide_render = True


def _face_labels_toggle_update(scene, context):
    set_face_labels_visible(scene)
    _tag_view3d_redraw()


def add_face_label_text(name, body, location, rotation, collection, root, material, size):
    curve = bpy.data.curves.new(name=f"{name}_Curve", type="FONT")
    curve.body = body
    curve.align_x = "CENTER"
    curve.align_y = "CENTER"
    curve.size = size
    curve.extrude = 0.012
    curve.bevel_depth = 0.004
    curve.bevel_resolution = 2

    obj = bpy.data.objects.new(name, curve)
    collection.objects.link(obj)
    obj.location = location
    obj.rotation_euler = rotation
    obj.data.materials.append(material)
    obj.parent = root
    obj["rc_is_face_label"] = True
    obj.hide_select = True
    obj.hide_render = True
    return obj


def add_face_labels(scene, collection, root):
    """Add fixed notation labels for the six world-space Rubik faces.

    Each marker shows the clockwise and counter-clockwise notation for the same
    face (for example U / U-prime). Labels are parented to the cube root, not
    to face cubies, so they never spin when a face turn is animated.
    """
    white = make_face_label_material("M_Rubik_Face_Label", (0.95, 0.95, 0.95), 2.0)
    dark = make_face_label_material("M_Rubik_Face_Label_Shadow", (0.015, 0.015, 0.02), 0.0)

    # Put the text slightly beyond the center sticker plane. The duplicate dark
    # text is a larger backing glyph, which keeps the white label readable over
    # every sticker color without covering the face itself.
    r = SPACING + CUBIE_SIZE / 2 + STICKER_THICKNESS + STICKER_OFFSET + 0.10
    specs = {
        "U": ((0.0, 0.0, +r), (0.0, 0.0, 0.0)),
        "D": ((0.0, 0.0, -r), (math.pi, 0.0, 0.0)),
        "R": ((+r, 0.0, 0.0), (0.0, math.pi / 2, 0.0)),
        "L": ((-r, 0.0, 0.0), (0.0, -math.pi / 2, 0.0)),
        "F": ((0.0, -r, 0.0), (math.pi / 2, 0.0, 0.0)),
        "B": ((0.0, +r, 0.0), (-math.pi / 2, 0.0, 0.0)),
    }
    for face, (location, rotation) in specs.items():
        body = f"{face} / {face}′"
        # Slightly larger dark copy behind the bright text for contrast.
        add_face_label_text(
            f"FaceLabel_{face}_Shadow", body, location, rotation,
            collection, root, dark, 0.56,
        )
        # Move the bright copy a hair farther outward to avoid z-fighting.
        normal = Vector(location).normalized()
        front_loc = Vector(location) + normal * 0.018
        add_face_label_text(
            f"FaceLabel_{face}", body, tuple(front_loc), rotation,
            collection, root, white, 0.50,
        )

    set_face_labels_visible(scene)


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


def iter_object_fcurves(obj):
    """Yield F-Curves used by *obj* on Blender 4.x and Blender 5.x.

    Blender 5.0 removed Action.fcurves from the public API. Slotted/layered
    Actions keep F-Curves in an ActionChannelbag instead.
    """
    anim_data = obj.animation_data
    if anim_data is None or anim_data.action is None:
        return

    action = anim_data.action

    # Blender 4.x / legacy Actions.
    legacy_fcurves = getattr(action, "fcurves", None)
    if legacy_fcurves is not None:
        yield from legacy_fcurves
        return

    # Blender 5.x / slotted layered Actions.
    active_slot = getattr(anim_data, "action_slot", None)
    for layer in getattr(action, "layers", ()):
        for strip in getattr(layer, "strips", ()):
            channelbags = getattr(strip, "channelbags", None)
            if channelbags is None:
                continue

            for channelbag in channelbags:
                # Normally there is one slot per cubie action. Filtering keeps
                # this safe if an Action is ever shared between data-blocks.
                if active_slot is not None:
                    bag_slot = getattr(channelbag, "slot", None)
                    if bag_slot is not None and bag_slot != active_slot:
                        continue

                yield from getattr(channelbag, "fcurves", ())


def set_key_interpolation(obj, data_paths, interpolation="BEZIER"):
    for fcurve in iter_object_fcurves(obj):
        if fcurve.data_path in data_paths:
            for point in fcurve.keyframe_points:
                point.interpolation = interpolation


def set_key_interpolation_range(obj, data_paths, start_frame, end_frame, interpolation="LINEAR"):
    """Set interpolation only for keyframes inside one generated move.

    Turn animation is already eased and sampled once per frame, so LINEAR
    interpolation between those samples is intentional. Restricting the change
    to this frame range preserves the separate Bezier build animation.
    """
    start = float(start_frame) - 0.001
    end = float(end_frame) + 0.001
    for fcurve in iter_object_fcurves(obj):
        if fcurve.data_path not in data_paths:
            continue
        for point in fcurve.keyframe_points:
            frame = float(point.co.x)
            if start <= frame <= end:
                point.interpolation = interpolation


_ONE_SHOT_END_FRAMES = {}
_MANUAL_START_PENDING = set()
_TUTORIAL_TRACKS = {}


def clear_tutorial_runtime(scene, clear_text=False):
    _TUTORIAL_TRACKS.pop(scene.as_pointer(), None)
    scene.rc_tutorial_active = False
    scene.rc_tutorial_paused = False
    if clear_text:
        scene.rc_tutorial_stage = ""
        scene.rc_tutorial_text = ""
        scene.rc_tutorial_move = ""
        scene.rc_tutorial_algorithm = ""


def begin_tutorial_runtime(scene, entries, method_name):
    _TUTORIAL_TRACKS[scene.as_pointer()] = list(entries)
    scene.rc_tutorial_active = bool(entries)
    scene.rc_tutorial_paused = False
    scene.rc_tutorial_method = method_name
    if entries:
        first = entries[0]
        scene.rc_tutorial_stage = first[2]
        scene.rc_tutorial_text = first[3]
        scene.rc_tutorial_move = first[4]
        scene.rc_tutorial_algorithm = first[5]


def _tag_view3d_redraw():
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return
    for window in wm.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _update_tutorial_state(scene):
    entries = _TUTORIAL_TRACKS.get(scene.as_pointer())
    if not entries:
        return
    frame = int(scene.frame_current)
    for entry in entries:
        start, end, stage, text, move_text, algorithm_text = entry[:6]
        if start <= frame <= end:
            changed = False
            if scene.rc_tutorial_stage != stage:
                scene.rc_tutorial_stage = stage
                changed = True
            if scene.rc_tutorial_text != text:
                scene.rc_tutorial_text = text
                changed = True
            if scene.rc_tutorial_move != move_text:
                scene.rc_tutorial_move = move_text
                changed = True
            if scene.rc_tutorial_algorithm != algorithm_text:
                scene.rc_tutorial_algorithm = algorithm_text
                changed = True
            if changed:
                _tag_view3d_redraw()
            return

    # During deliberate gaps between method stages, leave the last explanation
    # visible so the learner has time to read it.
    _tag_view3d_redraw()


def _resume_scene_playback(scene):
    wm = getattr(bpy.context, "window_manager", None)
    if wm is not None:
        for window in wm.windows:
            screen = window.screen
            if screen is None or window.scene != scene:
                continue
            try:
                with bpy.context.temp_override(window=window, screen=screen, scene=scene):
                    bpy.ops.screen.animation_play()
                scene.rc_tutorial_paused = False
                return True
            except Exception:
                continue
    try:
        bpy.ops.screen.animation_play()
        scene.rc_tutorial_paused = False
        return True
    except Exception:
        return False


def stop_animation_playback():
    """Stop active Blender playback without jumping back to its original frame."""
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return

    for window in wm.windows:
        screen = window.screen
        if screen is None or not screen.is_animation_playing:
            continue
        try:
            with bpy.context.temp_override(window=window, screen=screen):
                bpy.ops.screen.animation_cancel(restore_frame=False)
        except Exception:
            pass


def _play_animation_for_scene(scene, start_frame, end_frame):
    """Start one-shot playback for a scene, including from timer callbacks."""
    start_frame = int(start_frame)
    end_frame = int(end_frame)

    stop_animation_playback()

    if scene.frame_end < end_frame:
        scene.frame_end = end_frame

    if scene.use_preview_range:
        if scene.frame_preview_start > start_frame:
            scene.frame_preview_start = start_frame
        if scene.frame_preview_end < end_frame:
            scene.frame_preview_end = end_frame

    _ONE_SHOT_END_FRAMES[scene.as_pointer()] = end_frame
    if _one_shot_frame_change not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(_one_shot_frame_change)

    scene.frame_set(start_frame)

    wm = getattr(bpy.context, "window_manager", None)
    if wm is not None:
        for window in wm.windows:
            screen = window.screen
            if screen is None or window.scene != scene:
                continue
            try:
                with bpy.context.temp_override(window=window, screen=screen, scene=scene):
                    bpy.ops.screen.animation_play()
                return True
            except Exception:
                continue

    try:
        bpy.ops.screen.animation_play()
        return True
    except Exception:
        _ONE_SHOT_END_FRAMES.pop(scene.as_pointer(), None)
        if not _ONE_SHOT_END_FRAMES:
            try:
                bpy.app.handlers.frame_change_post.remove(_one_shot_frame_change)
            except ValueError:
                pass
        return False


def _request_next_manual_turn(scene):
    """Start the next queued manual move on the next safe UI/timer tick."""
    key = scene.as_pointer()
    if key in _MANUAL_START_PENDING:
        return
    if not get_manual_queue(scene):
        scene.rc_manual_busy = False
        return
    if key in _ONE_SHOT_END_FRAMES:
        return

    _MANUAL_START_PENDING.add(key)

    def start_queued_turn():
        _MANUAL_START_PENDING.discard(key)
        try:
            if not rubik_root() or not get_manual_queue(scene):
                scene.rc_manual_busy = False
                return None
            if key in _ONE_SHOT_END_FRAMES:
                return 0.05

            move = pop_manual_move(scene)
            if move is None:
                scene.rc_manual_busy = False
                return None

            scene.rc_manual_busy = True
            start = next_animation_frame(scene)
            end = schedule_move(scene, move, start, record=True)
            scene.rc_last_animation_frame = max(scene.rc_last_animation_frame, int(end))

            if not _play_animation_for_scene(scene, start, end):
                # Keep the cube in its exact completed state even if viewport
                # playback cannot be started in the current Blender context.
                scene.frame_set(end)
                snap_all_cubies(scene)
                scene.rc_manual_busy = False
                if get_manual_queue(scene):
                    _request_next_manual_turn(scene)
            return None
        except Exception:
            scene.rc_manual_busy = False
            return None

    bpy.app.timers.register(start_queued_turn, first_interval=0.01)


def _one_shot_frame_change(scene, *args):
    """Update tutorial UI, then stop one-shot playback exactly at its end."""
    _update_tutorial_state(scene)
    scene_key = scene.as_pointer()
    end_frame = _ONE_SHOT_END_FRAMES.get(scene_key)
    if end_frame is None or scene.frame_current < end_frame:
        return

    stop_animation_playback()
    _ONE_SHOT_END_FRAMES.pop(scene_key, None)

    if scene.frame_current != end_frame:
        scene.frame_set(end_frame)

    # The final keyframe is already exact, but explicitly snapping here makes
    # the invariant resilient to long sessions and Blender interpolation noise.
    snap_all_cubies(scene)
    scene.rc_manual_busy = False

    if scene_key in _TUTORIAL_TRACKS:
        _TUTORIAL_TRACKS.pop(scene_key, None)
        scene.rc_tutorial_active = False
        scene.rc_tutorial_paused = False
        scene.rc_tutorial_stage = "Complete"
        scene.rc_tutorial_text = f"{scene.rc_tutorial_method} solve complete."
        scene.rc_tutorial_move = ""
        scene.rc_tutorial_algorithm = ""
        _tag_view3d_redraw()

    if get_manual_queue(scene):
        _request_next_manual_turn(scene)

    if not _ONE_SHOT_END_FRAMES:
        try:
            bpy.app.handlers.frame_change_post.remove(_one_shot_frame_change)
        except ValueError:
            pass


def try_play_animation(context, start_frame, end_frame):
    """Play only the requested generated range once, without timeline wrapping."""
    return _play_animation_for_scene(context.scene, start_frame, end_frame)


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
    # Intentionally no floor: the generated scene contains only the cube,
    # optional camera, and optional lights.
    camera_data = bpy.data.cameras.new("Rubik_Camera")
    camera = bpy.data.objects.new("Rubik_Camera", camera_data)
    collection.objects.link(camera)
    camera.location = (12.075, -14.375, 10.35)
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

    add_face_labels(scene, collection, root)

    if scene.rc_add_studio:
        add_studio(collection, materials)

    scene.rc_center = (0.0, 0.0, 0.0)
    set_history(scene, [])
    set_manual_queue(scene, [])
    scene.rc_manual_busy = False
    final_frame = animate_assembly(scene, cubie_map)
    scene.rc_last_animation_frame = final_frame

    bpy.ops.object.select_all(action="DESELECT")
    root.select_set(True)
    bpy.context.view_layer.objects.active = root
    return final_frame


def reset_cube_pose_at_frame(scene, frame):
    """Instantly put every physical cubie in its solved home pose at *frame*.

    Existing build keyframes remain intact. This is used by Section 4 to guarantee
    a clean, reproducible training scramble without replaying the construction
    sequence.
    """
    scene.frame_set(int(frame))
    center = Vector(scene.rc_center)
    for cubie in rubik_cubies():
        home = cubie.get("rc_home")
        if home is None:
            continue
        cubie["rc_pos"] = [int(home[0]), int(home[1]), int(home[2])]
        matrix = Matrix.Identity(4)
        matrix.translation = center + Vector((
            int(home[0]) * SPACING,
            int(home[1]) * SPACING,
            int(home[2]) * SPACING,
        ))
        keyframe_world_matrix(cubie, matrix, int(frame))
    set_history(scene, [])
    set_manual_queue(scene, [])
    scene.rc_manual_busy = False
    invalidate_cfop_plan(scene)


# ============================================================
# TURN ANIMATION
# ============================================================

def keyframe_world_matrix(obj, matrix_world, frame):
    _set_world_matrix_with_quaternion_continuity(obj, matrix_world)
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

    # Commit logical positions first, then overwrite the final sampled keyframe
    # with an exact cardinal orientation and exact SPACING-grid translation.
    # This prevents floating-point drift from accumulating over hundreds or
    # thousands of manual turns.
    for obj in selected:
        obj["rc_pos"] = rotate_grid_position(obj["rc_pos"], axis, angle)
        snap_cubie_to_grid(obj, scene, frame=end_frame, keyframe=True)
        # We already sampled the eased circular motion on every frame. Prevent
        # Blender's default Bezier handles from adding overshoot or accidental
        # quaternion excursions between those exact samples.
        set_key_interpolation_range(
            obj,
            {"location", "rotation_quaternion"},
            start_frame,
            end_frame,
            "LINEAR",
        )

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


def schedule_method_segments(scene, segments, start_frame, tutorial=False):
    """Schedule staged method moves and return (end_frame, tutorial_entries)."""
    cursor = int(start_frame)
    entries = []
    turn_frames = scene.rc_turn_frames
    pause_frames = scene.rc_pause_frames
    if tutorial:
        turn_frames = max(turn_frames, 12)
        # Section 4 has its own learner-controlled pause so tutorial pacing
        # can be changed without affecting manual turns, scrambles, or the
        # normal one-click solver animation.
        pause_frames = max(0, int(scene.rc_tutorial_pause_frames))

    for segment in segments:
        stage = segment.get("stage", "Solve")
        text = segment.get("text", "")
        moves = list(segment.get("moves", []))
        if not moves:
            continue
        algorithm_text = " ".join(moves)
        for move in moves:
            move_start = cursor
            cursor = schedule_move(
                scene,
                move,
                cursor,
                record=False,
                turn_frames=turn_frames,
                pause_frames=pause_frames,
            )
            entries.append((move_start, max(move_start, cursor - 1), stage, text, move, algorithm_text))
        if tutorial:
            # A visible stage boundary gives the learner time to read and lets
            # them pause before the next CFOP/Two-Phase concept begins.
            cursor += max(12, pause_frames * 2)

    return cursor, entries


def next_animation_frame(scene):
    # Never schedule new motion inside an older animation range, even if the
    # user scrubbed the timeline backwards. That preserves state consistency.
    return max(
        int(scene.frame_current),
        int(scene.rc_build_end_frame) + 8,
        int(scene.rc_last_animation_frame),
    )


# ============================================================
# OPERATORS
# ============================================================

class RC_OT_create_cube(bpy.types.Operator):
    bl_idname = "rubik.create_cube"
    bl_label = "Create / Reset Cube"
    bl_description = "Create a complete solver-ready Rubik's Cube without touching other scene objects"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        stop_animation_playback()
        _ONE_SHOT_END_FRAMES.pop(context.scene.as_pointer(), None)
        set_manual_queue(context.scene, [])
        context.scene.rc_manual_busy = False
        clear_tutorial_runtime(context.scene, clear_text=True)
        invalidate_cfop_plan(context.scene)
        final_frame = create_cube(context.scene)
        try_play_animation(context, 1, final_frame)
        self.report({"INFO"}, "Cube created. The build animation plays once: bottom, middle, then top.")
        return {"FINISHED"}



class RC_OT_turn(bpy.types.Operator):
    bl_idname = "rubik.turn"
    bl_label = "Turn Rubik Face"
    bl_options = {"REGISTER", "UNDO"}

    move: StringProperty()

    def execute(self, context):
        scene = context.scene
        if not rubik_root():
            self.report({"ERROR"}, "Create the cube first.")
            return {"CANCELLED"}

        try:
            parse_move(self.move)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        # Button presses are never allowed to interrupt a move already playing.
        # They are stored in FIFO order and consumed one at a time. Any manual
        # edit invalidates a previously generated CFOP training plan.
        invalidate_cfop_plan(scene)
        clear_tutorial_runtime(scene, clear_text=True)
        enqueue_manual_move(scene, self.move)
        if not scene.rc_manual_busy and scene.as_pointer() not in _ONE_SHOT_END_FRAMES:
            _request_next_manual_turn(scene)
        return {"FINISHED"}


class RC_OT_scramble(bpy.types.Operator):
    bl_idname = "rubik.scramble"
    bl_label = "Scramble"
    bl_description = "Create and animate a random scramble"

    def execute(self, context):
        if not rubik_root():
            self.report({"ERROR"}, "Create the cube first.")
            return {"CANCELLED"}
        if context.scene.rc_manual_busy or get_manual_queue(context.scene):
            self.report({"WARNING"}, "Queued manual turns will play first.")
            return {"CANCELLED"}

        invalidate_cfop_plan(context.scene)
        clear_tutorial_runtime(context.scene, clear_text=True)
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

        context.scene.rc_last_animation_frame = max(context.scene.rc_last_animation_frame, int(end))
        try_play_animation(context, start, end)
        self.report({"INFO"}, "Scramble: " + " ".join(moves))
        return {"FINISHED"}


class RC_OT_solve(bpy.types.Operator):
    bl_idname = "rubik.solve"
    bl_label = "Solve Rubik's Cube"
    bl_description = "Immediately solve the recorded scramble and animate the solution"

    def execute(self, context):
        if context.scene.rc_manual_busy or get_manual_queue(context.scene):
            self.report({"WARNING"}, "Queued manual turns will play first.")
            return {"CANCELLED"}

        clear_tutorial_runtime(context.scene, clear_text=True)
        invalidate_cfop_plan(context.scene)
        history = get_history(context.scene)
        if not history:
            self.report({"WARNING"}, "There are no recorded moves to solve.")
            return {"CANCELLED"}

        # Use the shortest equivalent solution we can obtain from the recorded
        # move history, then animate it with the user's normal Animation settings.
        solution = [inverse_move(m) for m in reversed(history)]
        solution = simplify_moves(solution)

        start = next_animation_frame(context.scene)
        try:
            end = schedule_sequence(
                context.scene,
                solution,
                start,
                record=False,
            )
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        set_history(context.scene, [])
        context.scene.rc_last_animation_frame = max(context.scene.rc_last_animation_frame, int(end))
        try_play_animation(context, start, end)
        self.report({"INFO"}, "Solution: " + (" ".join(solution) if solution else "Already solved"))
        return {"FINISHED"}


class RC_OT_method_scramble(bpy.types.Operator):
    bl_idname = "rubik.method_scramble"
    bl_label = "Random Scramble"
    bl_description = "Create a randomized CFOP-teachable scramble without replaying the cube build"

    def execute(self, context):
        scene = context.scene
        if not rubik_root():
            self.report({"ERROR"}, "Create the cube first.")
            return {"CANCELLED"}
        if scene.rc_manual_busy or get_manual_queue(scene) or scene.as_pointer() in _ONE_SHOT_END_FRAMES:
            self.report({"WARNING"}, "Finish the current animation or queued turns first.")
            return {"CANCELLED"}

        clear_tutorial_runtime(scene, clear_text=True)
        start = next_animation_frame(scene) + 1
        reset_cube_pose_at_frame(scene, start)

        plan = generate_cfop_training_plan()
        solution = flatten_plan(plan)
        scramble = inverse_sequence(solution)

        # Scrambling is intentionally faster than the teaching solve.
        scramble_turn_frames = max(2, min(6, scene.rc_turn_frames // 2 if scene.rc_turn_frames > 2 else 2))
        try:
            end = schedule_sequence(
                scene,
                scramble,
                start + 1,
                record=True,
                turn_frames=scramble_turn_frames,
                pause_frames=0,
            )
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        scene.rc_cfop_plan = json.dumps(plan, separators=(",", ":"))
        scene.rc_cfop_signature = scene.rc_history
        scene.rc_last_animation_frame = max(scene.rc_last_animation_frame, int(end))
        scene.rc_method_status = f"CFOP training scramble ready ({len(scramble)} moves)"
        try_play_animation(context, start, end)
        self.report({"INFO"}, "Random CFOP training scramble created.")
        return {"FINISHED"}


class RC_OT_solve_cfop(bpy.types.Operator):
    bl_idname = "rubik.solve_cfop"
    bl_label = "CFOP (Human)"
    bl_description = "Solve the Section 4 training scramble with Cross, F2L, 2-look OLL, and PLL"

    def execute(self, context):
        scene = context.scene
        if not rubik_root():
            self.report({"ERROR"}, "Create the cube first.")
            return {"CANCELLED"}
        if scene.rc_manual_busy or get_manual_queue(scene) or scene.as_pointer() in _ONE_SHOT_END_FRAMES:
            self.report({"WARNING"}, "Finish the current animation or queued turns first.")
            return {"CANCELLED"}
        if not cfop_plan_is_valid(scene):
            self.report({"WARNING"}, "Click Random Scramble in Section 4 first. Manual changes invalidate the CFOP lesson plan.")
            return {"CANCELLED"}

        plan = load_cfop_plan(scene)
        if not plan:
            self.report({"ERROR"}, "The CFOP training plan could not be read. Create a new Random Scramble.")
            return {"CANCELLED"}

        current_state = scene_cubie_state(scene)
        proposed = state_after_moves(flatten_plan(plan), current_state)
        if not state_is_solved(proposed):
            self.report({"ERROR"}, "CFOP training state no longer matches its lesson plan. Create a new Random Scramble.")
            return {"CANCELLED"}

        start = next_animation_frame(scene)
        tutorial = bool(scene.rc_tutorial_mode)
        try:
            end, entries = schedule_method_segments(scene, plan, start, tutorial=tutorial)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        set_history(scene, [])
        scene.rc_cfop_signature = ""
        scene.rc_last_animation_frame = max(scene.rc_last_animation_frame, int(end))
        scene.rc_method_status = f"CFOP: {len(flatten_plan(plan))} moves"
        begin_tutorial_runtime(scene, entries, "CFOP")
        if not tutorial:
            scene.rc_tutorial_active = False
        try_play_animation(context, start, end)
        return {"FINISHED"}


class RC_OT_solve_two_phase(bpy.types.Operator):
    bl_idname = "rubik.solve_two_phase"
    bl_label = "Two-Phase (Computer)"
    bl_description = "Compute and animate a Two-Phase computer solution from the current logical cube state"

    def execute(self, context):
        scene = context.scene
        if not rubik_root():
            self.report({"ERROR"}, "Create the cube first.")
            return {"CANCELLED"}
        if scene.rc_manual_busy or get_manual_queue(scene) or scene.as_pointer() in _ONE_SHOT_END_FRAMES:
            self.report({"WARNING"}, "Finish the current animation or queued turns first.")
            return {"CANCELLED"}

        state = scene_cubie_state(scene)
        if state_is_solved(state):
            set_history(scene, [])
            invalidate_cfop_plan(scene)
            scene.rc_method_status = "Cube is already solved"
            self.report({"INFO"}, "Cube is already solved.")
            return {"FINISHED"}

        scene.rc_method_status = "Computing Two-Phase solution..."
        try:
            solution, phase1_length = solve_two_phase(state, timeout_seconds=20.0)
        except Exception as exc:
            scene.rc_method_status = "Two-Phase solve failed"
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        verified = state_after_moves(solution, state)
        if not state_is_solved(verified):
            self.report({"ERROR"}, "Internal verification rejected the computed solution.")
            return {"CANCELLED"}

        phase1 = solution[:phase1_length]
        phase2 = solution[phase1_length:]
        segments = []
        if phase1:
            segments.append({
                "stage": "Two-Phase - Phase 1",
                "text": "Orient corners and edges and place the four E-slice edges into the middle slice. This reaches the restricted G1 subgroup.",
                "moves": phase1,
            })
        if phase2:
            segments.append({
                "stage": "Two-Phase - Phase 2",
                "text": "With orientation and slice membership fixed, use the restricted move set to permute every corner and edge into its solved position.",
                "moves": phase2,
            })

        start = next_animation_frame(scene)
        tutorial = bool(scene.rc_tutorial_mode)
        try:
            end, entries = schedule_method_segments(scene, segments, start, tutorial=tutorial)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        set_history(scene, [])
        invalidate_cfop_plan(scene)
        scene.rc_last_animation_frame = max(scene.rc_last_animation_frame, int(end))
        scene.rc_method_status = f"Two-Phase: {len(solution)} moves ({phase1_length} + {len(solution) - phase1_length})"
        begin_tutorial_runtime(scene, entries, "Two-Phase")
        if not tutorial:
            scene.rc_tutorial_active = False
        try_play_animation(context, start, end)
        self.report({"INFO"}, f"Two-Phase solution: {len(solution)} moves")
        return {"FINISHED"}


class RC_OT_tutorial_pause_resume(bpy.types.Operator):
    bl_idname = "rubik.tutorial_pause_resume"
    bl_label = "Pause / Resume Tutorial"
    bl_description = "Pause the tutorial solve at the current frame, or resume it from that frame"

    def execute(self, context):
        scene = context.scene
        if not scene.rc_tutorial_active:
            self.report({"WARNING"}, "No tutorial solve is currently active.")
            return {"CANCELLED"}

        if scene.rc_tutorial_paused:
            if not _resume_scene_playback(scene):
                self.report({"ERROR"}, "Could not resume viewport playback in the current Blender context.")
                return {"CANCELLED"}
        else:
            stop_animation_playback()
            scene.rc_tutorial_paused = True
        return {"FINISHED"}


class RC_OT_human_solve_guide(bpy.types.Operator):
    bl_idname = "rubik.human_solve_guide"
    bl_label = "Human CFOP Solve Guide"
    bl_description = "Open step-by-step instructions for solving a randomly scrambled 3x3 Rubik's Cube with CFOP"

    @staticmethod
    def _wrap(layout, text, width=78, icon=None):
        words = text.split()
        line = ""
        first = True
        for word in words:
            candidate = (line + " " + word).strip()
            if len(candidate) > width and line:
                if first and icon:
                    layout.label(text=line, icon=icon)
                else:
                    layout.label(text=line)
                first = False
                line = word
            else:
                line = candidate
        if line:
            if first and icon:
                layout.label(text=line, icon=icon)
            else:
                layout.label(text=line)

    def draw(self, context):
        layout = self.layout

        intro = layout.box()
        intro.label(text="CFOP: Cross -> F2L -> OLL -> PLL", icon="CUBE")
        self._wrap(
            intro,
            "Use this method from any valid random 3x3 state. CFOP becomes fast because you learn to recognize pieces and cases instead of solving one sticker at a time.",
        )

        notation = layout.box()
        notation.label(text="Notation", icon="INFO")
        self._wrap(notation, "U, R, F, D, L, B mean turn the Up, Right, Front, Down, Left, or Back face 90 degrees clockwise while looking directly at that face.")
        self._wrap(notation, "An apostrophe means counter-clockwise, for example R'. A 2 means a 180-degree turn, for example U2. Use the viewport face labels if you want the letters visible around the cube.")

        cross = layout.box()
        cross.label(text="1. Cross")
        self._wrap(cross, "Start with the white center on the bottom. Locate the four white edge pieces and build a white cross around the white center.")
        self._wrap(cross, "Important: each cross edge must also match the side-center color next to it. A white cross with mismatched side colors is not finished.")
        self._wrap(cross, "Speed goal: inspect before turning and gradually learn to plan all four cross edges before you start the timer.")

        f2l = layout.box()
        f2l.label(text="2. F2L - First Two Layers")
        self._wrap(f2l, "There are four corner-edge pairs. For each slot, find the bottom-layer corner and the middle-layer edge that belong together, bring both into the U layer, pair them, then insert the pair without breaking the cross.")
        self._wrap(f2l, "Common right trigger: R U R'. Common left trigger: L' U' L. Many F2L cases are variations of these ideas with U turns used to position the pieces first.")
        self._wrap(f2l, "Speed goal: solve pairs intuitively and begin looking for the next pair while your hands finish the current insertion.")

        oll = layout.box()
        oll.label(text="3. OLL - Orient Last Layer")
        self._wrap(oll, "After F2L, make every sticker on the U face the same color. For learning, use two-look OLL: orient the four top edges first, then orient the four top corners.")
        self._wrap(oll, "Edge-orientation algorithm: F R U R' U' F'. Use U setup turns as needed to place the line or L-shaped edge pattern correctly before executing it.")
        self._wrap(oll, "Core corner algorithms: Sune = R U R' U R U2 R'. Anti-Sune = R U2 R' U' R U' R'. Rotate U to place the corner pattern correctly, then apply the matching case.")
        self._wrap(oll, "Advanced speed goal: progress from two-look OLL to recognizing the full OLL set so the entire top face can be oriented in one algorithm.")

        pll = layout.box()
        pll.label(text="4. PLL - Permute Last Layer")
        self._wrap(pll, "Keep the top face oriented and move the last-layer pieces into their correct positions. At this stage sticker orientation is correct; only piece locations need to change.")
        self._wrap(pll, "The add-on tutorial demonstrates common PLL cases such as T, Ua, Ub, and Jb permutations. Use U setup turns to align the case, execute the algorithm, then make a final U turn if needed.")
        self._wrap(pll, "Advanced speed goal: learn all 21 PLL cases so the last-layer permutation is solved in one recognized algorithm.")

        practice = layout.box()
        practice.label(text="How to Practice for Speed", icon="PLAY")
        self._wrap(practice, "1) Become consistent with Cross. 2) Learn intuitive F2L instead of memorizing every F2L case. 3) Learn two-look OLL and two-look PLL. 4) Add full PLL. 5) Add full OLL. 6) Practice look-ahead and smooth turning before trying to turn faster.")
        self._wrap(practice, "Use Section 4 Random Scramble + CFOP (Human) with Tutorial Mode enabled to watch the stages and pause between moves. The current built-in CFOP trainer demonstrates representative cases; it is not yet a complete 57-OLL / 21-PLL case library.")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=700)

    def execute(self, context):
        return {"FINISHED"}


class RC_OT_algorithm_reference(bpy.types.Operator):
    bl_idname = "rubik.algorithm_reference"
    bl_label = "CFOP Algorithm Reference"
    bl_description = "Open a quick-reference library of common CFOP algorithms"

    category: EnumProperty(
        name="Reference Section",
        items=(
            ("TRIGGERS", "F2L Triggers", "Core triggers used throughout intuitive F2L"),
            ("OLL2", "2-Look OLL", "Beginner last-layer orientation algorithms"),
            ("PLL2", "2-Look PLL", "Beginner last-layer permutation algorithms"),
            ("PLLFULL", "Full PLL", "All 21 PLL cases for advanced CFOP"),
        ),
        default="TRIGGERS",
    )

    @staticmethod
    def _algorithm(layout, name, sequence, note="", width=66):
        box = layout.box()
        box.label(text=name)
        tokens = sequence.split()
        line = ""
        for token in tokens:
            candidate = (line + " " + token).strip()
            if len(candidate) > width and line:
                box.label(text=line)
                line = token
            else:
                line = candidate
        if line:
            box.label(text=line)
        if note:
            words = note.split()
            line = ""
            for word in words:
                candidate = (line + " " + word).strip()
                if len(candidate) > width and line:
                    box.label(text=line, icon="INFO" if not box else "NONE")
                    line = word
                else:
                    line = candidate
            if line:
                box.label(text=line)

    @staticmethod
    def _note(layout, text, width=88):
        words = text.split()
        line = ""
        for word in words:
            candidate = (line + " " + word).strip()
            if len(candidate) > width and line:
                layout.label(text=line)
                line = word
            else:
                line = candidate
        if line:
            layout.label(text=line)

    def draw(self, context):
        layout = self.layout

        header = layout.box()
        header.label(text="CFOP Algorithm Reference", icon="BOOKMARKS")
        self._note(
            header,
            "Prime (') means counter-clockwise. 2 means 180 degrees. Lowercase r/f are wide turns; M is the middle slice; x/y rotate the entire cube.",
        )
        layout.prop(self, "category", expand=True)

        if self.category == "TRIGGERS":
            info = layout.box()
            info.label(text="F2L / Trigger Building Blocks")
            self._note(info, "These are not a complete F2L case library. Learn how these triggers affect a corner-edge pair, then use U setup turns and mirrors to solve F2L intuitively.")
            col = layout.column(align=False)
            self._algorithm(col, "Sexy Move / Right Trigger", "R U R' U'", "Extremely common pairing and orientation trigger.")
            self._algorithm(col, "Reverse Sexy", "R' U' R U", "Useful mirror/reverse of the standard trigger.")
            self._algorithm(col, "Right Insert", "R U R'", "Insert a prepared pair into the front-right slot.")
            self._algorithm(col, "Left Insert", "L' U' L", "Mirror insertion into the front-left slot.")
            self._algorithm(col, "Sledgehammer", "R' F R F'", "Useful for changing pair orientation and preserving useful pieces.")
            self._algorithm(col, "Hedgehammer", "F R' F' R", "Inverse-style companion to the sledgehammer.")

        elif self.category == "OLL2":
            info = layout.box()
            info.label(text="2-Look OLL")
            self._note(info, "First orient the four U-layer edges to make a cross. Then use one corner-orientation case to make the entire U face one color.")

            row = layout.row()
            left = row.column()
            right = row.column()

            self._algorithm(left, "Edges - Line", "F R U R' U' F'", "Hold the line horizontally.", width=44)
            self._algorithm(left, "Edges - L Shape", "f R U R' U' f'", "Hold the L in the upper-left orientation.", width=44)
            self._algorithm(left, "Edges - Dot", "F R U R' U' F' f R U R' U' f'", "Creates the cross from the dot case.", width=44)
            self._algorithm(left, "Corners - Sune", "R U R' U R U2 R'", width=44)
            self._algorithm(left, "Corners - Anti-Sune", "R U2 R' U' R U' R'", width=44)

            self._algorithm(right, "Corners - H", "R U2 R' U' R U R' U' R U' R'", width=44)
            self._algorithm(right, "Corners - Pi", "R U2 R2 U' R2 U' R2 U2 R", width=44)
            self._algorithm(right, "Corners - Headlights", "R2 D' R U2 R' D R U2 R", width=44)
            self._algorithm(right, "Corners - Bowtie", "r U R' U' r' F R F'", width=44)
            self._algorithm(right, "Corners - T", "F' r U R' U' r' F R", width=44)

        elif self.category == "PLL2":
            info = layout.box()
            info.label(text="2-Look PLL")
            self._note(info, "Solve the last-layer corners first, then permute the four last-layer edges. Use U setup turns to match the case before the algorithm.")

            row = layout.row()
            left = row.column()
            right = row.column()
            self._algorithm(left, "Corners - Headlights", "R U R' U' R' F R2 U' R' U' R U R' F'", width=48)
            self._algorithm(left, "Corners - Diagonal", "F R U' R' U' R U R' F' R U R' U' R' F R F'", width=48)
            self._algorithm(left, "Edges - H Perm", "M2 U M2 U2 M2 U M2", width=48)
            self._algorithm(right, "Edges - Ua Perm", "R U' R U R U R U' R' U' R2", width=48)
            self._algorithm(right, "Edges - Ub Perm", "R2 U R U R' U' R' U' R' U R'", width=48)
            self._algorithm(right, "Edges - Z Perm", "M' U M2 U M2 U M' U2 M2", width=48)

        else:  # PLLFULL
            info = layout.box()
            info.label(text="Full PLL - 21 Cases")
            self._note(info, "One representative algorithm is shown for each PLL. Recognition and good finger tricks matter as much as memorizing the sequence.")

            pll = [
                ("Aa", "x R' U R' D2 R U' R' D2 R2 x'"),
                ("Ab", "x R2 D2 R U R' D2 R U' R x'"),
                ("E", "y x' R U' R' D R U R' D' R U R' D R U' R' D' x"),
                ("F", "y R' U' F' R U R' U' R' F R2 U' R' U' R U R' U R"),
                ("Ga", "R2 U R' U R' U' R U' R2 D U' R' U R D'"),
                ("Gb", "R' U' R U D' R2 U R' U R U' R U' R2 D"),
                ("Gc", "R2 U' R U' R U R' U R2 D' U R U' R' D"),
                ("Gd", "R U R' U' D R2 U' R U' R' U R' U R2 D'"),
                ("H", "M2 U' M2 U2 M2 U' M2"),
                ("Ja", "y R' U L' U2 R U' R' U2 R L"),
                ("Jb", "R U R' F' R U R' U' R' F R2 U' R'"),
                ("Na", "R U R' U R U R' F' R U R' U' R' F R2 U' R' U2 R U' R'"),
                ("Nb", "R' U R U' R' F' U' F R U R' F R' F' R U' R"),
                ("Ra", "y R U' R' U' R U R D R' U' R D' R' U2 R'"),
                ("Rb", "R' U2 R U2 R' F R U R' U' R' F' R2"),
                ("T", "R U R' U' R' F R2 U' R' U' R U R' F'"),
                ("Ua", "R U R' U R' U' R2 U' R' U R' U R"),
                ("Ub", "R' U R' U' R' U' R' U R U R2"),
                ("V", "R' U R' U' R D' R' D R' U D' R2 U' R2 D R2"),
                ("Y", "F R U' R' U' R U R' F' R U R' U' R' F R F'"),
                ("Z", "M' U' M2 U' M2 U' M' U2 M2"),
            ]

            row = layout.row()
            left = row.column()
            right = row.column()
            split = (len(pll) + 1) // 2
            for name, sequence in pll[:split]:
                self._algorithm(left, f"{name} Perm", sequence, width=49)
            for name, sequence in pll[split:]:
                self._algorithm(right, f"{name} Perm", sequence, width=49)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=920)

    def execute(self, context):
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

        manual = layout.box()
        manual.label(text="2. Manual Turns")
        for faces in (("U", "D"), ("R", "L"), ("F", "B")):
            row = manual.row(align=True)
            for face in faces:
                for token, label in ((face, face), (face + "'", face + "′"), (face + "2", face + "2")):
                    op = row.operator("rubik.turn", text=label)
                    op.move = token
        queued = len(get_manual_queue(scene))
        if scene.rc_manual_busy or queued:
            manual.label(text=f"Turn queue: {queued + (1 if scene.rc_manual_busy else 0)}")

        solve = layout.box()
        solve.label(text="3. Scramble / Solve")
        solve.prop(scene, "rc_scramble_moves")
        row = solve.row(align=True)
        row.operator("rubik.scramble", icon="FILE_REFRESH")
        row.operator("rubik.solve", icon="PLAY")
        solve.label(text=f"Recorded: {len(get_history(scene))} moves")

        methods = layout.box()
        methods.label(text="4. Speed Solving Methods")
        methods.operator("rubik.method_scramble", text="Random Scramble", icon="FILE_REFRESH")
        methods.prop(scene, "rc_tutorial_mode")
        methods.prop(scene, "rc_show_face_labels")
        if scene.rc_tutorial_mode:
            methods.prop(scene, "rc_tutorial_pause_frames")

        row = methods.row(align=True)
        cfop_col = row.column(align=True)
        cfop_col.enabled = cfop_plan_is_valid(scene)
        cfop_col.operator("rubik.solve_cfop", text="CFOP (Human)", icon="PLAY")
        row.operator("rubik.solve_two_phase", text="Two-Phase (Computer)", icon="PLAY")

        if not cfop_plan_is_valid(scene):
            methods.label(text="CFOP: use Random Scramble above to create a teachable case.", icon="INFO")
        if scene.rc_method_status:
            methods.label(text=scene.rc_method_status)

        if scene.rc_tutorial_mode and (scene.rc_tutorial_stage or scene.rc_tutorial_active):
            tutorial = methods.box()
            tutorial.label(text=f"Tutorial: {scene.rc_tutorial_stage or 'Ready'}")
            if scene.rc_tutorial_move:
                tutorial.label(text=f"Move: {scene.rc_tutorial_move}")
            if scene.rc_tutorial_algorithm:
                tutorial.label(text="Algorithm / phase moves:")
                alg_words = scene.rc_tutorial_algorithm.split()
                alg_line = ""
                for alg_word in alg_words:
                    alg_candidate = (alg_line + " " + alg_word).strip()
                    if len(alg_candidate) > 42 and alg_line:
                        tutorial.label(text=alg_line)
                        alg_line = alg_word
                    else:
                        alg_line = alg_candidate
                if alg_line:
                    tutorial.label(text=alg_line)
            text = scene.rc_tutorial_text or "Start CFOP or Two-Phase to see explanations here."
            words = text.split()
            line = ""
            for word in words:
                candidate = (line + " " + word).strip()
                if len(candidate) > 48 and line:
                    tutorial.label(text=line)
                    line = word
                else:
                    line = candidate
            if line:
                tutorial.label(text=line)
            if scene.rc_tutorial_active:
                label = "Resume" if scene.rc_tutorial_paused else "Pause"
                icon = "PLAY" if scene.rc_tutorial_paused else "PAUSE"
                tutorial.operator("rubik.tutorial_pause_resume", text=label, icon=icon)

        animation = layout.box()
        animation.label(text="Animation")
        animation.prop(scene, "rc_turn_frames")
        animation.prop(scene, "rc_pause_frames")

        advanced = layout.box()
        advanced.label(text="Build Options")
        advanced.prop(scene, "rc_add_studio")
        advanced.prop(scene, "rc_build_section_frames")
        advanced.prop(scene, "rc_build_gap_frames")

        guide = layout.box()
        guide.label(text="5. Human Solving Instructions")
        guide.label(text="Learn CFOP from a random cube state.")
        guide.operator("rubik.human_solve_guide", text="Open Human Solve Guide", icon="HELP")
        guide.operator("rubik.algorithm_reference", text="Open Algorithm Reference", icon="BOOKMARKS")


CLASSES = (
    RC_OT_create_cube,
    RC_OT_turn,
    RC_OT_scramble,
    RC_OT_solve,
    RC_OT_method_scramble,
    RC_OT_solve_cfop,
    RC_OT_solve_two_phase,
    RC_OT_tutorial_pause_resume,
    RC_OT_human_solve_guide,
    RC_OT_algorithm_reference,
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
    bpy.types.Scene.rc_tutorial_mode = BoolProperty(
        name="Tutorial Mode",
        description="Show method explanations during the solve and enable Pause / Resume",
        default=True,
    )
    bpy.types.Scene.rc_tutorial_pause_frames = IntProperty(
        name="Tutorial Pause Between Moves",
        description="Section 4 only: hold this many frames after each tutorial move so the learner can follow the solve",
        default=12,
        min=0,
        max=240,
    )
    bpy.types.Scene.rc_show_face_labels = BoolProperty(
        name="Viewport Face Labels",
        description="Show U/U′, R/R′, F/F′, D/D′, L/L′, and B/B′ notation labels on the cube faces in the 3D viewport",
        default=True,
        update=_face_labels_toggle_update,
    )
    bpy.types.Scene.rc_tutorial_active = BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})
    bpy.types.Scene.rc_tutorial_paused = BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})
    bpy.types.Scene.rc_tutorial_stage = StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})
    bpy.types.Scene.rc_tutorial_text = StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})
    bpy.types.Scene.rc_tutorial_move = StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})
    bpy.types.Scene.rc_tutorial_algorithm = StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})
    bpy.types.Scene.rc_tutorial_method = StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})
    bpy.types.Scene.rc_method_status = StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})
    bpy.types.Scene.rc_cfop_plan = StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})
    bpy.types.Scene.rc_cfop_signature = StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})
    bpy.types.Scene.rc_history = StringProperty(default="")
    bpy.types.Scene.rc_manual_queue = StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})
    bpy.types.Scene.rc_manual_busy = BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})
    bpy.types.Scene.rc_last_animation_frame = IntProperty(default=1, min=1, options={"HIDDEN", "SKIP_SAVE"})
    bpy.types.Scene.rc_center = FloatVectorProperty(size=3, default=(0.0, 0.0, 0.0))
    bpy.types.Scene.rc_build_end_frame = IntProperty(default=64, min=1)


def unregister():
    stop_animation_playback()
    _ONE_SHOT_END_FRAMES.clear()
    _MANUAL_START_PENDING.clear()
    _TUTORIAL_TRACKS.clear()
    try:
        bpy.app.handlers.frame_change_post.remove(_one_shot_frame_change)
    except ValueError:
        pass

    for prop in (
        "rc_build_end_frame",
        "rc_center",
        "rc_last_animation_frame",
        "rc_manual_busy",
        "rc_manual_queue",
        "rc_cfop_signature",
        "rc_cfop_plan",
        "rc_method_status",
        "rc_tutorial_method",
        "rc_tutorial_algorithm",
        "rc_tutorial_move",
        "rc_tutorial_text",
        "rc_tutorial_stage",
        "rc_tutorial_paused",
        "rc_tutorial_active",
        "rc_show_face_labels",
        "rc_tutorial_pause_frames",
        "rc_tutorial_mode",
        "rc_history",
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
