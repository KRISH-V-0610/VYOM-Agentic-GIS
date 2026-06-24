"""
Database writer for the ARD pipeline: scene registration + metric caching.

This is the pipeline's DB access layer (it may use rasterio-adjacent data but does
NOT import rasterio itself). The Phase-3 PostGIS MCP server is a separate file with
the strict psycopg2-only rule.
"""

import json

import psycopg2

from ..config import get_db_url


def get_conn():
    return psycopg2.connect(get_db_url())


def scene_exists(scene_id: str, pipeline_version: str) -> bool:
    """Idempotency check — True if this scene is already ingested at this version."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM scenes WHERE id = %s AND pipeline_version = %s",
            (scene_id, pipeline_version),
        )
        return cur.fetchone() is not None


def register_scene(scene: dict) -> None:
    """Upsert a row into scenes. `scene` keys map to column names."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO scenes (
                id, collection, satellite, sensor, geometry, acq_datetime,
                cloud_cover, gsd_m, processing_level, event_key, window_type,
                properties, assets, pipeline_version
            ) VALUES (
                %(id)s, %(collection)s, %(satellite)s, %(sensor)s,
                ST_GeomFromText(%(footprint_wkt)s, 4326), %(acq_datetime)s,
                %(cloud_cover)s, %(gsd_m)s, %(processing_level)s, %(event_key)s,
                %(window_type)s, %(properties)s, %(assets)s, %(pipeline_version)s
            )
            ON CONFLICT (id) DO UPDATE SET
                collection = EXCLUDED.collection,
                geometry = EXCLUDED.geometry,
                acq_datetime = EXCLUDED.acq_datetime,
                cloud_cover = EXCLUDED.cloud_cover,
                properties = EXCLUDED.properties,
                assets = EXCLUDED.assets,
                pipeline_version = EXCLUDED.pipeline_version,
                ingested_at = now()
            """,
            {
                **scene,
                "properties": json.dumps(scene["properties"]),
                "assets": json.dumps(scene["assets"]),
            },
        )
        conn.commit()


def cache_metrics(scene_id: str, aoi_hash: str, metrics: dict[str, float],
                  pipeline_version: str) -> int:
    """Insert/refresh scene_metrics rows. Returns number of metrics written."""
    if not metrics:
        return 0
    with get_conn() as conn, conn.cursor() as cur:
        for metric, value in metrics.items():
            cur.execute(
                """
                INSERT INTO scene_metrics
                    (scene_id, aoi_hash, metric, value, pipeline_version)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (scene_id, aoi_hash, metric, pipeline_version)
                DO UPDATE SET value = EXCLUDED.value, computed_at = now()
                """,
                (scene_id, aoi_hash, metric, value, pipeline_version),
            )
        conn.commit()
    return len(metrics)


def register_derived_product(scene_id: str, product_type: str, relative_path: str,
                             pipeline_version: str, metadata: dict | None = None) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO derived_products
                (scene_id, product_type, relative_path, pipeline_version, metadata)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (scene_id, product_type, relative_path, pipeline_version,
             json.dumps(metadata or {})),
        )
        conn.commit()
