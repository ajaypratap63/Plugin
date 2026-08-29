from abaqusGui import *
from abaqusConstants import *
import os
import tempfile

# ===== [ADDED] Branding / identity constants =====
# Edit these three lines to update the credit line shown in the header
# and footer of the GUI -- nothing else in the file needs to change.
LAB_NAME = 'MicroSolidics Lab'
DEPARTMENT_NAME = 'Department of Mechanical Engineering'
SUPERVISOR_NAME = 'Prof. Tarkes Dora Pallicity'
TOOL_TITLE = 'MicroSolidics'
TOOL_SUBTITLE = 'ODB Homogenisation'
# ===== [END ADDED] =====

# ===== [ADDED] Simple colour palette (FOX/AFX colours are plain 0xBBGGRR
# ints, built here with FXRGB so every widget below shares one consistent
# scheme). Kept in one place so the whole look can be retinted later by
# editing only this block. =====
try:
    COLOR_HEADER_BG   = FXRGB(234, 240, 247)   # pale steel-blue header strip
    COLOR_FOOTER_BG   = FXRGB(234, 240, 247)
    COLOR_ACCENT_DARK = FXRGB(17, 45, 78)      # deep navy - titles
    COLOR_ACCENT_MED  = FXRGB(31, 90, 150)     # brand blue - primary buttons
    COLOR_ACCENT_TEXT = FXRGB(255, 255, 255)   # text on accent buttons
    COLOR_TEXT_MUTED  = FXRGB(95, 105, 115)    # subtitles / footer text
    _COLORS_READY = True
except Exception:
    _COLORS_READY = False
# ===== [END ADDED] =====

# ===== [ADDED] Small helper: build a font and never let a font failure
# break the GUI (font names/weights are not 100% portable across FOX
# builds, so every call is wrapped) =====
def _tryMakeFont(app, face, size, bold=False):
    try:
        if bold:
            try:
                return FXFont(app, face, size, FXFont.Bold)
            except Exception:
                return FXFont(app, face, size, 128)
        return FXFont(app, face, size)
    except Exception:
        return None
# ===== [END ADDED] =====

# ===== [ADDED] Reference text for the "Variable Handling" tip button =====
# Describes, for each Abaqus output-variable category, whether/how it is
# currently handled by data_extractor.py, and the averaging methodology
# (the maths) used for that category. Purely informational - does not
# affect computation in any way.
VARIABLE_HANDLING_TIP_TEXT = """VARIABLE CATEGORY HANDLING REFERENCE


  This plugin auto-detects how each requested output variable is
  stored in the ODB (its position and type) and applies the matching
  averaging strategy described below. You do not need to know these
  categories yourself -- just type the variable name(s) in the
  Variables field. This panel exists purely for reference and
  transparency.


>> SYMBOLS USED IN THE FORMULAS BELOW

  Every formula below is built from the same small set of symbols.
  They are defined once here rather than repeated in every category.

  value_i          One individual data value read from the ODB for
                    the requested variable -- e.g. one integration
                    point's stress, one element's total energy, one
                    node's displacement, or one contact node's
                    pressure, depending on the category. The
                    subscript i simply indexes "one such value" out
                    of however many the category collects.

  N                 The COUNT of value_i entries that get summed
                    together for a given phase (material region) at
                    a given frame. N only appears in the categories
                    that use an unweighted (equal-weighted) average,
                    i.e. every value counts the same regardless of
                    size or volume.

  IVOL_i            The integration-point volume that Abaqus reports
                    for the SAME integration point that value_i came
                    from. Used only as a weight for element
                    integration point variables.

  EVOL_i            The element volume that Abaqus reports for the
                    SAME element that value_i came from. Used as a
                    weight for centroidal variables and, differently,
                    as the denominator for whole-element
                    (density-style) variables.

  CNAREA_i          The contact-node area that Abaqus reports for the
                    SAME contact node that value_i came from. Used
                    only as a weight for surface variables.

  Sum(...)          Plain arithmetic addition of everything inside
                    the brackets, across every value_i (and, where
                    shown, its matching weight) found in the current
                    phase's region at the current frame.

  avg               The final averaged number this plugin writes to
                    the output file for that variable, that phase
                    (or "Overall"), at that frame.

  weighted_total,   Two running totals kept per phase so that
  weight_sum        combining phases into the single "Overall
                    Average" is done correctly: the totals and
                    weights from every phase are added together
                    FIRST, and only THEN divided -- rather than
                    averaging the phases' own averages together,
                    which would give a different (and wrong) answer
                    whenever the phases have different sizes.


>> CATEGORIES


  1) ELEMENT INTEGRATION POINT VARIABLES
     Examples : S, E, PE, PEEQ, and most SDVs.
     Position : INTEGRATION_POINT.
     Method   : A volume-weighted average, using each integration
                point's own volume (IVOL) as its weight:

                    avg = Sum(value_i * IVOL_i) / Sum(IVOL_i)

                This is computed separately for each material phase.
                The overall (whole-model) average is then formed by
                adding every phase's weighted_total and weight_sum
                together first, and dividing only once at the end.

  2) ELEMENT CENTROIDAL VARIABLES
     Position : CENTROID.
     Method   : The same volume-weighted average as category (1),
                but each value is weighted by its element's volume
                (EVOL) instead of an integration-point volume:

                    avg = Sum(value_i * EVOL_i) / Sum(EVOL_i)

  3) ELEMENT SECTION VARIABLES
     Examples : SF, SM, SE, STH (beam and shell section forces and
                moments).
     Position : INTEGRATION_POINT or CENTROID, depending on element
                type.
     Method   : Handled exactly like category (1) or (2) above,
                since Abaqus stores these at the same positions as
                ordinary integration-point or centroidal variables.
                No separate formula is needed.

  4) WHOLE ELEMENT VARIABLES
     Examples : ELEN, ELSE, ELPD, NFORC -- values Abaqus already
                reports as one total per element, not a density.
     Position : WHOLE_ELEMENT.
     Method   : Because each value_i is already a per-element total,
                it is NOT multiplied by volume again. Instead the
                plain sum of values is divided by the plain sum of
                element volumes, giving a density-style average:

                    weighted_total = Sum(value_i)
                    weight_sum     = Sum(EVOL_i)
                    avg            = weighted_total / weight_sum

                This matches how a quantity like element strain
                energy (ELSE) is conventionally reported: total
                energy per unit volume.

  5) ELEMENT FACE VARIABLES
     Examples : some contact and heat-transfer face outputs.
     Position : ELEMENT_FACE.
     Method   : An equal-weighted average -- a plain mean over every
                face value found -- because Abaqus does not expose a
                generic per-face area in the field output itself:

                    avg = Sum(value_i) / N

                Here, value_i is one element face's reported value,
                and N is simply how many such face values were found
                and summed (a count of faces, not an area).

  6) WHOLE ELEMENT ENERGY DENSITY VARIABLES
     Examples : SENER, CENER, DMENER.
     Position : WHOLE_ELEMENT.
     Method   : Identical to category (4) -- sum of values divided
                by sum of element volumes.

  7) WHOLE ELEMENT ERROR INDICATOR VARIABLES
     Examples : mesh error indicators.
     Position : WHOLE_ELEMENT.
     Method   : Identical to category (4) -- sum of values divided
                by sum of element volumes.

  8) NODAL VARIABLES
     Examples : U, V, A, RF, CF, NT.
     Position : NODAL or ELEMENT_NODAL.
     Method   : An equal-weighted average over node values:

                    avg = Sum(value_i) / N

                Here, value_i is one node's reported value and N is
                the number of nodes summed. Each node is mapped back
                to a material phase through that phase's own
                element-to-node connectivity, so a per-phase nodal
                average is produced as well as the overall one --
                generalizing what the original script only did for
                the U variable.

  9) MODAL VARIABLES
     Examples : EIGVAL, EIGFREQ, GM, DAMPRATIO.
     Source   : Pulled directly from
                step.historyRegions[...].historyOutputs[var].data.
                These are whole-model or whole-mode HISTORY outputs,
                not spatial field outputs, so no averaging formula
                applies at all -- no phase split and no volume
                weighting. The raw history time series is reported
                exactly as Abaqus recorded it.

  10) SURFACE VARIABLES
      Examples : CSTRESS, CPRESS, CDISP, HFL on a contact surface.
      Position : NODAL, but on a SURFACE region rather than a
                 material (element-set) region.
      Method   : A dedicated surface-averaging routine, weighted by
                 contact-node area (CNAREA) when Abaqus provides it:

                     avg = Sum(value_i * CNAREA_i) / Sum(CNAREA_i)

                 Here, value_i is one contact node's reported value.
                 This is NOT split by material phase, since a
                 contact surface is not a material region.

  11) CAVITY RADIATION VARIABLES
      Examples : radiation view-factor and cavity heat-flux outputs.
      Status   : Not yet given a dedicated strategy of its own. The
                 generic classifier still inspects how Abaqus
                 actually stores the variable at run time and routes
                 it through whichever category above matches its
                 reported position, so a request for one of these
                 will not crash or silently disappear -- but unlike
                 categories (1) through (10), this path has not been
                 specifically verified, so results for this category
                 should be spot-checked against a known value.

  12) SECTION VARIABLES
      Note     : This is a separate Abaqus category from "Element
                 section variables" in (3) -- it refers to
                 whole-model *section print style outputs, not
                 element-level SF/SM.
      Source   : Same treatment as category (9): pulled from history
                 output as a whole-model quantity, with no phase
                 split.

  13) WHOLE AND PARTIAL MODEL VARIABLES
      Examples : ALLIE, ALLKE, ALLSE, ETOTAL, and the other ALL**
                 energy totals.
      Source   : Same treatment as category (9): pulled from history
                 output, whole-model by nature.

  14) SOLUTION-DEPENDENT AMPLITUDE VARIABLES
      Examples : AMPCU.
      Source   : Same treatment as category (9): pulled from history
                 output.

  15) STRUCTURAL OPTIMIZATION VARIABLES
      Examples : MAT_PROP_NORMALIZED, DISP_OPT, THICKNESS,
                 DELTA_THICKNESS.
      Source   : Same treatment as category (9): pulled from history
                 output.


>> TENSOR-SPECIFIC NOTE

  This applies to any 6-component tensor variable, such as S, E, or
  IE, regardless of which category above it falls into.

  Before any of the averaging formulas above are applied, the
  tensor's raw Abaqus storage order [11,22,33,12,13,23] is converted
  to Mandel/Kelvin order [11,22,33,23,13,12], with its three
  off-diagonal terms scaled by sqrt(2).

  Averaging is a linear operation, and this reordering-and-scaling
  step is also linear, so the two operations commute: the averaged
  result is identical whether the conversion happens before or after
  averaging. The "Full tensor / Mandel / Voigt" tick-marks in this
  GUI simply let you view that one averaged result in three
  different, mathematically equivalent notations -- they do not
  change the underlying computation.
"""
# ===== [END ADDED] =====

# ===== [ADDED] Small embedded light-bulb icon (base64 PNG), so the tip
# button works as an icon with no external image file required =====
TIP_BULB_ICON_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAA/ElEQVR4nNWWQRLCIAxF"
    "G8fzsPFCHAMXrlzYY3AhN1wIFzoMND8hdFqrXXWA/PeTNgDlnKc9n9Ou6t8AnI3riIgP"
    "WspL3UVQ2o7pAGr1/GynLiaGBijqC2mIkXREwFtdkeYYKIU/slT36/3z8rjhKM7QflNo"
    "H0oriQJA1/7iXY8VM1DsDyVh7WRuWUpiJWBqjcMk1gMks5YkrBlwy8YkREBpUd1mma13"
    "jkYHtt9QG9eAsUaTTA2txIB15yguxmG76YLBMRucB5wBH13BdCanlKQp55we+//XlqNv"
    "FYURY+Tj3vtu+A+UaFKr1K+wcVcgohBCPTLPsyV29xK9AHYuhRLElgWRAAAAAElFTkSu"
    "QmCC"
)
# ===== [END ADDED] =====

# ===== [ADDED] Short tip content for the other three groups on this tab =====
ODB_TIP_TEXT = """OUTPUT DATABASE (ODB) SELECTION

  Pick the .odb file this run reads from. Click "Browse..." to
  locate it, or paste the full path directly into the field.

  Everything else on this tab needs a valid ODB path here first.
"""

PREPROCESS_TIP_TEXT = """PRE-PROCESS & INSPECTION

  Click "Pre-process" to quickly inspect the selected ODB before
  choosing variables. It lists the field output variables available,
  the material phases (instance / section / material), and the
  steps and frame counts found in the file.

  Optional, but useful for confirming variable and step names below.
"""

TARGETSTEP_TIP_TEXT = """TARGET STEP & OUTPUT FILE

  Step Name -- the Abaqus step to extract results from. Must match
  a step name in the ODB exactly (e.g. "Step-1").

  Output File Name -- the MATLAB script this run writes its averaged
  results to. Should be a .m file (e.g. "results.m"). MATLAB requires
  the filename to start with a letter and contain only letters,
  numbers, or underscores -- no hyphens or spaces (e.g. use
  "stress_strain_data.m", not "stress-strain_data.m"). Every quantity
  is written as a named, ready-to-use MATLAB variable -- just run or
  open the .m file in MATLAB to load them into the workspace.
"""
# ===== [END ADDED] =====


# ===== [ADDED] Scrollable read-only info dialog, reused by every tip
# button on this tab (title/text/size are parameterized so the same
# dialog serves the Variable Handling panel and the three short ones) =====
class VariableTipDialog(FXDialogBox):
    def __init__(self, owner, title=None, text=None, width=760, height=560):
        FXDialogBox.__init__(self, owner, title if title else 'Variable Handling Reference',
                              DECOR_TITLE | DECOR_BORDER | DECOR_RESIZE,
                              0, 0, width, height)
        mainFrame = FXVerticalFrame(self, LAYOUT_FILL_X | LAYOUT_FILL_Y, 0, 0, 0, 0, 10, 10, 10, 10)
        scrollWin = FXScrollWindow(mainFrame, LAYOUT_FILL_X | LAYOUT_FILL_Y)
        self.infoText = FXText(scrollWin, None, 0, LAYOUT_FILL_X | LAYOUT_FILL_Y | TEXT_READONLY | TEXT_WORDWRAP)
        self.infoText.setText(text if text else VARIABLE_HANDLING_TIP_TEXT)
        # [CHANGED] Same font as the rest of the GUI window (Helvetica),
        # instead of the previous monospace Courier New, per request.
        contentFont = _tryMakeFont(self.getApp(), 'Helvetica', 10)
        if contentFont:
            try:
                self.infoText.setFont(contentFont)
            except Exception:
                pass
        btnFrame = FXHorizontalFrame(mainFrame, LAYOUT_FILL_X, 0, 0, 0, 0, 0, 0, 8, 0)
        closeBtn = FXButton(btnFrame, 'Close', None, self, FXDialogBox.ID_ACCEPT,
                             LAYOUT_CENTER_X | FRAME_RAISED | FRAME_THICK, 0, 0, 0, 0, 24, 24, 5, 5)
        if _COLORS_READY:
            try:
                closeBtn.setBackColor(COLOR_ACCENT_MED)
                closeBtn.setTextColor(COLOR_ACCENT_TEXT)
            except Exception:
                pass
# ===== [END ADDED] =====


class MyGuiDialog(AFXDataDialog):
    # Create unique IDs for the button commands
    ID_COMPUTE = AFXDataDialog.ID_LAST + 1
    ID_BROWSE_ODB = AFXDataDialog.ID_LAST + 2 
    ID_PREPROCESS = AFXDataDialog.ID_LAST + 3
    ID_TIP_INFO = AFXDataDialog.ID_LAST + 4  # [ADDED]
    ID_TIP_INFO_ODB = AFXDataDialog.ID_LAST + 5          # [ADDED]
    ID_TIP_INFO_PREPROCESS = AFXDataDialog.ID_LAST + 6   # [ADDED]
    ID_TIP_INFO_TARGETSTEP = AFXDataDialog.ID_LAST + 7   # [ADDED]

    def __init__(self, form):
        # Create the dialog box with just a 'Dismiss' button at the bottom
        AFXDataDialog.__init__(self, form, '%s  |  %s' % (TOOL_TITLE, TOOL_SUBTITLE),
                                self.DISMISS, DIALOG_NORMAL, 0,0,0,0)

        # ===== [ADDED] Fonts used across the header/footer banners =====
        self.titleFont = _tryMakeFont(self.getApp(), 'Helvetica', 16, bold=True)
        self.subtitleFont = _tryMakeFont(self.getApp(), 'Helvetica', 10)
        self.creditFont = _tryMakeFont(self.getApp(), 'Helvetica', 9)
        self.footerFont = _tryMakeFont(self.getApp(), 'Helvetica', 8)
        # ===== [END ADDED] =====

        # ===== [ADDED] Header banner: logo, tool name, and lab/professor
        # credit line, laid out as logo (left) | title block (center) |
        # credit block (right), on a pale accent strip =====
        thisDir = os.path.dirname(os.path.abspath(__file__))
        logoPath = os.path.join(thisDir, 'microsolidics-logo.png')

        headerFrame = FXHorizontalFrame(self, LAYOUT_FILL_X, 0,0,0,0, 14,14,10,10)
        if _COLORS_READY:
            try:
                headerFrame.setBackColor(COLOR_HEADER_BG)
            except Exception:
                pass

        # -- Logo (left) --
        if os.path.isfile(logoPath):
            self.logoIcon = afxCreatePNGIcon(logoPath)
            maxWidth = 100
            if self.logoIcon.getWidth() > maxWidth:
                scaleFactor = float(maxWidth) / float(self.logoIcon.getWidth())
                newW = int(self.logoIcon.getWidth() * scaleFactor)
                newH = int(self.logoIcon.getHeight() * scaleFactor)
                self.logoIcon.scale(newW, newH)
            logoLabel = FXLabel(headerFrame, '', self.logoIcon, LAYOUT_LEFT|LAYOUT_CENTER_Y)
            if _COLORS_READY:
                try:
                    logoLabel.setBackColor(COLOR_HEADER_BG)
                except Exception:
                    pass

        # -- Title block (center, expands to fill) --
        titleBlock = FXVerticalFrame(headerFrame, LAYOUT_FILL_X|LAYOUT_CENTER_Y, 0,0,0,0, 10,0,0,0, 0,2)
        if _COLORS_READY:
            try:
                titleBlock.setBackColor(COLOR_HEADER_BG)
            except Exception:
                pass
        titleLabel = FXLabel(titleBlock, TOOL_TITLE.upper(), None, LAYOUT_LEFT)
        subtitleLabel = FXLabel(titleBlock, TOOL_SUBTITLE, None, LAYOUT_LEFT)
        if self.titleFont:
            titleLabel.setFont(self.titleFont)
        if self.subtitleFont:
            subtitleLabel.setFont(self.subtitleFont)
        if _COLORS_READY:
            try:
                titleLabel.setTextColor(COLOR_ACCENT_DARK)
                titleLabel.setBackColor(COLOR_HEADER_BG)
                subtitleLabel.setTextColor(COLOR_TEXT_MUTED)
                subtitleLabel.setBackColor(COLOR_HEADER_BG)
            except Exception:
                pass

        # -- Credit block (right): department, lab, and supervising
        # professor, right-aligned in the conventional "presented by" spot --
        creditBlock = FXVerticalFrame(headerFrame, LAYOUT_RIGHT|LAYOUT_CENTER_Y, 0,0,0,0, 0,0,0,0, 0,1)
        if _COLORS_READY:
            try:
                creditBlock.setBackColor(COLOR_HEADER_BG)
            except Exception:
                pass
        creditLines = [DEPARTMENT_NAME, LAB_NAME, 'Supervised by %s' % SUPERVISOR_NAME]
        for lineText in creditLines:
            lbl = FXLabel(creditBlock, lineText, None, LAYOUT_RIGHT)
            if self.creditFont:
                lbl.setFont(self.creditFont)
            if _COLORS_READY:
                try:
                    lbl.setTextColor(COLOR_ACCENT_DARK if lineText == creditLines[-1] else COLOR_TEXT_MUTED)
                    lbl.setBackColor(COLOR_HEADER_BG)
                except Exception:
                    pass

        FXHorizontalSeparator(self, SEPARATOR_GROOVE|LAYOUT_FILL_X)
        # ===== [END ADDED] =====

        # Tab Book (holds the tabs)
        tabBook = FXTabBook(self, None, 0, TABBOOK_NORMAL, 0,0,0,0, 0,0,0,0)

        # ================= Tab 1 =================
        tab1Item = FXTabItem(tabBook, 'Field average quantities ', None, TAB_TOP_NORMAL)
        
        # Using a 2-column horizontal frame layout to avoid scrolling
        tab1Frame = FXHorizontalFrame(tabBook, FRAME_SUNKEN|FRAME_THICK|LAYOUT_FILL_X|LAYOUT_FILL_Y, 0,0,0,0, 10,10,10,10)
        
        leftCol = FXVerticalFrame(tab1Frame, LAYOUT_FILL_X|LAYOUT_FILL_Y, 0,0,0,0, 0,5,0,0)
        rightCol = FXVerticalFrame(tab1Frame, LAYOUT_FILL_X|LAYOUT_FILL_Y, 0,0,0,0, 5,0,0,0)

        # ===== [ADDED] Load the shared bulb tip icon ONCE here, and define
        # two small reusable helpers:
        #   _addTipButtonInline -- tucks the tip button onto the END of an
        #       existing content row (e.g. next to a Browse button or a
        #       text field), so no extra row is added at all.
        #   _addTipButtonRow -- for groups with no pre-existing row to
        #       tuck into: a compact standalone row, docked top-right
        #       (the conventional spot for a help/info icon).
        # Reused below for all four groups on this tab. =====
        _tipIconReady = False
        try:
            import base64 as _b64
            tipIconTmpPath = os.path.join(tempfile.gettempdir(), 'my_gui_tip_bulb_icon.png')
            with open(tipIconTmpPath, 'wb') as _iconFile:
                _iconFile.write(_b64.b64decode(TIP_BULB_ICON_B64))
            self.tipIcon = afxCreatePNGIcon(tipIconTmpPath)
            # Shrink to half its native size for a compact, professional look
            halfW = max(1, int(self.tipIcon.getWidth() * 0.5))
            halfH = max(1, int(self.tipIcon.getHeight() * 0.5))
            self.tipIcon.scale(halfW, halfH)
            _tipIconReady = True
        except Exception:
            _tipIconReady = False

        def _addTipButtonInline(parentRow, cmdId, tooltip):
            if _tipIconReady:
                btn = FXButton(parentRow, '', self.tipIcon, self, cmdId,
                                LAYOUT_RIGHT|LAYOUT_CENTER_Y|FRAME_RAISED|FRAME_THICK,
                                0,0,0,0, 2,2,2,2)
            else:
                btn = FXButton(parentRow, '?', None, self, cmdId,
                                LAYOUT_RIGHT|LAYOUT_CENTER_Y|FRAME_RAISED|FRAME_THICK,
                                0,0,0,0, 5,5,2,2)
            try:
                btn.setTipText(tooltip)
            except Exception:
                pass
            return btn

        def _addTipButtonRow(parentGroup, cmdId, tooltip):
            row = FXHorizontalFrame(parentGroup, LAYOUT_FILL_X, 0,0,0,0, 0,0,0,2)
            if _tipIconReady:
                btn = FXButton(row, '', self.tipIcon, self, cmdId,
                                LAYOUT_RIGHT|LAYOUT_TOP|FRAME_RAISED|FRAME_THICK,
                                0,0,0,0, 2,2,2,2)
            else:
                btn = FXButton(row, '?', None, self, cmdId,
                                LAYOUT_RIGHT|LAYOUT_TOP|FRAME_RAISED|FRAME_THICK,
                                0,0,0,0, 5,5,2,2)
            try:
                btn.setTipText(tooltip)
            except Exception:
                pass
            return btn

        def _styleAccentButton(btn):
            # Paints a button with the brand accent colour so the key
            # actions (Browse / Pre-process / Compute) read as clear,
            # confident calls to action rather than default grey buttons.
            if not _COLORS_READY:
                return btn
            try:
                btn.setBackColor(COLOR_ACCENT_MED)
                btn.setTextColor(COLOR_ACCENT_TEXT)
            except Exception:
                pass
            return btn
        # ===== [END ADDED] =====

        # --- Professional Grouping 1: File Selection (LEFT COLUMN) ---
        group1 = FXGroupBox(leftCol, 'Output Database (ODB) Selection', FRAME_GROOVE|FRAME_THICK|LAYOUT_FILL_X, 0,0,0,0, 5,5,5,5)
        row1 = FXHorizontalFrame(group1, LAYOUT_FILL_X, 0,0,0,0, 0,0,0,0)
        self.odbPathField = AFXTextField(row1, 40, 'ODB File Path:', None, 0, LAYOUT_FILL_X|LAYOUT_CENTER_Y)
        browseBtn = FXButton(row1, 'Browse...', None, self, self.ID_BROWSE_ODB, LAYOUT_CENTER_Y|FRAME_RAISED|FRAME_THICK, 0,0,0,0, 10,10,2,2)
        browseBtn.setTipText('Choose an ODB file from disk')
        _styleAccentButton(browseBtn)  # [ADDED]
        _addTipButtonInline(row1, self.ID_TIP_INFO_ODB, 'About: Output Database (ODB) Selection')  # [ADDED]

        # --- Professional Grouping 2: Pre-process ODB (LEFT COLUMN) ---
        group3 = FXGroupBox(leftCol, 'Pre-process & Inspection', FRAME_GROOVE|FRAME_THICK|LAYOUT_FILL_X|LAYOUT_FILL_Y, 0,0,0,0, 5,5,5,5)
        _addTipButtonRow(group3, self.ID_TIP_INFO_PREPROCESS, 'About: Pre-process & Inspection')  # [ADDED]
        FXLabel(group3, 'Click Pre-process to inspect the ODB before choosing variables:')
        preprocessBtn = FXButton(group3, 'Pre-process', None, self, self.ID_PREPROCESS, LAYOUT_CENTER_X|FRAME_RAISED|FRAME_THICK, 0,0,0,0, 20,20,5,5)
        preprocessBtn.setTipText('Scan the ODB for available variables, phases, steps, and frames')
        _styleAccentButton(preprocessBtn)  # [ADDED]
        preprocessFrame = FXVerticalFrame(group3, FRAME_SUNKEN|FRAME_THICK|LAYOUT_FILL_X|LAYOUT_FILL_Y, 0,0,0,0, 2,2,2,2)
        self.preprocessText = FXText(preprocessFrame, None, 0, LAYOUT_FILL_X|LAYOUT_FIX_HEIGHT|TEXT_READONLY, 0,0,0,200)
        self.preprocessText.setText('(Pre-process results will appear here)')

        # --- Professional Grouping 3: Analysis Settings (RIGHT COLUMN) ---
        group2 = FXGroupBox(rightCol, 'Target Step & Output File', FRAME_GROOVE|FRAME_THICK|LAYOUT_FILL_X, 0,0,0,0, 5,5,5,5)
        _addTipButtonRow(group2, self.ID_TIP_INFO_TARGETSTEP, 'About: Target Step & Output File')  # [ADDED]
        row2 = FXHorizontalFrame(group2, LAYOUT_FILL_X, 0,0,0,0, 0,0,5,5)
        self.stepNameKw = AFXStringTarget('Step-1')
        self.stepNameField = AFXTextField(row2, 40, 'Step Name:', self.stepNameKw, 0, LAYOUT_FILL_X)

        row3 = FXHorizontalFrame(group2, LAYOUT_FILL_X, 0,0,0,0, 0,0,5,5)
        self.outNameKw = AFXStringTarget('stress_strain_data_IP.m')
        self.outNameField = AFXTextField(row3, 40, 'Output File Name:', self.outNameKw, 0, LAYOUT_FILL_X)

        # --- Professional Grouping 4: Variables & Computation (RIGHT COLUMN) ---
        group4 = FXGroupBox(rightCol, 'Extraction & Computation', FRAME_GROOVE|FRAME_THICK|LAYOUT_FILL_X|LAYOUT_FILL_Y, 0,0,0,0, 5,5,5,5)
        _addTipButtonRow(group4, self.ID_TIP_INFO, 'Which variable types are handled here, and how')  # [ADDED]
        FXLabel(group4, 'Enter field variables to average, separated by commas (e.g., S,E,PEEQ,SENER):')
        row4 = FXHorizontalFrame(group4, LAYOUT_FILL_X, 0,0,0,0, 0,0,5,5)
        self.varsKw = AFXStringTarget('S,E')
        self.varsField = AFXTextField(row4, 60, 'Variables:', self.varsKw, 0, LAYOUT_FILL_X)

        # ===== [ADDED] Tensor notation tick-mark options (before Compute button) =====
        FXLabel(group4, '\nSelect tensor notation(s) to report for 6-component tensor variables (e.g. S, E):')
        notationRow = FXHorizontalFrame(group4, LAYOUT_FILL_X, 0,0,0,0, 0,0,2,2)
        self.chkFullTensor = FXCheckButton(notationRow, 'Full tensor notation', None, 0, ICON_BEFORE_TEXT|LAYOUT_CENTER_Y)
        self.chkMandel = FXCheckButton(notationRow, 'Mandel notation (single column)', None, 0, ICON_BEFORE_TEXT|LAYOUT_CENTER_Y)
        self.chkVoigt = FXCheckButton(notationRow, 'Voigt notation (single column)', None, 0, ICON_BEFORE_TEXT|LAYOUT_CENTER_Y)
        self.chkFullTensor.setCheck(TRUE)
        self.chkMandel.setCheck(TRUE)
        self.chkVoigt.setCheck(TRUE)
        # ===== [END ADDED] =====

        FXLabel(group4, '\nClick the button below to extract data and find the average stress:')
        computeBtn = FXButton(group4, 'Compute', None, self, self.ID_COMPUTE, LAYOUT_CENTER_X|FRAME_RAISED|FRAME_THICK, 0,0,0,0, 30,30,8,8)
        computeBtn.setTipText('Run the extraction and phase-averaging, and write the results file')
        _styleAccentButton(computeBtn)  # [ADDED]
        if self.titleFont:
            try:
                computeBtn.setFont(self.titleFont)
            except Exception:
                pass

        # ================= Tab 2 =================
        tab2Item = FXTabItem(tabBook, 'Second moments', None, TAB_TOP_NORMAL)
        tab2Frame = FXVerticalFrame(tabBook, FRAME_SUNKEN|FRAME_THICK|LAYOUT_FILL_X|LAYOUT_FILL_Y, 0,0,0,0, 10,10,10,10)
        FXLabel(tab2Frame, 'This is the second tab. Add future features here.')

        # ================= Tab 3 =================
        tab3Item = FXTabItem(tabBook, 'Effective properties', None, TAB_TOP_NORMAL)
        tab3Frame = FXVerticalFrame(tabBook, FRAME_SUNKEN|FRAME_THICK|LAYOUT_FILL_X|LAYOUT_FILL_Y, 0,0,0,0, 10,10,10,10)
        FXLabel(tab3Frame, 'This is the third tab. Add future features here.')

        # ===== [ADDED] Footer credit strip =====
        FXHorizontalSeparator(self, SEPARATOR_GROOVE|LAYOUT_FILL_X)
        footerFrame = FXHorizontalFrame(self, LAYOUT_FILL_X, 0,0,0,0, 10,10,4,4)
        if _COLORS_READY:
            try:
                footerFrame.setBackColor(COLOR_FOOTER_BG)
            except Exception:
                pass
        footerText = '%s  |  %s  |  Supervised by %s' % (LAB_NAME, DEPARTMENT_NAME, SUPERVISOR_NAME)
        footerLabel = FXLabel(footerFrame, footerText, None, LAYOUT_CENTER_X)
        if self.footerFont:
            footerLabel.setFont(self.footerFont)
        if _COLORS_READY:
            try:
                footerLabel.setTextColor(COLOR_TEXT_MUTED)
                footerLabel.setBackColor(COLOR_FOOTER_BG)
            except Exception:
                pass
        # ===== [END ADDED] =====

    # ===== [ADDED] Function triggered when the "Tip" button is clicked =====
    def onTipInfo(self, sender, sel, ptr):
        dlg = VariableTipDialog(self)
        dlg.create()
        dlg.execute(PLACEMENT_OWNER)
        return 1

    def onTipInfoOdb(self, sender, sel, ptr):
        dlg = VariableTipDialog(self, 'Output Database (ODB) Selection', ODB_TIP_TEXT, 480, 250)
        dlg.create()
        dlg.execute(PLACEMENT_OWNER)
        return 1

    def onTipInfoPreprocess(self, sender, sel, ptr):
        dlg = VariableTipDialog(self, 'Pre-process & Inspection', PREPROCESS_TIP_TEXT, 480, 260)
        dlg.create()
        dlg.execute(PLACEMENT_OWNER)
        return 1

    def onTipInfoTargetStep(self, sender, sel, ptr):
        dlg = VariableTipDialog(self, 'Target Step & Output File', TARGETSTEP_TIP_TEXT, 480, 250)
        dlg.create()
        dlg.execute(PLACEMENT_OWNER)
        return 1
    # ===== [END ADDED] =====

    # Function triggered when "Browse..." is clicked
    def onBrowseOdb(self, sender, sel, ptr):
        try:
            import Tkinter as tk
            import tkFileDialog as filedialog
        except ImportError:
            import tkinter as tk
            from tkinter import filedialog

        root = tk.Tk()
        root.withdraw() 
        
        root.attributes('-topmost', True)

        fileName = filedialog.askopenfilename(
            title='Select ODB File',
            filetypes=[('Output Database', '*.odb'), ('All files', '*.*')]
        )
        root.destroy()

        if fileName:
            self.odbPathField.setText(str(fileName))

        return 1

    # Function triggered when the "Pre-process" button is clicked
    def onPreProcess(self, sender, sel, ptr):
        odb_path = self.odbPathField.getText()

        if not odb_path:
            FXMessageBox.warning(self, MBOX_OK, "Missing Input", "Please browse and select an ODB file first.")
            return 1

        infoPath = os.path.join(tempfile.gettempdir(), 'abaqus_preprocess_info.txt')

        command_string = (
            "import data_extractor\n"
            "try:\n"
            "    from importlib import reload\n"
            "except ImportError:\n"
            "    pass\n"
            "reload(data_extractor)\n"
            "data_extractor.get_odb_preprocess_info(r'{0}', r'{1}')"
        ).format(odb_path, infoPath)

        sendCommand(command_string)

        try:
            with open(infoPath, 'r') as f:
                infoText = f.read()
        except Exception as e:
            infoText = "Could not read pre-process info: %s" % str(e)

        self.preprocessText.setText(infoText)
        return 1

    # Function triggered when the "Compute" button is clicked
    def onCompute(self, sender, sel, ptr):
        odb_path = self.odbPathField.getText()
        step_name = self.stepNameField.getText()
        out_name = self.outNameField.getText()
        
        varsText = self.varsKw.getValue()
        varList = [v.strip() for v in varsText.split(',') if v.strip()]

        if not odb_path:
            FXMessageBox.warning(self, MBOX_OK, "Missing Input", "Please browse and select an ODB file first.")
            return 1

        # [ADDED] Read the tensor notation tick-marks here so they can be
        # passed straight into the single run_computation() call below --
        # no second sendCommand / no second ODB session.
        fullTensorFlag = bool(self.chkFullTensor.getCheck())
        mandelFlag = bool(self.chkMandel.getCheck())
        voigtFlag = bool(self.chkVoigt.getCheck())

        command_string = (
            "import data_extractor\n"
            "try:\n"
            "    from importlib import reload\n"
            "except ImportError:\n"
            "    pass\n"
            "reload(data_extractor)\n"
            "data_extractor.run_computation(r'{0}', r'{1}', r'{2}', requested_vars={3}, "
            "fullTensor={4}, mandelNotation={5}, voigtNotation={6})"
        ).format(odb_path, step_name, out_name, repr(varList),
                  fullTensorFlag, mandelFlag, voigtFlag)

        sendCommand(command_string)
        return 1

FXMAPFUNC(MyGuiDialog, SEL_COMMAND, MyGuiDialog.ID_COMPUTE, MyGuiDialog.onCompute)
FXMAPFUNC(MyGuiDialog, SEL_COMMAND, MyGuiDialog.ID_BROWSE_ODB, MyGuiDialog.onBrowseOdb)
FXMAPFUNC(MyGuiDialog, SEL_COMMAND, MyGuiDialog.ID_PREPROCESS, MyGuiDialog.onPreProcess)
FXMAPFUNC(MyGuiDialog, SEL_COMMAND, MyGuiDialog.ID_TIP_INFO, MyGuiDialog.onTipInfo)  # [ADDED]
FXMAPFUNC(MyGuiDialog, SEL_COMMAND, MyGuiDialog.ID_TIP_INFO_ODB, MyGuiDialog.onTipInfoOdb)  # [ADDED]
FXMAPFUNC(MyGuiDialog, SEL_COMMAND, MyGuiDialog.ID_TIP_INFO_PREPROCESS, MyGuiDialog.onTipInfoPreprocess)  # [ADDED]
FXMAPFUNC(MyGuiDialog, SEL_COMMAND, MyGuiDialog.ID_TIP_INFO_TARGETSTEP, MyGuiDialog.onTipInfoTargetStep)  # [ADDED]

class MyGuiForm(AFXForm):
    def __init__(self, owner):
        AFXForm.__init__(self, owner)

    def getFirstDialog(self):
        return MyGuiDialog(self)