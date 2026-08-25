"""Benchmark bank version snapshots — pure SQL reader.

Fetch a specific version (or ``latest``) of a bank from ``hr.battery`` /
``hr.battery_item`` / ``hr.item_pool`` and summarize it: item count,
difficulty distribution, and holdout count.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class BenchmarkBankVersion:
    """Versioned snapshot of a benchmark bank."""

    bank_code: str
    version: str
    item_count: int
    created_at: str
    difficulty_distribution: dict[str, int]  # stratum -> count
    holdout_count: int
    metadata: dict[str, Any]

    def get_stratum_items(self, stratum: str) -> list[str]:
        """Get item IDs for a specific difficulty stratum."""
        return list(self.metadata.get("stratum_items", {}).get(stratum, []))


def fetch_bank_version(
    conn, bank_code: str, version: str = "latest"
) -> BenchmarkBankVersion | None:
    """Get a specific version of a benchmark bank (None when unknown)."""
    with conn.cursor() as cur:
        if version == "latest":
            cur.execute(
                """
                SELECT b.battery_code, b.version, COUNT(*) as item_count,
                       MIN(b.created_at) as created_at
                FROM hr.battery_item bi
                JOIN hr.battery b ON bi.battery_id = b.battery_id
                JOIN hr.item_pool ip ON bi.item_id = ip.item_id
                WHERE b.battery_code = %s
                GROUP BY b.battery_code, b.version
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (bank_code,),
            )
        else:
            cur.execute(
                """
                SELECT b.battery_code, b.version, COUNT(*) as item_count,
                       MIN(b.created_at) as created_at
                FROM hr.battery_item bi
                JOIN hr.battery b ON bi.battery_id = b.battery_id
                JOIN hr.item_pool ip ON bi.item_id = ip.item_id
                WHERE b.battery_code = %s AND b.version = %s
                GROUP BY b.battery_code, b.version
                """,
                (bank_code, version),
            )

        row = cur.fetchone()

    if row is None:
        return None

    code, ver, count, created = row

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(ip.json_meta->>'difficulty', 'medium') as difficulty,
                   COUNT(*) as count
            FROM hr.battery_item bi
            JOIN hr.battery b ON bi.battery_id = b.battery_id
            JOIN hr.item_pool ip ON bi.item_id = ip.item_id
            WHERE b.battery_code = %s AND b.version = %s
            GROUP BY difficulty
            """,
            (code, ver),
        )
        difficulty_rows = cur.fetchall()

    difficulty_dist = {row[0]: row[1] for row in difficulty_rows}

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM hr.battery_item bi
            JOIN hr.battery b ON bi.battery_id = b.battery_id
            JOIN hr.item_pool ip ON bi.item_id = ip.item_id
            WHERE b.battery_code = %s AND b.version = %s
              AND (ip.json_meta->>'holdout')::boolean = true
            """,
            (code, ver),
        )
        holdout_count = cur.fetchone()[0]

    return BenchmarkBankVersion(
        bank_code=code,
        version=ver,
        item_count=count,
        created_at=str(created),
        difficulty_distribution=difficulty_dist,
        holdout_count=holdout_count,
        metadata={},
    )


__all__ = ["BenchmarkBankVersion", "fetch_bank_version"]