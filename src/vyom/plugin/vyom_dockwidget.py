"""
VYOM QGIS dock widget — the plugin's interactive panel.

Layout:
  • Header: API base URL + Connect (always visible)
  • Tabs:
      Chat   — event picker, query templates, question box, conversation
               (markdown + tables + inline charts + layer explanations + a
               confidence badge), copy-answer.
      Map    — opacity slider for the last VYOM layer, NDWI flood-threshold
               slider + re-run, draw-AOI tool, clear-session.
      Scenes — a scene browser table (date / window / cloud / sensor) with
               double-click-to-zoom.
      Status — connection health, last-run summary, tool-call trace.
  • Status bar (always visible)

All network calls run on a background ``_Worker`` QThread so the QGIS main loop never
blocks. The worker only touches the Qt-free ``VyomApiClient``; results come back to the
GUI thread via signals, where layers are added to the QGIS canvas.

The Qt imports use ``qgis.PyQt`` so the plugin works under both PyQt5 (QGIS 3) and PyQt6
(QGIS 4). Enum forms differ between the two, so version-sensitive enums are resolved once
below with a try/except fallback. The Qt-free logic lives in ``api_client.py`` so it stays
unit-testable without a QGIS runtime.
"""

import html
import json
import os
import re

from qgis.PyQt.QtCore import QObject, QThread, QTimer, pyqtSignal, Qt, QUrl
from qgis.PyQt.QtGui import QColor, QImage, QTextCursor, QTextDocument
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDockWidget, QFileDialog, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSlider, QSpinBox,
    QTabWidget, QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)
from qgis.core import (
    QgsColorRampShader, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsFeature, QgsFillSymbol, QgsGeometry, QgsPointXY, QgsProject, QgsRasterLayer,
    QgsRasterShader, QgsRectangle, QgsSingleBandPseudoColorRenderer,
    QgsVectorLayer, QgsWkbTypes,
)
from qgis.gui import QgsMapTool, QgsRubberBand

from .api_client import (
    DEFAULT_BASE_URL, VyomApiClient, VyomApiError,
    best_preprocessed_layer, build_result_html, chart_filenames,
    confidence_badge, georef_layers, layer_explanation, query_templates,
)


# ── PyQt5/PyQt6 enum resolution (resolved once at import) ────────────────────────

try:
    _ORIENT_H = Qt.Orientation.Horizontal           # PyQt6
except AttributeError:
    _ORIENT_H = Qt.Horizontal                        # PyQt5

try:
    _POLY_GEOM = QgsWkbTypes.GeometryType.PolygonGeometry
except AttributeError:
    _POLY_GEOM = QgsWkbTypes.PolygonGeometry

try:
    _SELECT_ROWS = QAbstractItemView.SelectionBehavior.SelectRows
except AttributeError:
    _SELECT_ROWS = QAbstractItemView.SelectRows

try:
    _TEXT_SELECTABLE = Qt.TextInteractionFlag.TextSelectableByMouse
except AttributeError:
    _TEXT_SELECTABLE = Qt.TextSelectableByMouse

try:
    _RIGHT_DOCK = Qt.DockWidgetArea.RightDockWidgetArea
except AttributeError:
    _RIGHT_DOCK = Qt.RightDockWidgetArea  # not used here but kept for parity

try:
    _CURSOR_END = QTextCursor.MoveOperation.End
except AttributeError:
    _CURSOR_END = QTextCursor.End

# ── Chat bubble HTML helpers ────────────────────────────────────────────────────

_SEPARATOR = (
    '<table width="100%" cellpadding="0" cellspacing="0">'
    '<tr><td style="border-top:1px solid #e5e7eb;padding:2px;"></td></tr>'
    '</table>'
)

_TOOL_ICONS = {
    "list_events": "📋", "get_event_aoi": "🗺",
    "check_coverage": "🔍", "compare_windows": "📊",
    "list_scenes": "📷", "get_scene_metrics": "📈",
    "flood_extent": "🌊", "export_png": "🖼",
    "compute_change": "↔", "clip_to_aoi": "✂",
    "scenes_by_date_range": "📅",
}


def _user_bubble(text: str) -> str:
    """Blue right-aligned chat bubble for user messages."""
    return (
        '<table width="100%" cellpadding="0" cellspacing="4">'
        '<tr><td width="18%"> </td>'
        '<td bgcolor="#2563eb" style="padding:9px 13px;border-radius:12px 12px 4px 12px;">'
        '<font color="#bfdbfe"><b>You</b></font><br>'
        f'<font color="#ffffff">{text}</font>'
        '</td></tr></table>'
    )


def _vyom_bubble(badge_html: str, body_html: str) -> str:
    """Light-blue left-aligned bubble for VYOM responses."""
    return (
        '<table width="100%" cellpadding="0" cellspacing="4">'
        '<tr>'
        '<td bgcolor="#eff6ff" style="padding:9px 13px;border-radius:12px 12px 12px 4px;">'
        f'<b>{badge_html}</b><br>'
        f'<font color="#1e293b">{body_html}</font>'
        '</td>'
        '<td width="18%"> </td>'
        '</tr></table>'
    )


def _thinking_bubble(steps: list) -> str:
    """Loader bubble shown while the agent is working."""
    if steps:
        steps_html = "<br>".join(steps)
        body = (f'<font color="#94a3b8"><i>thinking…</i></font><br>{steps_html}')
    else:
        body = '<font color="#94a3b8"><i>thinking…  ⏳</i></font>'
    return (
        '<table width="100%" cellpadding="0" cellspacing="4">'
        '<tr>'
        '<td bgcolor="#f8fafc" style="padding:9px 13px;'
        'border-left:4px solid #2563eb;">'
        f'<b><font color="#1d4ed8">● VYOM</font></b> {body}'
        '</td>'
        '<td width="18%"> </td>'
        '</tr></table>'
    )


# ── Query input (Enter = send, Shift+Enter = newline) ───────────────────────────


class _QueryEdit(QPlainTextEdit):
    """QPlainTextEdit that emits ``submit`` on Enter; Shift+Enter inserts a newline."""
    submit = pyqtSignal()

    def keyPressEvent(self, event):  # noqa: N802
        try:
            ret = (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            shift = Qt.KeyboardModifier.ShiftModifier
        except AttributeError:
            ret = (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            shift = Qt.ShiftModifier
        if event.key() in ret and not (event.modifiers() & shift):
            self.submit.emit()
            return
        super().keyPressEvent(event)


# ── Streaming worker ────────────────────────────────────────────────────────────


class _StreamWorker(QObject):
    """Reads /query/stream SSE on a background thread; emits one signal per tool."""

    tool_event = pyqtSignal(dict)   # emitted for each completed tool call
    token_event = pyqtSignal(str)   # emitted for each streamed text token
    done_event = pyqtSignal(dict)   # emitted with the full result when stream ends
    failed = pyqtSignal(str)        # emitted on network/server error

    def __init__(self, client, question, max_steps, aoi_geojson):
        super().__init__()
        self._client = client
        self._question = question
        self._max_steps = max_steps
        self._aoi = aoi_geojson

    def run(self):
        try:
            def on_event(event):
                kind = event.get("type")
                if kind == "tool":
                    self.tool_event.emit(event)
                elif kind == "token":
                    self.token_event.emit(event.get("text", ""))

            result = self._client.query_stream(
                self._question, self._max_steps, self._aoi, on_event=on_event
            )
            self.done_event.emit(result)
        except VyomApiError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class _ChatBrowser(QTextBrowser):
    """Read-only chat pane that resolves inline chart images by registered name.

    ``setHtml`` clears a QTextDocument's resources, so instead of pre-registering
    images we override ``loadResource`` — the document calls it during layout for
    every ``<img src="name">``, and we map the name to a downloaded file path.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image_paths = {}  # logical name → local file path
        self.setOpenExternalLinks(True)

    def add_image(self, name: str, path: str):
        self._image_paths[name] = path

    def loadResource(self, resource_type, url):  # noqa: N802 (Qt override)
        path = self._image_paths.get(url.toString())
        if path:
            img = QImage(path)
            if not img.isNull():
                return img
        return super().loadResource(resource_type, url)


class _RectAoiTool(QgsMapTool):
    """Click-drag rectangle map tool. Emits the rectangle (in map CRS) on release."""

    def __init__(self, canvas, on_done):
        super().__init__(canvas)
        self._canvas = canvas
        self._on_done = on_done
        self._start = None
        self._band = QgsRubberBand(canvas, _POLY_GEOM)
        self._band.setColor(QColor(220, 30, 30, 70))
        self._band.setWidth(2)

    def canvasPressEvent(self, event):  # noqa: N802 (Qt override)
        self._start = self.toMapCoordinates(event.pos())

    def canvasMoveEvent(self, event):  # noqa: N802 (Qt override)
        if self._start is None:
            return
        self._draw(self._start, self.toMapCoordinates(event.pos()))

    def canvasReleaseEvent(self, event):  # noqa: N802 (Qt override)
        if self._start is None:
            return
        end = self.toMapCoordinates(event.pos())
        rect = QgsRectangle(self._start, end)
        self._start = None
        if not rect.isEmpty():
            self._on_done(rect)

    def reset(self):
        self._band.reset(_POLY_GEOM)

    def _draw(self, p1, p2):
        rect = QgsRectangle(p1, p2)
        self._band.reset(_POLY_GEOM)
        corners = [
            QgsPointXY(rect.xMinimum(), rect.yMinimum()),
            QgsPointXY(rect.xMaximum(), rect.yMinimum()),
            QgsPointXY(rect.xMaximum(), rect.yMaximum()),
            QgsPointXY(rect.xMinimum(), rect.yMaximum()),
        ]
        for pt in corners:
            self._band.addPoint(pt, False)
        self._band.addPoint(corners[0], True)


class _Worker(QObject):
    """Runs one blocking API method on a worker thread and signals the outcome."""

    done = pyqtSignal(object)   # emits the result (dict / str / path)
    failed = pyqtSignal(str)    # emits a human-readable error message

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self._fn, self._args, self._kwargs = fn, args, kwargs

    def run(self):
        try:
            self.done.emit(self._fn(*self._args, **self._kwargs))
        except VyomApiError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # never let a worker exception kill QGIS silently
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class VyomDockWidget(QDockWidget):
    """The dockable VYOM panel added to QGIS by the plugin."""

    def __init__(self, iface, parent=None):
        super().__init__("VYOM — Agentic GIS", parent)
        self.iface = iface
        self._threads = []               # keep refs so threads aren't GC'd mid-run
        self._events = []                # cached /events payload
        self._transcript_html = ""       # completed chat bubbles (user + VYOM)
        self._thinking_html = ""         # in-progress streaming bubble
        self._thinking_steps = []        # tool steps accumulated during streaming
        self._streaming_answer = ""      # tokens accumulated during answer streaming
        self._got_first_token = False    # True once the LLM starts outputting text
        self._last_answer_md = ""        # for "copy answer"
        self._last_vyom_layer = None     # for the opacity slider
        self._result_extent = None       # zoom union for a query's layers
        self._custom_aoi_geojson = None  # user-drawn AOI (JSON string)
        self._aoi_tool = None
        self._prev_map_tool = None
        # Debounce token renders — update UI at most every 60ms (≈16 fps)
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(60)
        self._render_timer.timeout.connect(self._render_all)
        self._build_ui()

    # ── UI construction ──────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QWidget()
        layout = QVBoxLayout(root)

        # Header — connection (always visible)
        conn = QGroupBox("VYOM API")
        conn_l = QHBoxLayout(conn)
        self.url_edit = QLineEdit(DEFAULT_BASE_URL)
        self.url_edit.setToolTip("Base URL of the running VYOM API (python -m vyom.api)")
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._on_connect)
        conn_l.addWidget(QLabel("URL:"))
        conn_l.addWidget(self.url_edit, 1)
        conn_l.addWidget(self.connect_btn)
        layout.addWidget(conn)

        # Tabs
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_chat_tab(), "Chat")
        self.tabs.addTab(self._build_map_tab(), "Map")
        self.tabs.addTab(self._build_scenes_tab(), "Scenes")
        self.tabs.addTab(self._build_status_tab(), "Status")
        layout.addWidget(self.tabs, 1)

        # Status bar (always visible)
        self.status = QLabel("Not connected.")
        self.status.setTextInteractionFlags(_TEXT_SELECTABLE)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.setWidget(root)

    def _build_chat_tab(self) -> QWidget:
        tab = QWidget()
        l = QVBoxLayout(tab)
        l.setSpacing(4)
        l.setContentsMargins(4, 4, 4, 4)

        # Top bar: event picker + AOI (compact single row)
        top_row = QHBoxLayout()
        top_row.setSpacing(4)
        self.event_combo = QComboBox()
        self.event_combo.setToolTip("Registered disaster events (loaded on Connect)")
        self.event_combo.currentIndexChanged.connect(self._on_event_changed)
        self.aoi_btn = QPushButton("AOI")
        self.aoi_btn.setToolTip("Add the event bounding box as a map layer")
        self.aoi_btn.setFixedWidth(44)
        self.aoi_btn.setEnabled(False)
        self.aoi_btn.clicked.connect(self._on_add_aoi)
        top_row.addWidget(self.event_combo, 1)
        top_row.addWidget(self.aoi_btn)
        l.addLayout(top_row)

        # ── Conversation fills all remaining space ──────────────────────────────
        self.answer_view = _ChatBrowser()
        self.answer_view.setReadOnly(True)
        l.addWidget(self.answer_view, 1)

        # ── Input area at the bottom (chat-style) ───────────────────────────────
        self.query_edit = _QueryEdit()
        self.query_edit.setPlaceholderText(
            "Ask anything… (Enter to send, Shift+Enter for newline)")
        self.query_edit.setFixedHeight(52)
        self.query_edit.submit.connect(self._on_ask)
        l.addWidget(self.query_edit)

        # Bottom row: templates | max-steps | Ask | Copy
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(4)
        self.template_combo = QComboBox()
        self.template_combo.setToolTip("Example questions")
        self.template_combo.addItem("— examples —", None)
        self.template_combo.currentIndexChanged.connect(self._on_template_picked)
        self.steps_spin = QSpinBox()
        self.steps_spin.setRange(1, 30)
        self.steps_spin.setValue(12)
        self.steps_spin.setFixedWidth(48)
        self.steps_spin.setToolTip("Max tool-call steps")
        self.ask_btn = QPushButton("Ask")
        self.ask_btn.setFixedWidth(52)
        self.ask_btn.clicked.connect(self._on_ask)
        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setFixedWidth(52)
        self.copy_btn.setEnabled(False)
        self.copy_btn.setToolTip("Copy last answer to clipboard")
        self.copy_btn.clicked.connect(self._on_copy_answer)
        self.report_btn = QPushButton("Report")
        self.report_btn.setFixedWidth(60)
        self.report_btn.setEnabled(False)
        self.report_btn.setToolTip("Download this chat session as a PDF report")
        self.report_btn.clicked.connect(self._on_download_report)
        bottom_row.addWidget(self.template_combo, 1)
        bottom_row.addWidget(self.steps_spin)
        bottom_row.addWidget(self.ask_btn)
        bottom_row.addWidget(self.copy_btn)
        bottom_row.addWidget(self.report_btn)
        l.addLayout(bottom_row)

        return tab

    def _build_map_tab(self) -> QWidget:
        tab = QWidget()
        l = QVBoxLayout(tab)

        # Opacity of last VYOM layer
        op = QGroupBox("Last VYOM layer — opacity")
        op_l = QHBoxLayout(op)
        self.opacity_slider = QSlider(_ORIENT_H)
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setValue(100)
        self.opacity_slider.valueChanged.connect(self._on_opacity_changed)
        self.opacity_label = QLabel("100%")
        op_l.addWidget(self.opacity_slider, 1)
        op_l.addWidget(self.opacity_label)
        l.addWidget(op)

        # Flood threshold
        thr = QGroupBox("Flood detection — NDWI threshold")
        thr_l = QVBoxLayout(thr)
        thr_row = QHBoxLayout()
        self.thr_slider = QSlider(_ORIENT_H)
        self.thr_slider.setRange(10, 60)   # 0.10 .. 0.60
        self.thr_slider.setValue(30)
        self.thr_slider.valueChanged.connect(self._on_threshold_changed)
        self.thr_label = QLabel("0.30")
        thr_row.addWidget(self.thr_slider, 1)
        thr_row.addWidget(self.thr_label)
        thr_l.addLayout(thr_row)
        self.rerun_btn = QPushButton("Re-run flood extent with this threshold")
        self.rerun_btn.setToolTip("Higher = stricter (less water). Lower = more water. "
                                  "McFeeters default is 0.30.")
        self.rerun_btn.clicked.connect(self._on_rerun_flood)
        thr_l.addWidget(self.rerun_btn)
        l.addWidget(thr)

        # Drawn AOI
        aoi = QGroupBox("Area of interest (override the event bbox)")
        aoi_l = QVBoxLayout(aoi)
        aoi_btns = QHBoxLayout()
        self.draw_aoi_btn = QPushButton("Draw AOI rectangle")
        self.draw_aoi_btn.setToolTip("Click-drag a rectangle on the map; the next "
                                     "question will analyse only that area.")
        self.draw_aoi_btn.clicked.connect(self._on_draw_aoi)
        self.clear_aoi_btn = QPushButton("Clear drawn AOI")
        self.clear_aoi_btn.clicked.connect(self._on_clear_aoi)
        aoi_btns.addWidget(self.draw_aoi_btn)
        aoi_btns.addWidget(self.clear_aoi_btn)
        aoi_l.addLayout(aoi_btns)
        self.aoi_status = QLabel("No custom AOI — queries use the event's full extent.")
        self.aoi_status.setWordWrap(True)
        aoi_l.addWidget(self.aoi_status)
        l.addWidget(aoi)

        self.clear_session_btn = QPushButton("Clear session (remove VYOM layers + chat)")
        self.clear_session_btn.clicked.connect(self._on_clear_session)
        l.addWidget(self.clear_session_btn)

        l.addStretch(1)
        return tab

    def _build_scenes_tab(self) -> QWidget:
        tab = QWidget()
        l = QVBoxLayout(tab)
        self.load_scenes_btn = QPushButton("Load scenes for selected event")
        self.load_scenes_btn.setEnabled(False)
        self.load_scenes_btn.clicked.connect(self._on_load_scenes)
        l.addWidget(self.load_scenes_btn)

        self.scene_table = QTableWidget(0, 4)
        self.scene_table.setHorizontalHeaderLabels(["Date", "Window", "Cloud %", "Sensor"])
        self.scene_table.setSelectionBehavior(_SELECT_ROWS)
        self.scene_table.setEditTriggers(QAbstractItemView.NoEditTriggers
                                         if hasattr(QAbstractItemView, "NoEditTriggers")
                                         else QAbstractItemView.EditTrigger.NoEditTriggers)
        self.scene_table.cellDoubleClicked.connect(self._on_scene_double_clicked)
        self.scene_table.setToolTip("Double-click a scene to zoom its footprint on the map.")
        l.addWidget(self.scene_table, 1)

        self.scene_hint = QLabel("Connect, pick an event, then Load scenes.")
        self.scene_hint.setWordWrap(True)
        l.addWidget(self.scene_hint)
        return tab

    def _build_status_tab(self) -> QWidget:
        tab = QWidget()
        l = QVBoxLayout(tab)
        l.addWidget(QLabel("Connection / health:"))
        self.health_label = QLabel("Not connected.")
        self.health_label.setWordWrap(True)
        self.health_label.setTextInteractionFlags(_TEXT_SELECTABLE)
        l.addWidget(self.health_label)

        l.addWidget(QLabel("Last run:"))
        self.run_label = QLabel("—")
        self.run_label.setWordWrap(True)
        l.addWidget(self.run_label)

        l.addWidget(QLabel("Tool-call trace:"))
        self.trace_view = QPlainTextEdit()
        self.trace_view.setReadOnly(True)
        l.addWidget(self.trace_view, 1)
        return tab

    # ── worker plumbing ──────────────────────────────────────────────────────────

    def _client(self) -> VyomApiClient:
        return VyomApiClient(self.url_edit.text().strip())

    def _run_async(self, fn, on_done, *args, busy_msg="Working…", on_error=None, **kwargs):
        """Run ``fn(*args)`` on a background thread; call ``on_done(result)`` on success."""
        self._set_busy(True, busy_msg)
        thread = QThread()
        worker = _Worker(fn, *args, **kwargs)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)

        def _cleanup():
            thread.quit()
            thread.wait()
            if thread in self._threads:
                self._threads.remove(thread)

        def _ok(result):
            self._set_busy(False)
            _cleanup()
            on_done(result)

        def _err(msg):
            self._set_busy(False)
            _cleanup()
            self._set_status(msg, error=True)
            if on_error:
                on_error(msg)

        worker.done.connect(_ok)
        worker.failed.connect(_err)
        thread._worker = worker  # keep ref alive
        self._threads.append(thread)
        thread.start()

    def _set_busy(self, busy: bool, msg: str = ""):
        for btn in (self.connect_btn, self.ask_btn, self.aoi_btn, self.rerun_btn,
                    self.load_scenes_btn):
            btn.setEnabled(not busy)
        if busy:
            self._set_status(msg)

    def _set_status(self, msg: str, error: bool = False):
        self.status.setText(msg)
        self.status.setStyleSheet("color: #c0392b;" if error else "color: palette(text);")

    # ── connection / events ───────────────────────────────────────────────────────

    # ── Connect-button color helpers ─────────────────────────────────────────────

    def _set_connect_state(self, state: str, detail: str = ""):
        """Update the Connect button appearance.

        state ∈ "idle" | "connecting" | "ok" | "degraded" | "failed"
        """
        styles = {
            "idle":       ("Connect",          ""),
            "connecting": ("Connecting…",      "QPushButton{color:#555;background:#ddd;}"),
            "ok":         ("● Connected",      "QPushButton{color:white;background:#27ae60;"
                                               "border-radius:3px;padding:2px 8px;}"),
            "degraded":   ("⚠ Degraded",       "QPushButton{color:white;background:#e67e22;"
                                               "border-radius:3px;padding:2px 8px;}"),
            "failed":     ("✗ Failed — retry", "QPushButton{color:white;background:#c0392b;"
                                               "border-radius:3px;padding:2px 8px;}"),
        }
        text, css = styles.get(state, styles["idle"])
        self.connect_btn.setText(text)
        self.connect_btn.setStyleSheet(css)
        if detail:
            self.connect_btn.setToolTip(detail)

    def _on_connect(self):
        # Reset to neutral while the request is in flight.
        self._set_connect_state("connecting")

        def done(health):
            db_ok = health.get("db_ok", False)
            key_ok = health.get("llm_key_configured", False)
            provider = health.get("llm_provider") or "llm"
            model = health.get("llm_model") or "?"
            db_label = "ok" if db_ok else "DOWN"
            key_label = "configured" if key_ok else "MISSING"

            if db_ok and key_ok:
                self._set_connect_state("ok",
                    f"DB: {db_label} | {provider} ({model}) | LLM key: {key_label}")
            else:
                issues = []
                if not db_ok:
                    issues.append("DB is DOWN")
                if not key_ok:
                    issues.append(f"{provider} key MISSING")
                self._set_connect_state("degraded", " | ".join(issues))

            self._set_status(
                f"status={health.get('status')}, DB={db_label}, {provider} key={key_label}.")
            self.health_label.setText(
                f"status: {health.get('status')}\nDB: {db_label}\n"
                f"LLM provider: {provider}\nLLM model: {model}\nLLM key: {key_label}")
            self._run_async(self._client().events, self._populate_events,
                            busy_msg="Loading events…")

        def failed(msg):
            self._set_connect_state("failed", f"Could not reach {self.url_edit.text()}: {msg}")

        self._run_async(self._client().health, done,
                        busy_msg="Connecting…", on_error=failed)

    def _populate_events(self, payload):
        self._events = payload.get("events") or []
        self.event_combo.clear()
        for ev in self._events:
            label = ev.get("display_name") or ev.get("key")
            hazard = ev.get("hazard_type")
            self.event_combo.addItem(
                f"{label}  [{hazard}]" if hazard else label, ev)
        self.aoi_btn.setEnabled(bool(self._events))
        self.load_scenes_btn.setEnabled(bool(self._events))
        self._set_status(f"Loaded {len(self._events)} events.")
        self._on_event_changed()

    def _on_event_changed(self, *_):
        """Repopulate the template dropdown for the newly selected event."""
        ev = self.event_combo.currentData()
        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        self.template_combo.addItem("— pick an example question —", None)
        if ev:
            name = ev.get("display_name") or ev.get("key")
            for t in query_templates(ev.get("hazard_type"), name):
                self.template_combo.addItem(t, t)
        self.template_combo.setCurrentIndex(0)
        self.template_combo.blockSignals(False)

    def _on_template_picked(self, index):
        if index <= 0:
            return
        text = self.template_combo.currentData()
        if text:
            self.query_edit.setPlainText(text)
        self.template_combo.setCurrentIndex(0)
        self.tabs.setCurrentIndex(0)

    def _on_add_aoi(self):
        ev = self.event_combo.currentData()
        if not ev:
            return
        bbox = ev.get("bbox")
        if not bbox or len(bbox) != 4:
            self._set_status(f"Event {ev.get('key')} has no bbox.", error=True)
            return
        self._add_aoi_layer(ev.get("key", "event"), bbox)

    def _add_aoi_layer(self, name: str, bbox: list):
        min_lon, min_lat, max_lon, max_lat = bbox
        layer = QgsVectorLayer("Polygon?crs=EPSG:4326", f"VYOM AOI — {name}", "memory")
        ring = [
            (min_lon, min_lat), (max_lon, min_lat), (max_lon, max_lat),
            (min_lon, max_lat), (min_lon, min_lat),
        ]
        feat = QgsFeature()
        feat.setGeometry(QgsGeometry.fromPolygonXY([[QgsPointXY(x, y) for x, y in ring]]))
        layer.dataProvider().addFeatures([feat])
        layer.updateExtents()
        # Transparent fill, red dashed outline — so it doesn't hide the basemap.
        symbol = QgsFillSymbol.createSimple({
            "style": "no",                  # no fill
            "outline_style": "dash",
            "outline_color": "220,30,30,255",
            "outline_width": "0.8",
        })
        layer.renderer().setSymbol(symbol)
        QgsProject.instance().addMapLayer(layer)
        # Transform WGS84 bbox → canvas CRS before zooming (canvas is typically EPSG:3857).
        # Passing raw degree values to setExtent() when the canvas is in metres zooms to
        # coordinates near 0°N 0°E (African coast) — the root cause of the blue-screen bug.
        rect = QgsRectangle(min_lon, min_lat, max_lon, max_lat)
        src = QgsCoordinateReferenceSystem("EPSG:4326")
        dst = self.iface.mapCanvas().mapSettings().destinationCrs()
        if dst.isValid() and dst != src:
            xform = QgsCoordinateTransform(src, dst, QgsProject.instance())
            rect = xform.transformBoundingBox(rect)
        self.iface.mapCanvas().setExtent(rect)
        self.iface.mapCanvas().refresh()
        self._set_status(f"Added AOI layer for {name}.")

    # ── ask / answer ──────────────────────────────────────────────────────────────

    def _on_ask(self):
        question = self.query_edit.toPlainText().strip()
        if not question:
            self._set_status("Enter a question first.", error=True)
            return
        self._ask(question)

    def _ask(self, question: str):
        self.trace_view.clear()
        self._result_extent = None
        self._thinking_steps = []
        self._streaming_answer = ""
        self._got_first_token = False

        # Show user bubble immediately — don't wait for the server.
        self._transcript_html += _user_bubble(html.escape(question))
        self._thinking_html = _thinking_bubble([])
        self._render_all()
        self.query_edit.clear()

        self._run_stream(question, self.steps_spin.value(), self._custom_aoi_geojson)

    def _run_stream(self, question: str, max_steps: int, aoi_geojson: str):
        """Launch a _StreamWorker on a background QThread."""
        self._set_busy(True, "Asking VYOM (streaming)…")
        client = self._client()
        thread = QThread()
        worker = _StreamWorker(client, question, max_steps, aoi_geojson)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)

        def _cleanup():
            thread.quit()
            thread.wait()
            if thread in self._threads:
                self._threads.remove(thread)

        def _on_tool(event):
            step_html = self._format_tool_step(event)
            self._thinking_steps.append(step_html)
            if not self._got_first_token:
                self._thinking_html = _thinking_bubble(self._thinking_steps)
                self._render_all()

        def _on_token(token: str):
            self._streaming_answer += token
            if not self._got_first_token:
                self._got_first_token = True
            # Show growing answer; debounced via QTimer
            self._thinking_html = self._make_streaming_html()
            if not self._render_timer.isActive():
                self._render_timer.start()

        def _on_done(result):
            self._render_timer.stop()
            self._set_busy(False)
            _cleanup()
            self._thinking_html = ""
            self._streaming_answer = ""
            self._got_first_token = False
            self._on_stream_done(result)

        def _on_err(msg):
            self._render_timer.stop()
            self._set_busy(False)
            _cleanup()
            self._thinking_html = ""
            self._streaming_answer = ""
            self._got_first_token = False
            self._render_all()
            self._set_status(msg, error=True)

        worker.tool_event.connect(_on_tool)
        worker.token_event.connect(_on_token)
        worker.done_event.connect(_on_done)
        worker.failed.connect(_on_err)
        thread._worker = worker
        self._threads.append(thread)
        thread.start()

    @staticmethod
    def _format_tool_step(event: dict) -> str:
        """Format one tool event as a short HTML line for the thinking bubble."""
        name = event.get("name", "?")
        icon = _TOOL_ICONS.get(name, "⚙")
        step = event.get("step", "?")
        result = event.get("result") or {}
        if isinstance(result, dict):
            if "error" in result:
                summary = (
                    f'<font color="#ef4444">⚠ '
                    f'{html.escape(str(result["error"])[:60])}</font>')
            elif name == "check_coverage":
                has_d = result.get("has_data")
                cnt = result.get("scene_count", "?")
                summary = f'has_data={has_d}, {cnt} scenes'
            elif name == "compare_windows":
                metric = result.get("metric", "metric")
                wnames = list((result.get("windows") or {}).keys())[:3]
                summary = f'{html.escape(metric)}, windows: {", ".join(wnames)}'
            elif name == "flood_extent":
                pct = result.get("water_area_pct")
                km2 = result.get("estimated_water_area_km2")
                summary = (f'{pct:.2f}% water, {km2:.1f} km²'
                           if pct is not None and km2 is not None else "computed")
            elif name == "list_events":
                summary = f'{result.get("count", "?")} events'
            elif name == "list_scenes":
                summary = f'{len(result.get("scenes") or [])} scenes'
            else:
                keys = [k for k in list(result.keys())[:4]]
                summary = "{" + ", ".join(html.escape(k) for k in keys) + "}"
        else:
            summary = html.escape(str(result)[:60])
        return (f'<font color="#64748b">{icon} #{step} '
                f'<b>{html.escape(name)}</b></font>'
                f'<font color="#475569"> → {summary}</font>')

    def _make_streaming_html(self) -> str:
        """Render the in-progress VYOM bubble showing typed tokens so far."""
        safe = html.escape(self._streaming_answer)
        # Blinking cursor character shown at the end while typing
        body = f'{safe}<font color="#2563eb">▊</font>'
        return _vyom_bubble(
            '● <font color="#1e40af">VYOM</font>'
            ' <font color="#94a3b8">typing…</font>',
            body,
        )

    # ── chat transcript rendering ──────────────────────────────────────────────

    def _render_all(self):
        """Rebuild the chat view from completed transcript + current thinking bubble."""
        self.answer_view.setHtml(self._transcript_html + self._thinking_html)
        cursor = self.answer_view.textCursor()
        cursor.movePosition(_CURSOR_END)
        self.answer_view.setTextCursor(cursor)
        self.answer_view.ensureCursorVisible()

    def _append_html(self, fragment: str):
        """Append a completed HTML fragment (e.g. layer explanation) to transcript."""
        self._transcript_html += fragment
        self._render_all()

    @staticmethod
    @staticmethod
    def _strip_latex(text: str) -> str:
        """Convert LaTeX math notation to readable plain text.

        All regex patterns are built at call-time from chr() + re.escape() so
        the Edit tool cannot corrupt them by doubling literal backslashes.
        """
        # chr codes — no backslash literals touch the Edit tool
        _BS  = chr(92)   # backslash
        _DLR = chr(36)   # dollar sign
        _OBR = chr(91)   # [
        _CBR = chr(93)   # ]
        _OPR = chr(40)   # (
        _CPR = chr(41)   # )
        _NL  = chr(10)   # newline

        # \text{X} / \mathbf{X} / \mathrm{X} → X
        text = re.sub(
            re.escape(_BS) + r'(?:text|mathbf|mathrm|mathit|boldsymbol)' +
            re.escape('{') + r'([^{}]*)' + re.escape('}'),
            lambda m: m.group(1), text)

        # \frac{A}{B} → (A / B)  — 3 passes for nesting
        _frac = (re.escape(_BS + 'frac') +
                 re.escape('{') + r'([^{}]*)' + re.escape('}') +
                 re.escape('{') + r'([^{}]*)' + re.escape('}'))
        for _ in range(3):
            text = re.sub(_frac, lambda m: f'({m.group(1)}) / ({m.group(2)})', text)

        # \[ ... \] display math
        _lb = re.escape(_BS + _OBR)
        _rb = re.escape(_BS + _CBR)
        text = re.sub(_lb + r'(.*?)' + _rb,
                      lambda m: f'`{m.group(1).strip()}`', text, flags=re.DOTALL)

        # \( ... \) inline math
        _lp = re.escape(_BS + _OPR)
        _rp = re.escape(_BS + _CPR)
        text = re.sub(_lp + r'(.*?)' + _rp,
                      lambda m: f'`{m.group(1).strip()}`', text, flags=re.DOTALL)

        # [ formula ] on its own line (common Sarvam pattern)
        text = re.sub(
            r'(?m)^[ ]*' + re.escape(_OBR) + r'(.*?)' + re.escape(_CBR) + r'[ ]*$',
            lambda m: f'`{m.group(1).strip()}`', text)

        # $$ ... $$ and $ ... $
        _D = re.escape(_DLR)
        text = re.sub(_D + _D + r'(.*?)' + _D + _D,
                      lambda m: f'`{m.group(1).strip()}`', text, flags=re.DOTALL)
        text = re.sub(_D + r'([^' + _DLR + _NL + r']+?)' + _D,
                      lambda m: f'`{m.group(1)}`', text)

        # Simple symbol replacements via str.replace — no regex, no escaping issues
        _SYM = [
            (_BS + 'times',  chr(215)), (_BS + 'cdot',   chr(183)),
            (_BS + 'approx', chr(8776)), (_BS + 'geq',   chr(8805)),
            (_BS + 'leq',    chr(8804)), (_BS + 'neq',   chr(8800)),
            (_BS + 'pm',     chr(177)),  (_BS + 'infty',  chr(8734)),
            (_BS + 'sigma',  chr(963)),  (_BS + 'alpha',  chr(945)),
            (_BS + 'beta',   chr(946)),  (_BS + 'gamma',  chr(947)),
            (_BS + 'delta',  chr(948)),  (_BS + 'mu',     chr(956)),
            (_BS + 'lambda', chr(955)),  (_BS + 'theta',  chr(952)),
            (_BS + 'pi',     chr(960)),  (_BS + 'sqrt',   'sqrt'),
            (_BS + 'left(',  '('),       (_BS + 'right)', ')'),
            (_BS + 'left[',  '['),       (_BS + 'right]', ']'),
        ]
        for src, dst in _SYM:
            text = text.replace(src, dst)

        # Strip any remaining \command tokens
        text = re.sub(re.escape(_BS) + r'[a-zA-Z]+', '', text)

        return text

    def _markdown_body(self, md: str) -> str:
        md = self._strip_latex(md)
        doc = QTextDocument()
        if hasattr(doc, "setMarkdown"):
            doc.setMarkdown(md)
            full = doc.toHtml()
            m = re.search(r"<body[^>]*>(.*)</body>", full, re.DOTALL)
            return m.group(1) if m else full
        return "<pre>%s</pre>" % html.escape(md)

    def _on_stream_done(self, result: dict):
        """Handle the completed result — build VYOM bubble + load layers."""
        answer_md = result.get("answer") or "_(no answer returned)_"
        self._last_answer_md = answer_md
        self.copy_btn.setEnabled(True)
        self.report_btn.setEnabled(True)

        badge = confidence_badge(result)   # None for conversational replies
        if badge:
            level, color, reason = badge
            badge_html = (
                f'<font color="{color}">●</font> <font color="#1e40af">VYOM</font> '
                f'<font color="#94a3b8">({level} confidence)</font>'
            )
            confidence_note = (
                f'<p><i><font color="{color}">{html.escape(reason)}</font></i></p>')
        else:
            level = "n/a"
            badge_html = '<font color="#1e40af">● VYOM</font>'
            confidence_note = ""

        body_parts = [self._markdown_body(answer_md), confidence_note]

        tables = build_result_html(result)
        if tables:
            body_parts.append("<br>" + tables)

        charts = chart_filenames(result)

        # Insert a loading-spinner placeholder for chart area.
        # A unique token lets us swap it for real <img> tags in one setHtml call
        # instead of N re-renders (one per chart).
        _charts_token = f"<!--vyom-charts-{id(result)}-->"
        if charts:
            n = len(charts)
            _chart_placeholder = (
                _charts_token +
                f'<div style="margin:6px 0;padding:10px 12px;background:#eff6ff;'
                f'border-left:3px solid #3b82f6;border-radius:4px;'
                f'font-size:12px;color:#1e40af;">'
                f'&#9203; Loading {n} chart{"s" if n > 1 else ""}…</div>'
            )
            body_parts.append("<br>" + _chart_placeholder)
        else:
            _chart_placeholder = ""

        self._transcript_html += _vyom_bubble(badge_html, "".join(body_parts))
        self._transcript_html += _SEPARATOR
        self._render_all()

        self._render_trace(result)
        conf_str = f"{level} confidence" if badge else "no data tools"
        self.run_label.setText(
            f"{result.get('steps')} steps · stopped={result.get('stopped')} · "
            f"coverage_checked={result.get('coverage_checked')} · {conf_str}")
        self._set_status(f"Done — {result.get('steps')} steps.")

        # Download ALL charts in one batch thread → single setHtml swap when ready.
        if charts:
            _placeholder_ref = "<br>" + _chart_placeholder

            def _batch_download_charts():
                client = self._client()
                paths = {}
                for fname in charts:
                    try:
                        paths[fname] = client.download_export(fname)
                    except Exception:
                        pass
                return paths

            def _on_all_charts_ready(paths: dict):
                if not paths:
                    return
                for name, path in paths.items():
                    self.answer_view.add_image(name, path)
                imgs = "".join(
                    f'<br><img src="{html.escape(n)}" width="320">'
                    for n in charts
                    if n in paths
                )
                self._transcript_html = self._transcript_html.replace(
                    _placeholder_ref, imgs, 1)
                self._render_all()
                self._scroll_to_end()

            n_charts = len(charts)
            self._run_async(
                _batch_download_charts, _on_all_charts_ready,
                busy_msg=f"Loading {n_charts} chart{'s' if n_charts > 1 else ''}…")

        # Download georeferenced GeoTIFFs → styled, positioned raster layers + explainer.
        analysis_layers = georef_layers(result)
        for layer in analysis_layers:
            self._run_async(
                self._client().download_export,
                lambda path, lyr=layer: self._add_georef_raster(
                    path, lyr.get("kind"), lyr.get("bounds_wgs84"),
                    lyr.get("layer_name")),
                layer["filename"], busy_msg=f"Downloading {layer['filename']}…")

        # Fallback: if no raster tool ran (catalog-only query like compare_windows),
        # stream the primary-index COG for the resolved scene via vsicurl so the
        # map is never empty for analysis queries.
        if not analysis_layers and result.get("intent") in ("analyze", "discover"):
            base_url = self.url_edit.text().strip() or DEFAULT_BASE_URL

            def _add_preprocessed():
                desc = best_preprocessed_layer(result, base_url)
                return desc   # pass to done-callback on GUI thread

            def _on_preprocessed(desc):
                if not desc:
                    return
                vsicurl_path = desc.get("vsicurl_path")
                if vsicurl_path:
                    self._add_vsicurl_layer(vsicurl_path, desc.get("kind", "index"),
                                            desc.get("bounds_wgs84"),
                                            desc.get("layer_name", "Preprocessed COG"))

            self._run_async(_add_preprocessed, _on_preprocessed,
                            busy_msg="Resolving map layer…")

    def _scroll_to_end(self):
        """Scroll the chat browser to the bottom after adding new content."""
        sb = self.answer_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_chart_downloaded(self, path: str):
        name = os.path.basename(path)
        self.answer_view.add_image(name, path)
        self._render_all()
        self._scroll_to_end()

    def _on_copy_answer(self):
        if self._last_answer_md:
            QApplication.clipboard().setText(self._last_answer_md)
            self._set_status("Last answer copied to clipboard.")

    # ── PDF session report ────────────────────────────────────────────────────────

    def _report_header_html(self) -> str:
        """Title block prepended to the exported PDF."""
        import datetime
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        try:
            url = self.url_edit.text().strip()
        except Exception:
            url = ""
        try:
            event = self.event_combo.currentText().strip()
        except Exception:
            event = ""
        meta = "Generated: %s" % ts
        if url:
            meta += " &nbsp;|&nbsp; Server: %s" % html.escape(url)
        if event:
            meta += " &nbsp;|&nbsp; Event: %s" % html.escape(event)
        return (
            '<div style="border-bottom:2px solid #1d4ed8;padding-bottom:6px;'
            'margin-bottom:12px;">'
            '<h2 style="color:#1d4ed8;margin:0;">VYOM — Agentic GIS</h2>'
            '<div style="color:#475569;font-size:13px;">Chat Session Report</div>'
            '<div style="color:#64748b;font-size:11px;margin-top:3px;">%s</div>'
            '</div>'
        ) % meta

    def _capture_map_png(self) -> str:
        """Save the current map canvas to a temp PNG; return path or '' on failure."""
        import tempfile
        try:
            canvas = self.iface.mapCanvas()
            out = os.path.join(tempfile.gettempdir(), "vyom_map_%d.png" % id(self))
            canvas.saveAsImage(out)
            return out if os.path.exists(out) else ""
        except Exception:
            return ""

    def _on_download_report(self):
        """Export the full chat session (queries + responses + charts + map) to PDF."""
        if not self._transcript_html:
            self._set_status("Nothing to export yet — ask a question first.", error=True)
            return

        import datetime
        from qgis.PyQt.QtPrintSupport import QPrinter

        default_name = "VYOM_session_%s.pdf" % datetime.datetime.now().strftime(
            "%Y%m%d_%H%M")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save VYOM session report", default_name, "PDF files (*.pdf)")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"

        try:
            doc = QTextDocument()

            # Resource-type enum is scoped differently across Qt builds; the
            # integer value of ImageResource is 2 in every Qt version.
            try:
                img_res = QTextDocument.ResourceType.ImageResource
            except AttributeError:
                img_res = getattr(QTextDocument, "ImageResource", 2)

            # Register the inline chart images so <img src="name"> resolves in print.
            for name, img_path in self.answer_view._image_paths.items():
                img = QImage(img_path)
                if not img.isNull():
                    doc.addResource(img_res, QUrl(name), img)

            # Capture + register the current map view.
            map_section = ""
            map_png = self._capture_map_png()
            if map_png:
                mimg = QImage(map_png)
                if not mimg.isNull():
                    doc.addResource(img_res, QUrl("__vyom_map__"), mimg)
                    map_section = (
                        '<br><div style="border-top:1px solid #e5e7eb;'
                        'margin-top:10px;padding-top:8px;">'
                        '<h3 style="color:#1d4ed8;margin:0 0 6px 0;">Map view</h3>'
                        '<img src="__vyom_map__" width="680"></div>'
                    )

            doc.setHtml(self._report_header_html() + self._transcript_html + map_section)

            # QPrinter enums are also scoped differently across Qt5/Qt6 builds.
            def _qenum(scope, attr, fallback):
                try:
                    return getattr(getattr(QPrinter, scope), attr)
                except AttributeError:
                    return getattr(QPrinter, attr, fallback)

            mode = _qenum("PrinterMode", "HighResolution", 2)
            pdf_fmt = _qenum("OutputFormat", "PdfFormat", 1)

            printer = QPrinter(mode)
            printer.setOutputFormat(pdf_fmt)
            printer.setOutputFileName(path)
            try:
                from qgis.PyQt.QtGui import QPageSize
                printer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
            except Exception:
                try:
                    printer.setPageSize(QPrinter.A4)
                except Exception:
                    pass
            # Qt6/PyQt6 renamed QTextDocument.print_() to print().
            _print = getattr(doc, "print_", None) or getattr(doc, "print")
            _print(printer)
            self._set_status("Report saved: %s" % path)
        except Exception as exc:
            self._set_status("Report export failed: %s: %s"
                             % (type(exc).__name__, exc), error=True)

    def _render_trace(self, result: dict):
        lines = []
        for call in result.get("tool_calls") or []:
            heavy = "  [HEAVY]" if call.get("heavy") else ""
            lines.append(f"#{call.get('step')} {call.get('name')}{heavy}")
            args = call.get("args") or {}
            if args:
                lines.append(f"    args: {json.dumps(args)[:300]}")
            summary = self._summarise_result(call.get("result"))
            if summary:
                lines.append(f"    → {summary}")
        self.trace_view.setPlainText("\n".join(lines) or "(no tool calls)")

    @staticmethod
    def _summarise_result(res) -> str:
        if isinstance(res, dict):
            if "error" in res:
                return f"error: {res['error']}"
            return "{%s}" % ", ".join(list(res.keys())[:8])
        return str(res)[:200]

    # ── raster layer styling ────────────────────────────────────────────────────

    def _add_georef_raster(self, path: str, kind: str, bounds_wgs84,
                           layer_name: str = None):
        name = layer_name or os.path.splitext(os.path.basename(path))[0]
        layer = QgsRasterLayer(path, f"VYOM — {name}")
        if not layer.isValid():
            self._set_status(f"Could not load raster {name}.", error=True)
            return
        self._style_raster(layer, kind)
        QgsProject.instance().addMapLayer(layer)
        self._last_vyom_layer = layer
        self.opacity_slider.blockSignals(True)
        self.opacity_slider.setValue(100)
        self.opacity_slider.blockSignals(False)
        self.opacity_label.setText("100%")
        self._zoom_to_bounds(bounds_wgs84)
        self._set_status(f"Added {kind} layer: {name}")
        # Explain the layer in the chat. Use an explicit dark text colour so the bubble
        # stays readable under QGIS's dark theme (default text colour is white there).
        explain = layer_explanation(kind)
        if explain:
            title, body = explain
            self._append_html(
                f'<table width="100%" cellpadding="6" style="background:#eef5ff;">'
                f'<tr><td style="color:#0b2545;">'
                f'<b style="color:#13315c;">{html.escape(title)}</b><br>'
                f'<span style="color:#1b3a5b;">{body}</span>'
                f'</td></tr></table>')

    def _add_vsicurl_layer(self, vsicurl_path: str, kind: str, bounds_wgs84,
                           layer_name: str = "Preprocessed COG"):
        """Add a COG via GDAL vsicurl — streams range-requests, no full download.

        This is the fallback path for catalog-only queries (compare_windows etc.)
        where no analysis GeoTIFF was produced.  The COG already lives on the local
        server so the vsicurl head-request is ~50ms; QGIS loads overview tiles only.
        """
        try:
            from osgeo import gdal
            gdal.SetConfigOption("GDAL_HTTP_PROXY", "")           # bypass corp proxy
            gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
            gdal.SetConfigOption("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif")
        except ImportError:
            pass

        layer = QgsRasterLayer(vsicurl_path, f"VYOM — {layer_name}")
        if not layer.isValid():
            # vsicurl failed (e.g. proxy); skip silently — catalog answer is complete.
            return
        self._style_raster(layer, kind)
        QgsProject.instance().addMapLayer(layer)
        self._last_vyom_layer = layer
        self.opacity_slider.blockSignals(True)
        self.opacity_slider.setValue(85)
        self.opacity_slider.blockSignals(False)
        self.opacity_label.setText("85%")
        self._zoom_to_bounds(bounds_wgs84)
        self._set_status(f"Added preprocessed layer: {layer_name}")

    def _style_raster(self, layer, kind: str):
        if kind == "rgb":
            return  # default multiband RGB renderer is correct for false_color
        if kind == "flood":
            try:
                shader = QgsRasterShader()
                ramp = QgsColorRampShader()
                ramp.setColorRampType(QgsColorRampShader.Discrete)
                ramp.setColorRampItemList([
                    QgsColorRampShader.ColorRampItem(0, QColor(0, 0, 0, 0), "dry"),
                    QgsColorRampShader.ColorRampItem(1, QColor(0, 90, 200, 255), "water"),
                ])
                shader.setRasterShaderFunction(ramp)
                layer.setRenderer(QgsSingleBandPseudoColorRenderer(
                    layer.dataProvider(), 1, shader))
            except Exception:
                pass
            return

        if kind == "burn":
            # 5 USGS dNBR classes (0 unburned … 4 high) — matches _DNBR_COLORMAP.
            try:
                shader = QgsRasterShader()
                ramp = QgsColorRampShader()
                ramp.setColorRampType(QgsColorRampShader.Discrete)
                ramp.setColorRampItemList([
                    QgsColorRampShader.ColorRampItem(0, QColor(0, 0, 0, 0), "unburned"),
                    QgsColorRampShader.ColorRampItem(1, QColor(255, 255, 178, 255), "low"),
                    QgsColorRampShader.ColorRampItem(2, QColor(254, 204, 92, 255), "moderate-low"),
                    QgsColorRampShader.ColorRampItem(3, QColor(253, 141, 60, 255), "moderate-high"),
                    QgsColorRampShader.ColorRampItem(4, QColor(227, 26, 28, 255), "high"),
                ])
                shader.setRasterShaderFunction(ramp)
                layer.setRenderer(QgsSingleBandPseudoColorRenderer(
                    layer.dataProvider(), 1, shader))
            except Exception:
                pass
            return

        if kind == "change":
            stops = [(-1.0, QColor(178, 24, 43)), (0.0, QColor(247, 247, 247)),
                     (1.0, QColor(33, 102, 172))]
        else:  # index
            stops = [(-1.0, QColor(165, 0, 38)), (0.0, QColor(255, 255, 191)),
                     (1.0, QColor(49, 54, 149))]
        try:
            shader = QgsRasterShader()
            ramp = QgsColorRampShader()
            ramp.setColorRampType(QgsColorRampShader.Interpolated)
            ramp.setColorRampItemList([
                QgsColorRampShader.ColorRampItem(v, c, f"{v:g}") for v, c in stops])
            shader.setRasterShaderFunction(ramp)
            renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
            renderer.setClassificationMin(-1.0)
            renderer.setClassificationMax(1.0)
            layer.setRenderer(renderer)
        except Exception:
            pass

    def _zoom_to_bounds(self, bounds_wgs84):
        if not bounds_wgs84 or len(bounds_wgs84) != 4:
            return
        try:
            minlon, minlat, maxlon, maxlat = bounds_wgs84
            rect = QgsRectangle(minlon, minlat, maxlon, maxlat)
            src = QgsCoordinateReferenceSystem("EPSG:4326")
            project = QgsProject.instance()
            dst = self.iface.mapCanvas().mapSettings().destinationCrs()
            if dst.isValid() and dst != src:
                xform = QgsCoordinateTransform(src, dst, project)
                rect = xform.transformBoundingBox(rect)
            if self._result_extent is None:
                self._result_extent = QgsRectangle(rect)
            else:
                self._result_extent.combineExtentWith(rect)
            self.iface.mapCanvas().setExtent(self._result_extent)
            self.iface.mapCanvas().refresh()
        except Exception as exc:
            self._set_status(f"Layer added but zoom failed: {exc}", error=True)

    # ── Map tab handlers ──────────────────────────────────────────────────────────

    def _on_opacity_changed(self, value):
        self.opacity_label.setText(f"{value}%")
        if self._last_vyom_layer is not None:
            try:
                self._last_vyom_layer.setOpacity(value / 100.0)
                self._last_vyom_layer.triggerRepaint()
            except Exception:
                pass

    def _on_threshold_changed(self, value):
        self.thr_label.setText(f"{value / 100.0:.2f}")

    def _on_rerun_flood(self):
        ev = self.event_combo.currentData()
        if not ev:
            self._set_status("Pick an event first (Chat tab).", error=True)
            return
        thr = self.thr_slider.value() / 100.0
        name = ev.get("display_name") or ev.get("key")
        q = (f"Calculate the flood extent for the {name} event-window scene using an "
             f"NDWI water threshold of {thr:.2f}, and report the inundated area in km².")
        self.tabs.setCurrentIndex(0)
        self._ask(q)

    def _on_draw_aoi(self):
        canvas = self.iface.mapCanvas()
        self._prev_map_tool = canvas.mapTool()
        self._aoi_tool = _RectAoiTool(canvas, self._on_aoi_drawn)
        canvas.setMapTool(self._aoi_tool)
        self._set_status("Draw a rectangle on the map (click-drag).")

    def _on_aoi_drawn(self, rect_mapcrs):
        try:
            geojson, bbox = self._rect_to_wgs84(rect_mapcrs)
            self._custom_aoi_geojson = geojson
            self.aoi_status.setText(
                "Custom AOI set: [{:.3f}, {:.3f}, {:.3f}, {:.3f}]. "
                "The next question will analyse only this area.".format(*bbox))
            self._add_aoi_layer("drawn", bbox)
            self._set_status("Custom AOI captured.")
        except Exception as exc:
            self._set_status(f"Could not build AOI: {exc}", error=True)
        finally:
            canvas = self.iface.mapCanvas()
            if self._prev_map_tool is not None:
                canvas.setMapTool(self._prev_map_tool)
            if self._aoi_tool is not None:
                self._aoi_tool.reset()

    def _rect_to_wgs84(self, rect):
        dst = self.iface.mapCanvas().mapSettings().destinationCrs()
        wgs = QgsCoordinateReferenceSystem("EPSG:4326")
        if dst.isValid() and dst != wgs:
            xform = QgsCoordinateTransform(dst, wgs, QgsProject.instance())
            rect = xform.transformBoundingBox(rect)
        minlon, minlat = rect.xMinimum(), rect.yMinimum()
        maxlon, maxlat = rect.xMaximum(), rect.yMaximum()
        geo = {
            "type": "Polygon",
            "coordinates": [[
                [minlon, minlat], [maxlon, minlat], [maxlon, maxlat],
                [minlon, maxlat], [minlon, minlat],
            ]],
        }
        return json.dumps(geo), [minlon, minlat, maxlon, maxlat]

    def _on_clear_aoi(self):
        self._custom_aoi_geojson = None
        self.aoi_status.setText(
            "No custom AOI — queries use the event's full extent.")
        if self._aoi_tool is not None:
            self._aoi_tool.reset()
        self._set_status("Custom AOI cleared.")

    def _on_clear_session(self):
        project = QgsProject.instance()
        for layer in list(project.mapLayers().values()):
            if layer.name().startswith("VYOM"):
                project.removeMapLayer(layer.id())
        self.iface.mapCanvas().refresh()
        self._transcript_html = ""
        self._thinking_html = ""
        self._thinking_steps = []
        self._streaming_answer = ""
        self._got_first_token = False
        self.answer_view.setHtml("")
        self.trace_view.clear()
        self._last_vyom_layer = None
        self._last_answer_md = ""
        self.copy_btn.setEnabled(False)
        self.report_btn.setEnabled(False)
        self._result_extent = None
        self.run_label.setText("—")
        self._set_status("Session cleared.")

    # ── Scenes tab ────────────────────────────────────────────────────────────────

    def _on_load_scenes(self):
        ev = self.event_combo.currentData()
        if not ev:
            self._set_status("Pick an event first.", error=True)
            return
        self._run_async(
            self._client().scenes, self._populate_scenes,
            ev.get("key"), None, None, None, 300,
            busy_msg="Loading scenes…")

    def _populate_scenes(self, payload):
        scenes = payload.get("scenes") or []
        self.scene_table.setRowCount(0)
        for sc in scenes:
            row = self.scene_table.rowCount()
            self.scene_table.insertRow(row)
            acq = (sc.get("acq_datetime") or "")[:10]
            cloud = sc.get("cloud_cover")
            cloud_s = f"{cloud:.1f}" if isinstance(cloud, (int, float)) else "—"
            cells = [acq, sc.get("window_type") or "", cloud_s, sc.get("sensor") or ""]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                if col == 0:
                    item.setData(Qt.UserRole if not hasattr(Qt, "ItemDataRole")
                                 else Qt.ItemDataRole.UserRole, sc.get("id"))
                self.scene_table.setItem(row, col, item)
        self.scene_table.resizeColumnsToContents()
        self.scene_hint.setText(
            f"{len(scenes)} scenes. Double-click a row to zoom its footprint.")
        self._set_status(f"Loaded {len(scenes)} scenes.")

    def _on_scene_double_clicked(self, row, _col):
        item = self.scene_table.item(row, 0)
        if not item:
            return
        role = Qt.UserRole if not hasattr(Qt, "ItemDataRole") else Qt.ItemDataRole.UserRole
        scene_id = item.data(role)
        if not scene_id:
            return
        self._run_async(
            self._client()._get_json, self._zoom_to_scene,
            f"/scenes/{scene_id}/assets",
            busy_msg=f"Locating {scene_id}…")

    def _zoom_to_scene(self, assets):
        bbox = assets.get("bbox_wgs84")
        if not bbox or len(bbox) != 4:
            self._set_status("Scene has no footprint to zoom to.", error=True)
            return
        self._add_aoi_layer(f"scene {assets.get('scene_id', '')[:12]}", bbox)
        self._set_status(f"Zoomed to scene {assets.get('scene_id')}.")

    # ── lifecycle ─────────────────────────────────────────────────────────────────

    def closeEvent(self, event):  # noqa: N802 (Qt override)
        for thread in list(self._threads):
            thread.quit()
            thread.wait()
        super().closeEvent(event)
