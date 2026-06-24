"""
Shared pytest configuration for the VYOM test suite.

src/ is added to pythonpath via pyproject.toml [tool.pytest.ini_options].
Tests that require a live PostgreSQL/PostGIS database are marked `integration`
and can be skipped with:  pytest -m "not integration"
"""
def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: tests that require a live PostgreSQL/PostGIS database",
    )
