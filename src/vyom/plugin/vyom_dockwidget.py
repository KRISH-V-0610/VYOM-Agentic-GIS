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

from qgis.PyQt.QtCore import QObject, QThread, pyqtSignal, Qt, QUrl
from qgis.PyQt.QtGui import QColor, QImage, QTextCursor, QTextDocument
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDockWidget, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSlider, QSpinBox, QTabWidget,
    QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
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
    build_result_html, chart_filenames, confidence_badge, georef_layers,
    layer_explanation, query_templates,
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
        self._threads = []           # keep refs so threads aren't GC'd mid-run
        self._events = []            # cached /events payload
        self._transcript_html = ""   # accumulated chat-bubble HTML
        self._last_answer_md = ""    # for "copy answer"
        self._last_vyom_layer = None # for the opacity slider
        self._result_extent = None   # zoom union for a query's layers
        self._custom_aoi_geojson = None  # user-drawn AOI (JSON string)
        self._aoi_tool = None
        self._prev_map_tool = None
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

        # Event picker + add AOI
        ev = QGroupBox("Event")
        ev_l = QHBoxLayout(ev)
        self.event_combo = QComboBox()
        self.event_combo.setToolTip("Registered disaster events (loaded on Connect)")
        self.event_combo.currentIndexChanged.connect(self._on_event_changed)
        self.aoi_btn = QPushButton("Add AOI layer")
        self.aoi_btn.setEnabled(False)
        self.aoi_btn.clicked.connect(self._on_add_aoi)
        ev_l.addWidget(self.event_combo, 1)
        ev_l.addWidget(self.aoi_btn)
        l.addWidget(ev)

        # Templates
        tmpl_row = QHBoxLayout()
        tmpl_row.addWidget(QLabel("Templates:"))
        self.template_combo = QComboBox()
        self.template_combo.addItem("— pick an example question —", None)
        self.template_combo.currentIndexChanged.connect(self._on_template_picked)
        tmpl_row.addWidget(self.template_combo, 1)
        l.addLayout(tmpl_row)

        # Query box
        self.query_edit = QPlainTextEdit()
        self.query_edit.setPlaceholderText(
            "e.g. How did water area change in the Kerala 2018 floods?")
        self.query_edit.setFixedHeight(60)
        l.addWidget(self.query_edit)

        steps_row = QHBoxLayout()
        steps_row.addWidget(QLabel("Max steps:"))
        self.steps_spin = QSpinBox()
        self.steps_spin.setRange(1, 30)
        self.steps_spin.setValue(12)
        steps_row.addWidget(self.steps_spin)
        steps_row.addStretch(1)
        self.ask_btn = QPushButton("Ask")
        self.ask_btn.clicked.connect(self._on_ask)
        steps_row.addWidget(self.ask_btn)
        l.addLayout(steps_row)

        # Conversation
        l.addWidget(QLabel("Conversation:"))
        self.answer_view = _ChatBrowser()
        self.answer_view.setReadOnly(True)
        l.addWidget(self.answer_view, 1)

        self.copy_btn = QPushButton("Copy last answer")
        self.copy_btn.setEnabled(False)
        self.copy_btn.clicked.connect(self._on_copy_answer)
        l.addWidget(self.copy_btn)

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
        self._append_html(f"<p><b>You:</b> {html.escape(question)}</p>")
        self.query_edit.clear()
        aoi = self._custom_aoi_geojson
        self._run_async(
            self._client().query, self._on_answer,
            question, self.steps_spin.value(), False, aoi,
            busy_msg="Asking VYOM (this can take a while)…",
        )

    # ── chat transcript rendering ──────────────────────────────────────────────

    def _append_html(self, fragment: str):
        self._transcript_html += fragment
        self.answer_view.setHtml(self._transcript_html)
        self._scroll_to_end()

    def _scroll_to_end(self):
        cursor = self.answer_view.textCursor()
        cursor.movePosition(_CURSOR_END)
        self.answer_view.setTextCursor(cursor)
        self.answer_view.ensureCursorVisible()

    @staticmethod
    def _markdown_body(md: str) -> str:
        doc = QTextDocument()
        if hasattr(doc, "setMarkdown"):
            doc.setMarkdown(md)
            full = doc.toHtml()
            m = re.search(r"<body[^>]*>(.*)</body>", full, re.DOTALL)
            return m.group(1) if m else full
        return "<pre>%s</pre>" % html.escape(md)

    def _on_answer(self, result: dict):
        answer_md = result.get("answer") or "_(no answer returned)_"
        self._last_answer_md = answer_md
        self.copy_btn.setEnabled(True)

        level, color, reason = confidence_badge(result)
        header = (f'<p><span style="color:{color};">●</span> <b>VYOM</b> '
                  f'<i>({level} confidence)</i></p>')
        bubble = [header, self._markdown_body(answer_md),
                  f'<p><i style="color:{color};">{html.escape(reason)}</i></p>']

        tables = build_result_html(result)
        if tables:
            bubble.append("<br>" + tables)

        charts = chart_filenames(result)
        for fname in charts:
            bubble.append(f'<br><img src="{html.escape(fname)}">')

        bubble.append("<hr>")
        self._append_html("".join(bubble))

        self._render_trace(result)
        self.run_label.setText(
            f"{result.get('steps')} steps · stopped={result.get('stopped')} · "
            f"coverage_checked={result.get('coverage_checked')} · confidence={level}")
        self._set_status(
            f"Done — {result.get('steps')} steps, {level} confidence.")

        # Download charts → inline images.
        for fname in charts:
            self._run_async(
                self._client().download_export, self._on_chart_downloaded,
                fname, busy_msg=f"Downloading chart {fname}…")

        # Download georeferenced GeoTIFFs → styled, positioned raster layers + explainer.
        for layer in georef_layers(result):
            self._run_async(
                self._client().download_export,
                lambda path, lyr=layer: self._add_georef_raster(
                    path, lyr.get("kind"), lyr.get("bounds_wgs84")),
                layer["filename"], busy_msg=f"Downloading {layer['filename']}…")

    def _on_chart_downloaded(self, path: str):
        name = os.path.basename(path)
        self.answer_view.add_image(name, path)
        self.answer_view.setHtml(self._transcript_html)
        self._scroll_to_end()

    def _on_copy_answer(self):
        if self._last_answer_md:
            QApplication.clipboard().setText(self._last_answer_md)
            self._set_status("Last answer copied to clipboard.")

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

    def _add_georef_raster(self, path: str, kind: str, bounds_wgs84):
        name = os.path.basename(path)
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
        self.answer_view.setHtml("")
        self.trace_view.clear()
        self._last_vyom_layer = None
        self._last_answer_md = ""
        self.copy_btn.setEnabled(False)
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
