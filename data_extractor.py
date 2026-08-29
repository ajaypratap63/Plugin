# ==============================================================================
# generalized_extractor.py
#
# GENERALIZED Abaqus/CAE (odbAccess) post-processor.
#
# WHAT CHANGED VS. THE ORIGINAL SCRIPT
# -------------------------------------------------------------------------
# The original script had one hand-written code path per variable name
# (S, E, SENER, CENER, DMENER, ELSE, EENER, U, ...). Every new variable
# required copy/pasting a new "block". This version instead asks Abaqus
# itself, at run time, HOW a requested output variable is stored:
#
#   fo = frame.fieldOutputs[varName]
#   fo.locations[i].position   -> INTEGRATION_POINT / CENTROID / WHOLE_ELEMENT
#                                  / ELEMENT_FACE / NODAL / ELEMENT_NODAL / ...
#   fo.type                    -> SCALAR / VECTOR / TENSOR_3D_FULL / ...
#
# From (position, type) we pick ONE of a small number of generic averaging
# strategies. Each strategy still uses bulkDataBlocks exactly like the
# original script (fast, vectorized with numpy) -- nothing is looped
# element-by-element in Python except where Abaqus forces it (nodal maps).
#
# CATEGORY -> STRATEGY MAPPING
# -------------------------------------------------------------------------
#   Element integration point variables    -> position=INTEGRATION_POINT
#                                             weight = IVOL   (per phase)
#   Element centroidal variables           -> position=CENTROID
#                                             weight = EVOL   (per phase)
#   Element section variables              -> position=INTEGRATION_POINT/
#                                             CENTROID depending on element
#                                             (SF, SM, SE, STH, ...) -> same
#                                             as integration-point strategy
#   Whole element variables                -> position=WHOLE_ELEMENT
#                                             (already element totals, e.g.
#                                             ELEN, ELSE, ELPD, NFORC ...)
#                                             weight = EVOL, values SUMMED
#                                             then divided by total volume
#                                             to give a density-style average
#                                             (matches ELSE handling in the
#                                             original script) plus a raw
#                                             SUM per phase.
#   Element face variables                 -> position=ELEMENT_FACE
#                                             equal-weighted average (no
#                                             generic per-face area exists
#                                             in the field output itself)
#   Whole element energy density vars      -> same as WHOLE_ELEMENT strategy
#   Whole element error indicator vars     -> same as WHOLE_ELEMENT strategy
#   Nodal variables                        -> position=NODAL / ELEMENT_NODAL
#                                             equal-weighted nodal average,
#                                             mapped to phases through the
#                                             element->node connectivity
#                                             (generalizes the old U-only code)
#   Surface variables                      -> position=NODAL on a *surface*
#                                             region (not an element/material
#                                             region) -> handled by a separate
#                                             surface-averaging routine,
#                                             weighted by CNAREA if present
#   Modal / Section / Whole-and-partial-   -> these are almost always
#   model / Solution-dependent amplitude /    *automatic* HISTORY outputs
#   Structural optimization variables         (EIGVAL, GM, LPF, ALLIE, SOF,
#                                             MAT_PROP_NORMALIZED, ...) and
#                                             are NOT decomposed by material
#                                             phase. These are pulled directly
#                                             from step.historyRegions[...]
#                                             .historyOutputs[var].data, or,
#                                             if they exist as a WHOLE_MODEL /
#                                             WHOLE_REGION field output, via
#                                             the WHOLE_ELEMENT-style strategy
#                                             with no phase decomposition.
#
# The user only has to supply variable NAMES (e.g. "S", "PEEQ", "HFL",
# "MISESAVG", "CTF1", "EIGFREQ", "SOF" ...). Everything else -- which
# element position it lives at, whether it is a tensor needing Mandel/
# Kelvin reordering, whether it can be split by phase, whether it must be
# read from history instead of field output -- is resolved automatically.
# ==============================================================================

from abaqus import session
from visualization import *
from abaqusConstants import *
import odbAccess
import numpy as np
import math
import time

NTENS = 6

# ---------- ORDER FIX: Abaqus -> Mandel (Kelvin) ----------
# Abaqus tensor order: [11,22,33,12,13,23] -> indices [0,1,2,3,4,5]
# Mandel order:        [11,22,33,23,13,12] -> indices [0,1,2,5,4,3]
ABAQUS_TO_MANDEL = np.array([0, 1, 2, 5, 4, 3], dtype=int)
SIG_SCALE = np.array([1.0, 1.0, 1.0, np.sqrt(2.0), np.sqrt(2.0), np.sqrt(2.0)])   # stress-like
EPS_SCALE = np.array([1.0, 1.0, 1.0, 1.0/np.sqrt(2.0), 1.0/np.sqrt(2.0), 1.0/np.sqrt(2.0)])  # strain-like

# Abaqus SymbolicConstants that mean "this is a tensor stored as engineering
# strain (needs the 1/sqrt(2) shear scaling)" vs. "stress-like" (sqrt(2)
# scaling). We can't always tell from fo.type alone, so we keep a small,
# EXTENSIBLE set of name fragments that are strain-like; everything else
# that is a 6-component tensor is treated as stress-like. This is the only
# place a user must add a name if they introduce a custom strain variable
# that isn't already covered (E, LE, NE, EE, IE, PE, CE, THE, ER, SE, SEE...).
STRAIN_LIKE_HINTS = ("E", "STRAIN")


def is_strain_like(varname):
    v = varname.upper()
    return v.startswith(("E", "LE", "NE", "EE", "IE", "PE", "CE", "THE", "ER", "SPE", "SEPE", "SEE", "SKE")) \
        or "STRAIN" in v


def to_mandel_kelvin(vec_or_array, scale):
    """Reorder Abaqus 6-vector(s) to Mandel order and apply Kelvin scaling."""
    a = np.asarray(vec_or_array, dtype=float)
    if a.ndim == 1:
        return a[ABAQUS_TO_MANDEL] * scale
    return a[:, ABAQUS_TO_MANDEL] * scale[None, :]


def get_principal_and_mises(mandel_vec):
    """3 principal values + Mises equivalent from a Mandel/Kelvin 6-vector."""
    s11, s22, s33 = mandel_vec[0], mandel_vec[1], mandel_vec[2]
    s23 = mandel_vec[3] / np.sqrt(2.0)
    s13 = mandel_vec[4] / np.sqrt(2.0)
    s12 = mandel_vec[5] / np.sqrt(2.0)
    m = np.array([[s11, s12, s13], [s12, s22, s23], [s13, s23, s33]])
    try:
        ev = np.linalg.eigvalsh(m)
        p1, p2, p3 = sorted(ev, reverse=True)
        mises = np.sqrt(0.5 * ((p1 - p2) ** 2 + (p2 - p3) ** 2 + (p3 - p1) ** 2))
        return [p1, p2, p3, mises]
    except Exception:
        return [0.0, 0.0, 0.0, 0.0]

#Incorrect implementation for field variables need ToDo
# ==============================================================================
# STEP 1: classify a requested variable by asking Abaqus how it is stored
# ==============================================================================

# Fallback table used only when a variable is not present as a field output in
# the current frame (e.g. it's zero at frame 0) but IS present in later frames,
# or when a variable is a pure history output. Extend as needed.
KNOWN_WHOLE_MODEL_HISTORY_VARS = set([
    "EIGVAL", "EIGFREQ", "GM", "CD", "EIGREAL", "EIGIMAG", "DAMPRATIO",
    "LPF", "AMPCU", "RATIO",
    "ALLAE", "ALLCCDW", "ALLCCEN", "ALLCCET", "ALLCCE", "ALLCCSDN", "ALLCCSDT",
    "ALLCCSD", "ALLCD", "ALLEE", "ALLFD", "ALLIE", "ALLJD", "ALLKE", "ALLKL",
    "ALLPD", "ALLQB", "ALLSD", "ALLSE", "ALLVD", "ALLDMD", "ALLWK", "ETOTAL",
    "MAT_PROP_NORMALIZED", "CTRL_INPUT", "DISP_OPT_VAL", "DISP_OPT",
    "THICKNESS", "DELTA_THICKNESS", "DISP_NORMAL_VAL",
])


class VarInfo(object):
    """Everything needed to decide how to average a given output variable."""
    def __init__(self, name, position, dtype, ncomp, is_tensor, source):
        self.name = name
        self.position = position     # SymbolicConstant or None
        self.dtype = dtype           # SCALAR / VECTOR / TENSOR_* / None
        self.ncomp = ncomp           # number of stored components
        self.is_tensor = is_tensor   # True if 6-component tensor
        self.source = source         # 'field' or 'history' or 'missing'


def classify_variable(frame, varname):
    """
    Inspect frame.fieldOutputs[varname] (if present) and return a VarInfo
    describing how the variable is stored. Falls back to 'history' for
    known whole-model / modal / optimization scalars, else 'missing'.
    """
    fo_dict = frame.fieldOutputs
    if varname in fo_dict:
        fo = fo_dict[varname]
        positions = [loc.position for loc in fo.locations]
        # priority order matches the category list in the module docstring
        for pref in (INTEGRATION_POINT, CENTROID, WHOLE_ELEMENT,
                     ELEMENT_FACE, ELEMENT_NODAL, NODAL):
            if pref in positions:
                pos = pref
                break
        else:
            pos = positions[0] if positions else None

        # peek one value's component count from the first non-empty block
        ncomp = 1
        for blk in fo.bulkDataBlocks:
            arr = np.asarray(blk.data)
            if arr.size:
                ncomp = arr.shape[1] if arr.ndim > 1 else 1
                break
        is_tensor = (ncomp == NTENS)
        return VarInfo(varname, pos, fo.type, ncomp, is_tensor, 'field')

    if varname in KNOWN_WHOLE_MODEL_HISTORY_VARS:
        return VarInfo(varname, None, None, None, False, 'history')

    return VarInfo(varname, None, None, None, False, 'missing')


# ==============================================================================
# STEP 2: generic per-phase averaging strategies (all use bulkDataBlocks)
# ==============================================================================

def _weight_field(frame, reg, position):
    """Return the correct volume/weight FieldOutput for a given position."""
    if position in (INTEGRATION_POINT,):
        return frame.fieldOutputs['IVOL'].getSubset(region=reg, position=INTEGRATION_POINT)
    # CENTROID / WHOLE_ELEMENT / ELEMENT_FACE all weight by element volume
    if 'EVOL' in frame.fieldOutputs:
        try:
            return frame.fieldOutputs['EVOL'].getSubset(region=reg)
        except Exception:
            return None
    return None


def average_element_based_variable(frame, var_info, reg):
    """
    Handles INTEGRATION_POINT, CENTROID, WHOLE_ELEMENT, ELEMENT_FACE positions.

    Returns (avg[ncomp], weighted_total[ncomp], weight_sum) where
    weighted_total / weight_sum == avg for THIS region, and -- critically --
    weighted_total and weight_sum are exactly the two numbers that must be
    summed *across phases* before dividing, to get a correct overall
    (whole-model) average. This mirrors the original script's approach of
    accumulating SigV_tot / Vtot from the still-volume-weighted per-phase
    sums, rather than re-averaging already-averaged phase values.

    - INTEGRATION_POINT / CENTROID (intensive quantities like stress,
      strain, temperature, ...): weighted_total = Sum(data * volume),
      weight_sum = Sum(volume)  -> true volume-weighted average.
    - WHOLE_ELEMENT (already-integrated element totals like ELSE, ELEN,
      NFORC, ...): weighted_total = Sum(data)  [NOT multiplied by volume,
      since the value is already a per-element total], weight_sum =
      Sum(element volume) -> gives a density-style average, matching the
      original ELSE handling.
    - ELEMENT_FACE: no generic per-face area is available from the field
      output itself, so weighted_total = Sum(data), weight_sum = number of
      face values (equal-weighted average).
    """
    fo = frame.fieldOutputs[var_info.name]
    pos = var_info.position
    kwargs = {'region': reg}
    if pos in (INTEGRATION_POINT, CENTROID, ELEMENT_FACE):
        kwargs['position'] = pos
    sub = fo.getSubset(**kwargs)

    ncomp = var_info.ncomp
    acc = np.zeros(ncomp)
    wsum = 0.0

    if pos == WHOLE_ELEMENT:
        wfo = _weight_field(frame, reg, pos)
        for b in range(len(sub.bulkDataBlocks)):
            data = np.asarray(sub.bulkDataBlocks[b].data, dtype=float)
            if data.ndim == 1:
                data = data.reshape(-1, 1)
            if var_info.is_tensor:
                scale = EPS_SCALE if is_strain_like(var_info.name) else SIG_SCALE
                data = to_mandel_kelvin(data, scale)
            acc[:data.shape[1]] += data.sum(axis=0)   # already a per-element total
            if wfo is not None and b < len(wfo.bulkDataBlocks):
                wsum += np.asarray(wfo.bulkDataBlocks[b].data, dtype=float).reshape(-1).sum()
            else:
                wsum += data.shape[0]

    elif pos == ELEMENT_FACE:
        for b in range(len(sub.bulkDataBlocks)):
            data = np.asarray(sub.bulkDataBlocks[b].data, dtype=float)
            if data.ndim == 1:
                data = data.reshape(-1, 1)
            if var_info.is_tensor:
                scale = EPS_SCALE if is_strain_like(var_info.name) else SIG_SCALE
                data = to_mandel_kelvin(data, scale)
            acc[:data.shape[1]] += data.sum(axis=0)
            wsum += data.shape[0]                      # equal weighting

    else:  # INTEGRATION_POINT / CENTROID -> true volume-weighted average
        wfo = _weight_field(frame, reg, pos)
        for b in range(len(sub.bulkDataBlocks)):
            data = np.asarray(sub.bulkDataBlocks[b].data, dtype=float)
            if data.ndim == 1:
                data = data.reshape(-1, 1)
            if var_info.is_tensor:
                scale = EPS_SCALE if is_strain_like(var_info.name) else SIG_SCALE
                data = to_mandel_kelvin(data, scale)
            if wfo is not None and b < len(wfo.bulkDataBlocks):
                w = np.asarray(wfo.bulkDataBlocks[b].data, dtype=float).reshape(-1)
            else:
                w = np.ones(data.shape[0])
            acc[:data.shape[1]] += (data * w[:, None]).sum(axis=0)   # Sum(data * volume)
            wsum += w.sum()                                          # Sum(volume)

    avg = acc / wsum if wsum > 0 else acc * 0.0
    return avg, acc, wsum


def build_phase_node_sets(phases):
    """Element-set phases -> set of (instanceName, nodeLabel) per phase.
    Needed for NODAL / ELEMENT_NODAL variables, which aren't natively tied
    to an element region the way IVOL/EVOL-weighted variables are."""
    phase_node_sets = [set() for _ in phases]
    for pj, ph in enumerate(phases):
        reg = ph['region']
        if hasattr(reg, 'elements') and reg.elements is not None:
            for elem in reg.elements:
                for nl in elem.connectivity:
                    phase_node_sets[pj].add((ph['instance'], nl))
    return phase_node_sets

#Incorrect expression implemented has ToDo
def average_nodal_variable(frame, var_info, phases, phase_node_sets):
    """
    Generic replacement for the old U-only nodal averaging block.
    Equal-weighted nodal average, both overall and per phase.
    """
    fo = frame.fieldOutputs[var_info.name]
    ncomp = var_info.ncomp
    nph = len(phases)

    total_sum = np.zeros(ncomp)
    total_n = 0
    phase_sum = np.zeros((nph, ncomp))
    phase_n = np.zeros(nph, dtype=int)

    for blk in fo.bulkDataBlocks:
        inst_name = blk.instance.name if blk.instance else None
        data = np.asarray(blk.data, dtype=float)
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        labels = blk.nodeLabels
        for i in range(len(labels)):
            row = data[i]
            total_sum += row
            total_n += 1
            ident = (inst_name, labels[i])
            for pj in range(nph):
                if ident in phase_node_sets[pj]:
                    phase_sum[pj] += row
                    phase_n[pj] += 1

    overall_avg = total_sum / total_n if total_n > 0 else total_sum * 0.0
    phase_avg = np.zeros((nph, ncomp))
    for pj in range(nph):
        if phase_n[pj] > 0:
            phase_avg[pj] = phase_sum[pj] / phase_n[pj]
    return overall_avg, phase_avg


def average_surface_variable(frame, varname, surfaceRegion):
    """
    Surface-category variables (CSTRESS, CPRESS, CDISP, HFL on a contact
    surface, ...). These are NOT split by material phase because a contact
    surface is not a material region. Weighted by CNAREA if present.
    """
    if varname not in frame.fieldOutputs:
        return None
    fo = frame.fieldOutputs[varname].getSubset(region=surfaceRegion)
    area_fo = None
    if 'CNAREA' in frame.fieldOutputs:
        try:
            area_fo = frame.fieldOutputs['CNAREA'].getSubset(region=surfaceRegion)
        except Exception:
            area_fo = None

    ncomp = 1
    for blk in fo.bulkDataBlocks:
        arr = np.asarray(blk.data)
        if arr.size:
            ncomp = arr.shape[1] if arr.ndim > 1 else 1
            break

    acc = np.zeros(ncomp)
    wsum = 0.0
    for b in range(len(fo.bulkDataBlocks)):
        data = np.asarray(fo.bulkDataBlocks[b].data, dtype=float)
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        if area_fo is not None and b < len(area_fo.bulkDataBlocks):
            w = np.asarray(area_fo.bulkDataBlocks[b].data, dtype=float).reshape(-1)
        else:
            w = np.ones(data.shape[0])
        acc[:data.shape[1]] += (data * w[:, None]).sum(axis=0)
        wsum += w.sum()
    return acc / wsum if wsum > 0 else acc * 0.0


def extract_history_series(odb, stepName, varname):
    """
    Modal / whole-model / solution-dependent-amplitude / structural
    optimization variables: pulled straight from history output, which is
    where Abaqus writes these 'automatic' whole-model/-mode quantities.
    Returns dict {historyRegionName: [(t, value), ...]} (there can be one
    entry per mode, per assembly node set, etc.)
    """
    step = odb.steps[stepName]
    out = {}
    for hrName, hr in step.historyRegions.items():
        if varname in hr.historyOutputs:
            out[hrName] = hr.historyOutputs[varname].data
    return out


# ==============================================================================
# STEP 3: main driver
# ==============================================================================

# ===== [ADDED] .m file support: comment-writer helper =====
# The output file is now a MATLAB script (.m), not a plain .txt report.
# MATLAB will refuse to run/load a script that contains ANY line that is
# neither valid MATLAB code nor a comment -- so every purely descriptive/
# informational line written below (headers, labels, warnings, notes)
# is routed through this helper, which prefixes each line with '%' so it
# becomes a MATLAB comment. Only actual "name = value;" / matrix-literal
# lines are written as real, executable MATLAB code elsewhere below.
def _mwrite_comment(f, text):
    for line in str(text).split("\n"):
        f.write("% " + line + "\n")
# ===== [END ADDED] =====


def run_computation(odbName, stepName, outName,
                     v1='', v2='', v3='', v4='', v5='',
                     v6='', v7='', v8='', v9='', v10='',
                     requested_vars=None, surfaceName=None,
                     fullTensor=True, mandelNotation=True, voigtNotation=True):
    """
    Backward-compatible signature: the GUI (my_gui.py) calls this the same
    way it called the original script --
        run_computation(odbName, stepName, outName, v1, v2, ..., v10)
    -- so v1..v10 are accepted positionally exactly as before (now extended
    from 5 to 10 slots) and internally collapsed into a single
    requested_vars list. You can ALSO pass a ready list directly via the
    requested_vars= keyword (e.g. from a non-GUI script), and/or mix both --
    duplicates are removed automatically.

    requested_vars: optional list of variable name strings, e.g.
        ['S', 'E', 'PEEQ', 'HFL', 'CTF1', 'MISESAVG', 'EIGFREQ', 'SOF']
    Each is independently classified and routed to the correct averaging
    strategy -- nothing is hard-coded per variable name.

    fullTensor / mandelNotation / voigtNotation: [ADDED] control whether
    the extra "MATLAB Variable Assignments" section (below) is written for
    requested variable that turns out to be a 6-component TENSOR. This
    reuses the SAME already-computed 'results' dict built during the ODB
    pass further down in this function -- no second ODB session is opened
    and nothing is re-read from disk to produce it.
    """
    start = time.time()

    gui_vars = [v.strip().upper() for v in [v1, v2, v3, v4, v5, v6, v7, v8, v9, v10] if v and v.strip()]
    extra_vars = [v.strip().upper() for v in (requested_vars or []) if v and v.strip()]
    requested_vars = []
    for v in gui_vars + extra_vars:
        if v not in requested_vars:
            requested_vars.append(v)

    if not requested_vars:
        raise ValueError("run_computation: no output variables were supplied "
                          "(v1..v5 and/or requested_vars were all empty).")
    odb = session.openOdb(name=odbName, readOnly=True)
    step = odb.steps[stepName]
    frames = step.frames
    nframes = len(frames)

    # -------- phases (material regions), same as the original script -----
    phases = []
    for inst in odb.rootAssembly.instances.values():
        for sa in inst.sectionAssignments:
            secName = getattr(sa, 'sectionName', 'UNKNOWN_SECTION')
            try:
                matName = odb.sections[secName].material
            except Exception:
                matName = 'UNKNOWN_MATERIAL'
            phases.append({'instance': inst.name, 'section': secName,
                            'material': matName, 'region': sa.region})
    nph = len(phases)
    phase_node_sets = build_phase_node_sets(phases)

    surfaceRegion = None
    if surfaceName is not None and surfaceName in odb.rootAssembly.surfaces:
        surfaceRegion = odb.rootAssembly.surfaces[surfaceName]

    # results[var] = {'t': [...], 'overall': [...per frame vector...],
    #                 'phase': [ [...per frame vector...] per phase ] }
    results = {v: {'t': [0.0] * nframes,
                    'overall': [None] * nframes,
                    'phase': [[None] * nframes for _ in range(nph)],
                    'principal_mises': [None] * nframes if False else None,
                    'source': None}
               for v in requested_vars}
    missing_vars = set()
    history_results = {}   # var -> extract_history_series output (single call)

    # [CHANGED] Frame 0 used to be hardcoded-skipped here (range started at
    # 1) on the assumption it never has field output. It is now included:
    # classify_variable() already checks, per frame, whether a variable's
    # fieldOutputs entry actually exists, so if frame 0 truly has nothing
    # for a given variable it is handled exactly like any other frame with
    # no data for that variable (results[v]['overall'][0] stays None and
    # is naturally skipped when writing output) -- but if frame 0 DOES
    # have field output (e.g. an initial/predefined state), it is no
    # longer silently discarded.
    found_data = dict((v, False) for v in requested_vars)  # [ADDED] tracks whether v got ANY frame's data

    for fi in range(nframes):
        fr = frames[fi]
        for v in requested_vars:
            results[v]['t'][fi] = fr.frameValue

        for v in requested_vars:
            info = classify_variable(fr, v)
            results[v]['source'] = info.source

            if info.source == 'missing':
                # [FIXED] no longer marks v globally missing here -- this
                # frame simply has no data for v; other frames may still
                # have it (e.g. frame 0's initial state often lacks S/E).
                continue

            if info.source == 'history':
                # history is a single call, not per-frame -> handled once below
                continue

            pos = info.position
            if pos in (INTEGRATION_POINT, CENTROID, WHOLE_ELEMENT, ELEMENT_FACE):
                overall_num = np.zeros(info.ncomp)
                overall_den = 0.0
                for pj, ph in enumerate(phases):
                    avg, weighted_total, wsum = average_element_based_variable(fr, info, ph['region'])
                    results[v]['phase'][pj][fi] = avg.tolist()
                    # Accumulate the *weighted* total (not a plain unweighted
                    # sum) across phases, exactly like the original script's
                    # SigV_tot += SigV[pj]; Avg_Stress = SigV_tot / Vtot.
                    overall_num += weighted_total
                    overall_den += wsum
                overall_avg = overall_num / overall_den if overall_den > 0 else overall_num * 0.0
                results[v]['overall'][fi] = overall_avg.tolist()
                found_data[v] = True  # [ADDED]

            elif pos in (NODAL, ELEMENT_NODAL):
                overall_avg, phase_avg = average_nodal_variable(fr, info, phases, phase_node_sets)
                results[v]['overall'][fi] = overall_avg.tolist()
                for pj in range(nph):
                    results[v]['phase'][pj][fi] = phase_avg[pj].tolist()
                found_data[v] = True  # [ADDED]

            else:
                pass  # [FIXED] see below -- do not mark missing per-frame

    # history-sourced variables: pulled once (they already carry their own
    # time axis and are whole-model/whole-mode by nature, no phase split)
    for v in requested_vars:
        info0 = classify_variable(frames[min(1, nframes - 1)], v)
        if info0.source == 'history':
            history_results[v] = extract_history_series(odb, stepName, v)

    # [ADDED] Now that every frame (and the history pass above) has been
    # checked, a variable is only flagged as truly missing if it never
    # produced data in ANY frame and isn't a history variable either --
    # not just because one particular frame (e.g. frame 0) lacked it.
    for v in requested_vars:
        if v not in history_results and not found_data[v]:
            missing_vars.add(v)

    # surface variables (optional, only if a surface name was supplied)
    surface_results = {}
    if surfaceRegion is not None:
        for v in requested_vars:
            series = [0.0] * nframes
            for fi in range(nframes):
                val = average_surface_variable(frames[fi], v, surfaceRegion)
                if val is not None:
                    series[fi] = val.tolist()
            if any(s != 0.0 for s in series):
                surface_results[v] = series

# -------------------- WRITE OUTPUT (.m file) --------------------
    with open(outName, 'w') as f:
        # ===== [ADDED] .m file header comment block =====
        # Marks this as a generated MATLAB script and explains, up top,
        # how to read it: '%' lines are comments/context only, everything
        # else is a real MATLAB variable assignment that becomes usable
        # in the workspace the moment this file is run/opened in MATLAB.
        _mwrite_comment(f, "Auto-generated MATLAB data file")
        _mwrite_comment(f, "Source ODB : %s" % odbName)
        _mwrite_comment(f, "Step       : %s" % stepName)
        _mwrite_comment(f, "Generated by data_extractor.py (run_computation)")
        _mwrite_comment(f, "")
        _mwrite_comment(f, "Every '%' line in this file is a comment/context note only.")
        _mwrite_comment(f, "Every other line is a real MATLAB assignment -- run or open")
        _mwrite_comment(f, "this file in MATLAB and each named variable below becomes")
        _mwrite_comment(f, "directly available in the workspace.")
        f.write("\n")
        # ===== [END ADDED] =====

        # Calculate volume fractions at first nonzero frame (typically frame index 1)
        vol_fractions = [0.0] * nph
        total_vol = 0.0
        if nframes > 1:
            fr1 = frames[1]
            vol_fo = None
            if 'EVOL' in fr1.fieldOutputs:
                vol_fo = fr1.fieldOutputs['EVOL']
            elif 'IVOL' in fr1.fieldOutputs:
                vol_fo = fr1.fieldOutputs['IVOL']

            if vol_fo is not None:
                for pj, ph in enumerate(phases):
                    try:
                        sub = vol_fo.getSubset(region=ph['region'])
                        v_sum = 0.0
                        for blk in sub.bulkDataBlocks:
                            d = np.asarray(blk.data, dtype=float)
                            v_sum += d.sum()
                        vol_fractions[pj] = v_sum
                        total_vol += v_sum
                    except Exception:
                        pass
                if total_vol > 0:
                    vol_fractions = [vf / total_vol for vf in vol_fractions]

        # Phase volume fractions are variable-independent, so they are
        # written once, up front, rather than repeated inside every
        # variable's block (as the old per-phase-first loop used to do).
        # Each volume fraction gets its own self-describing MATLAB
        # assignment name (VF_phaseN = value;), generic across however
        # many phases exist, so it is picked up as a real variable when
        # this .m file is run/opened in MATLAB.
        _mwrite_comment(f, "Phase volume fractions (at first nonzero frame):")
        for pj, ph in enumerate(phases):
            vf_name = "VF_phase%d" % (pj + 1)
            _mwrite_comment(f, "  phase %d (%s / %s / %s)" %
                             (pj + 1, ph['instance'], ph['section'], ph['material']))
            f.write("%s = %.6e;\n" % (vf_name, vol_fractions[pj]))
        f.write("\n")

        t_list = [0.0] * nframes
        if requested_vars and requested_vars[0] in results:
            t_list = results[requested_vars[0]]['t']

        # ===== [CHANGED] the old compact "[VAR] overall (...) / t=.. /
        # overall=[None, [...]] / phase N=[...]" block wrote Python list
        # literals (including the Python-only token 'None') -- that is
        # NOT valid MATLAB syntax, so it can no longer be written as
        # executable code now that the output is a .m file. It is kept
        # here only as a human-readable '%' comment summary; the actual,
        # MATLAB-loadable per-frame variables for every quantity are
        # written further below in the "MATLAB Variable Assignments"
        # section (unchanged logic, just now guaranteed comment-safe).
        for v in requested_vars:
            if v in missing_vars or v in history_results:
                continue
            overall_data = results[v]['overall']

            _mwrite_comment(f, "[%s] overall (volume/area/count-weighted) -- summary only" % v)
            _mwrite_comment(f, "  t=%s" % str(t_list))
            _mwrite_comment(f, "  overall=%s" % str(overall_data))
            for pj, ph in enumerate(phases):
                phase_data = results[v]['phase'][pj]
                _mwrite_comment(f, "  phase %d (%s / %s / %s): %s" %
                                 (pj + 1, ph['instance'], ph['section'], ph['material'], str(phase_data)))
            f.write("\n")
        # ===== [END CHANGED] =====

        # Missing Variables Warnings
        for v in missing_vars:
            _mwrite_comment(f, "WARNING: No such variable in ODB: %s" % v)
        f.write("\n\n")

        # ===== [CHANGED] Generalized "MATLAB Variable Assignments" report.
        # This used to be tensor-only (it skipped any variable whose
        # ncomp != NTENS). It now covers EVERY requested variable --
        # scalar, vector, or 6-component tensor -- through one generic
        # per-frame, per-region code path, keyed only on ncomp (never on
        # the variable's name string). Every quantity gets a fully
        # self-describing MATLAB assignment name (field symbol + region
        # + frame + time):
        #   scalar : PEEQ_overall_frame3_t1_0 = 1.234500e-02;
        #   vector : U_overall_frame3_t1_0 = [ ... ];   (column vector)
        #   tensor : S_overall_frame3_t1_0_full/_mandel/_voigt = [...];
        # [CHANGED for .m output] every descriptive/label line is now
        # written via _mwrite_comment (so it becomes a '%' comment), and
        # every line that assigns a value is now a clean, standalone
        # "name = ...;" statement with NO descriptive text mixed into the
        # same line -- required for this file to be valid, runnable
        # MATLAB. This reuses the SAME already-computed 'results' dict
        # built during the ODB pass further up in this function -- no
        # second ODB session is opened and nothing is re-read from disk.
        # [FIXED] This used to be wrapped in
        # "if fullTensor or mandelNotation or voigtNotation:", which meant
        # unchecking all three tensor-notation boxes in the GUI silently
        # skipped per-frame data for EVERY variable -- scalars and vectors
        # included -- not just tensors, since this loop is now the one and
        # only place per-frame named MATLAB variables get written. The loop
        # itself always runs now; fullTensor/mandelNotation/voigtNotation
        # only decide which tensor sub-notations (full/Mandel/Voigt) get
        # written for 6-component TENSOR quantities inside it.
        _mwrite_comment(f, "")
        _mwrite_comment(f, "===== MATLAB Variable Assignments =====")
        if fullTensor or mandelNotation or voigtNotation:
            chosen = []
            if fullTensor:
                chosen.append('Full tensor (matrix) for tensor quantities')
            if mandelNotation:
                chosen.append('Mandel (single column) for tensor quantities')
            if voigtNotation:
                chosen.append('Voigt (single column) for tensor quantities')
            _mwrite_comment(f, "Tensor notations included: %s" % ", ".join(chosen))
        else:
            _mwrite_comment(f, "No tensor notation selected -- tensor quantities will")
            _mwrite_comment(f, "list their names below but without a matrix; scalars")
            _mwrite_comment(f, "and vectors are unaffected and are written as normal.")
        _mwrite_comment(f, "Scalars are written as 'name = value;'.")
        _mwrite_comment(f, "Non-tensor vectors are written as 'name = [ ...column... ];'.")

        for v in requested_vars:
            if v in missing_vars or v in history_results:
                continue

            overall_data = results[v]['overall']
            scale = EPS_SCALE if is_strain_like(v) else SIG_SCALE

            _mwrite_comment(f, "")
            _mwrite_comment(f, "---- %s ----" % v)
            for fi in range(nframes):
                if overall_data[fi] is None:
                    # e.g. frame 0 often has no field output yet
                    continue

                val = overall_data[fi]
                ncomp = len(val) if isinstance(val, list) else 1
                t_here = results[v]['t'][fi]
                _mwrite_comment(f, "%s(frame %d, t=%s):" % (v, fi, str(t_here)))

                # Each quantity gets one fully self-describing MATLAB
                # variable name -- generic, built only from v/fi/
                # t_here/region, never a per-variable-name special case.
                overall_name = "%s_overall_frame%d_%s" % (v, fi, matlab_time_token(t_here))

                if ncomp == NTENS:
                    _mwrite_comment(f, "%s (tensor matrix):" % overall_name)
                    _write_tensor_block(f, val, scale, fullTensor, mandelNotation, voigtNotation, overall_name)
                elif ncomp > 1:
                    _mwrite_comment(f, "%s (vector, %d components)" % (overall_name, ncomp))
                    f.write("%s = ...\n" % overall_name)
                    f.write(format_column_vector(val))
                else:
                    sval = val[0] if isinstance(val, list) else val
                    f.write("%s = %.6e;\n" % (overall_name, sval))

                for pj, ph in enumerate(phases):
                    phase_val = results[v]['phase'][pj][fi]
                    if phase_val is None:
                        continue
                    p_ncomp = len(phase_val) if isinstance(phase_val, list) else 1
                    phase_name = "%s_phase%d_frame%d_%s" % (v, pj + 1, fi, matlab_time_token(t_here))

                    if p_ncomp == NTENS:
                        _mwrite_comment(f, "%s (%s / %s / %s):" %
                                         (phase_name, ph['instance'], ph['section'], ph['material']))
                        _write_tensor_block(f, phase_val, scale, fullTensor, mandelNotation, voigtNotation, phase_name)
                    elif p_ncomp > 1:
                        _mwrite_comment(f, "%s (vector, %d components, %s / %s / %s)" %
                                         (phase_name, p_ncomp, ph['instance'], ph['section'], ph['material']))
                        f.write("%s = ...\n" % phase_name)
                        f.write(format_column_vector(phase_val))
                    else:
                        sval = phase_val[0] if isinstance(phase_val, list) else phase_val
                        _mwrite_comment(f, "%s (%s / %s / %s)" %
                                         (phase_name, ph['instance'], ph['section'], ph['material']))
                        f.write("%s = %.6e;\n" % (phase_name, sval))

                f.write("\n")
        # ===== [END CHANGED / FIXED] =====

        # History and Surface defaults
        # [CHANGED] these were free-form Python-dict-shaped lines (not
        # guaranteed valid MATLAB, e.g. an unquoted region name) -- now
        # written as '%' comments so they never risk breaking the .m file,
        # while still being fully readable to a person opening the file.
        if history_results:
            for v, hdata in history_results.items():
                _mwrite_comment(f, "")
                _mwrite_comment(f, "[%s] History Data" % v)
                for hrName, data in hdata.items():
                    _mwrite_comment(f, "  region=%s, vals=%s" % (hrName, [d[1] for d in data]))

        if surface_results:
            for v, data in surface_results.items():
                _mwrite_comment(f, "  surface(%s)=%s" % (surfaceName, data))

        _mwrite_comment(f, "processing duration %s Seconds" % (time.time() - start))

    odb.close()
    print("Generalized extraction complete! Data saved to: " + outName)
# ------------------------------------------------------------------------
# Example calls:
#
# 1) Exactly like the GUI (positional v1..v10, up to 10
#    variables typed into the plugin's text boxes):
#
#       run_computation(odbName, stepName, outName, v1, v2, v3, v4, v5, v6, v7, v8, v9, v10)
#
# 2) From a standalone script, with an arbitrary-length variable list:
#
#       run_computation(
#           odbName='job.odb',
#           stepName='Step-1',
#           outName='generalized_output.txt',
#           requested_vars=['S', 'E', 'PEEQ', 'HFL', 'CTF1', 'ELSE',
#                            'EIGFREQ', 'SOF'],
#           surfaceName=None,   # e.g. 'ASSEMBLY_SURF-1' for surface output
#       )
# ------------------------------------------------------------------------


# ==============================================================================
# ADDITIONAL (new, standalone): ODB pre-process summary
#
# This function is purely additive - it does not modify, call, or depend on
# any of the existing code above. It is used by the GUI's "Pre-process"
# button to give a quick look at an ODB (available field output variable
# names, number of phases and their material names, number of steps, and
# number of frames per step) before the user picks variables to average.
# ==============================================================================
def get_odb_preprocess_info(odbName, infoOutPath):
    """
    Opens odbName read-only, gathers summary information, and writes it to
    the text file at infoOutPath so the GUI process can read it back and
    display it.
    """
    odb = session.openOdb(name=odbName, readOnly=True)
    try:
        # ---- phases (material regions) ----
        phases = []
        for inst in odb.rootAssembly.instances.values():
            for sa in inst.sectionAssignments:
                secName = getattr(sa, 'sectionName', 'UNKNOWN_SECTION')
                try:
                    matName = odb.sections[secName].material
                except Exception:
                    matName = 'UNKNOWN_MATERIAL'
                phases.append({'instance': inst.name, 'section': secName,
                                'material': matName})

        # ---- steps / frames ----
        stepNames = list(odb.steps.keys())
        stepFrameCounts = {}
        fieldVarNames = set()
        for sName in stepNames:
            st = odb.steps[sName]
            frms = st.frames
            nfr = len(frms)
            stepFrameCounts[sName] = nfr
            if nfr > 0:
                probeIdx = min(1, nfr - 1)
                try:
                    fieldVarNames.update(frms[probeIdx].fieldOutputs.keys())
                except Exception:
                    pass

        # ---- write summary ----
        with open(infoOutPath, 'w') as f:
            f.write("ODB Pre-process Summary\n")
            f.write("ODB file: %s\n\n" % odbName)

            f.write("Field output variables available (%d):\n" % len(fieldVarNames))
            for name in sorted(fieldVarNames):
                f.write("  - %s\n" % name)

            f.write("\nPhases / materials (%d):\n" % len(phases))
            for i, ph in enumerate(phases):
                f.write("  %d. instance=%s, section=%s, material=%s\n" %
                        (i + 1, ph['instance'], ph['section'], ph['material']))

            f.write("\nSteps (%d):\n" % len(stepNames))
            for sName in stepNames:
                f.write("  - %s : %d frames\n" % (sName, stepFrameCounts[sName]))
    finally:
        odb.close()


# ==============================================================================
# ADDITIONAL (new, standalone): Tensor notation report
#
# This section is purely additive. It does not modify, call in a mutating
# way, or otherwise depend on any state changes from run_computation() or
# any other function above -- it only *reuses* the already-defined helper
# functions (classify_variable, average_element_based_variable,
# to_mandel_kelvin, is_strain_like, ABAQUS_TO_MANDEL, SIG_SCALE, EPS_SCALE)
# to build a per-frame report of every requested 6-component TENSOR field
# output variable (e.g. S, E, ...), in up to three user-selectable
# notations (tick-marked in the GUI):
#
#   1) Full tensor notation   - the symmetric 3x3 matrix
#                                   [ s11 s12 s13 ]
#                                   [ s12 s22 s23 ]
#                                   [ s13 s23 s33 ]
#
#   2) Mandel/Kelvin notation - single column ("MATLAB style"), order
#                                   [11,22,33,23,13,12], off-diagonal terms
#                                   scaled by sqrt(2). This is exactly the
#                                   same Mandel/Kelvin vector that
#                                   run_computation() already reports.
#
#   3) Voigt notation         - single column, order
#                                   [11,22,33,12,13,23], NO sqrt(2) scaling
#                                   (this is Abaqus' native storage order).
#
# The whole-model (all-phases-combined) volume-weighted average is used,
# matching the "Overall Average" section of the main run_computation()
# output. The report is written per frame, formatted like:
#
#   S(frame 1, t=...):
#     Full tensor notation:
#       [ ... ]
#     Mandel notation (single column, order 11,22,33,23,13,12):
#       [...]
#     Voigt notation (single column, order 11,22,33,12,13,23):
#       [...]
# ==============================================================================

def mandel_to_voigt(mandel_vec, scale):
    """
    Convert a Mandel/Kelvin-ordered 6-vector back to Voigt (Abaqus native)
    order [11,22,33,12,13,23], undoing the reorder + sqrt(2) scaling that
    to_mandel_kelvin() applies. ABAQUS_TO_MANDEL is a self-inverse
    permutation, so the same index array undoes the reorder.
    """
    a = np.asarray(mandel_vec, dtype=float)
    idx = ABAQUS_TO_MANDEL
    return a[idx] / scale[idx]


# [ADDED] Turns a frame time value into a MATLAB-identifier-safe token
# (no '.', no '-', no spaces) so it can be embedded directly inside a
# generated MATLAB variable name, e.g. 1.0 -> "t1_0", -0.5 -> "tneg0_5".
def matlab_time_token(t):
    s = "%.6g" % float(t)
    s = s.replace('-', 'neg').replace('.', '_').replace('+', '')
    return "t" + s


def format_column_vector(vec):
    """
    Render a 6-component vector as a vertical single-column matrix
    (MATLAB-style column vector), e.g.:
        [ 2.617009e+07
          4.585359e+07
          2.430911e+07
          2.788220e+07
         -7.620473e-04
          2.690479e+07 ]
    Returns the formatted block as a single string, terminated with a
    newline, ready to be written directly to file.
    """
    v = np.asarray(vec, dtype=float)
    lines = ["    [ %.6e" % v[0]]
    for x in v[1:-1]:
        lines.append("      %.6e" % x)
    lines.append("      %.6e ];" % v[-1])
    return "\n".join(lines) + "\n"


def _write_tensor_block(f, mandel_vec, scale, fullTensor, mandelNotation, voigtNotation, base_name):
    """
    Shared writer for a single Mandel-ordered tensor vector, used for both
    the Overall Average section and each Phase section of the report so
    the two stay in an identical format.

    The Voigt-ordered vector is derived from mandel_vec ONCE here (not
    once per notation) and reused by both the "Full tensor" matrix and
    the "Voigt" column below, since they need the exact same numbers --
    just laid out differently on the page.

    [CHANGED] base_name is now REQUIRED (e.g. "S_overall_frame1_t1_0" or
    "S_phase1_frame1_t1_0", already carrying field symbol/region/frame/
    time -- see the caller). Each of the up-to-three matrices below
    (full tensor / Mandel column / Voigt column) is written as its own
    complete, self-named MATLAB assignment "<base_name>_full = [...];",
    "<base_name>_mandel = [...];", "<base_name>_voigt = [...];" -- so
    every printed matrix already has the exact variable name to give it
    when pasted into MATLAB, nothing generic/unnamed is left on the page.
    """
    # Compute the Voigt-ordered vector once, only if it's actually needed
    # by one or both of the notations below -- never derived twice.
    voigt_vec = mandel_to_voigt(mandel_vec, scale) if (fullTensor or voigtNotation) else None

    if fullTensor:
        t11, t22, t33, t12, t13, t23 = voigt_vec
        f.write("  %s_full = [   %% complete 3x3 matrix\n" % base_name)
        f.write("    %.6e   %.6e   %.6e\n" % (t11, t12, t13))
        f.write("    %.6e   %.6e   %.6e\n" % (t12, t22, t23))
        f.write("    %.6e   %.6e   %.6e ];\n" % (t13, t23, t33))

    if mandelNotation:
        f.write("  %s_mandel = ...   %% single column, order 11,22,33,23,13,12\n" % base_name)
        f.write(format_column_vector(mandel_vec))

    if voigtNotation:
        f.write("  %s_voigt = ...   %% single column, order 11,22,33,12,13,23\n" % base_name)
        f.write(format_column_vector(voigt_vec))
# ------------------------------------------------------------------------