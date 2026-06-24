"""
VYOM QGIS plugin package.

QGIS discovers plugins by calling ``classFactory(iface)`` on the plugin package's
``__init__``. We keep the QGIS-dependent imports *inside* the factory so this package
still imports cleanly outside QGIS (e.g. when unit-testing ``api_client.py`` via
``import vyom.plugin.api_client``), which has no Qt/qgis dependency.
"""


def classFactory(iface):  # noqa: N802 (QGIS API name)
    """Entrypoint QGIS calls to instantiate the plugin.

    Args:
        iface: the QgisInterface instance handed in by the QGIS plugin manager.
    """
    from .vyom_plugin import VyomPlugin
    return VyomPlugin(iface)
