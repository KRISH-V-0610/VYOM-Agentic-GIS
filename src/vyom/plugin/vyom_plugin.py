"""
VYOM QGIS plugin — entrypoint class.

QGIS instantiates this via ``classFactory(iface)`` (see ``__init__.py``) and calls
``initGui()`` on load and ``unload()`` on disable. The plugin adds a toolbar button +
menu action that toggles the VYOM dock widget, which talks to the FastAPI server
(``python -m vyom.api``) and loads agent-exported PNGs as QGIS raster layers.

The plugin holds no GIS logic itself — it is a thin shell around ``VyomDockWidget``.
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QAction

from .vyom_dockwidget import VyomDockWidget

PLUGIN_NAME = "VYOM — Agentic GIS"


class VyomPlugin:
    """Registered QGIS plugin object."""

    def __init__(self, iface):
        # iface is the QgisInterface handed in by QGIS at load time.
        self.iface = iface
        self.action = None
        self.dock = None

    def initGui(self):  # noqa: N802 (QGIS API name)
        self.action = QAction(PLUGIN_NAME, self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.setToolTip("Open the VYOM agentic-GIS panel")
        self.action.triggered.connect(self._toggle)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu(PLUGIN_NAME, self.action)

    def unload(self):
        if self.dock is not None:
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        if self.action is not None:
            self.iface.removePluginMenu(PLUGIN_NAME, self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None

    # ── internals ────────────────────────────────────────────────────────────────

    def _toggle(self, checked: bool):
        """Show/hide the dock, lazily creating it on first use."""
        if self.dock is None:
            self.dock = VyomDockWidget(self.iface, self.iface.mainWindow())
            self.dock.visibilityChanged.connect(self._on_visibility)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea if hasattr(Qt, 'DockWidgetArea') else Qt.RightDockWidgetArea, self.dock)
        self.dock.setVisible(checked)

    def _on_visibility(self, visible: bool):
        # Keep the toolbar toggle in sync when the user closes the dock directly.
        if self.action is not None:
            self.action.setChecked(visible)
