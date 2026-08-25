"""Benchmark bank versioning, exposure tracking, and state governance.

DB surface for benchmark item pools:

- Versioning: track which version of items was used in each evaluation
- Stratification: organize items by difficulty/characteristics
- Exposure tracking: monitor how many times each item has been shown to models
- Item state: every item is ``active``, ``holdout``, ``retired``, or
  ``flagged``; selection only ever draws from the active pool
- Contamination disclosure: every selection reports its method, exclusions,
  and caveats so reports are machine-readable

Sibling readers keep the SQL surface honest: version snapshots live in
:mod:`hr.bank_versions`, the pure selection algorithm in
:mod:`hr.bank_selection`; this module owns the manager and the remaining
SQL (exposure, state, safe-items).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from hr.bank_selection import (
    BankSelectionPolicy,
    BankSelectionResult,
    ItemExclusion,
    ItemState,
    SelectedBankItem,
    build_selection,
)
from hr.bank_versions import BenchmarkBankVersion, fetch_bank_version


@dataclass
class ItemExposure:
    """Track exposure statistics for a benchmark item."""

    item_id: str
    total_exposures: int
    unique_models_exposed: int
    last_exposed_at: str | None
    contamination_risk: float  # 0.0 (low) to 1.0 (high)

    def is_safe_for_evaluation(self, max_exposures: int = 100) -> bool:
        """Check if item is still safe to use (below exposure threshold)."""
        return self.total_exposures < max_exposures


class BenchmarkBankManager:
    """Manage benchmark bank versions, item state, and exposure tracking."""

    def __init__(self, conn) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # exposure
    # ------------------------------------------------------------------
    def get_item_exposure(self, item_id: str) -> ItemExposure:
        """Get exposure statistics for a specific item.

        3-column SELECT contract (total, unique, last) — pinned by unit
        fakes and recomputed live from ``hr.measurement``.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*) as total_exposures,
                    COUNT(DISTINCT r.model_id) as unique_models,
                    MAX(m.created_at) as last_exposed
                FROM hr.measurement m
                JOIN hr.run r ON r.run_id = m.run_id
                WHERE m.item_id = %s
                """,
                (item_id,),
            )
            row = cur.fetchone()

        if row is None:
            return ItemExposure(
                item_id=item_id,
                total_exposures=0,
                unique_models_exposed=0,
                last_exposed_at=None,
                contamination_risk=0.0,
            )

        total, unique, last = row
        risk = min(1.0, total / 100.0)  # Normalize to 0-1 scale

        return ItemExposure(
            item_id=item_id,
            total_exposures=total,
            unique_models_exposed=unique,
            last_exposed_at=str(last) if last else None,
            contamination_risk=risk,
        )

    # ------------------------------------------------------------------
    # item state
    # ------------------------------------------------------------------
    def get_item_state(self, item_id: str) -> ItemState:
        """Resolve an item's governance state.

        The legacy ``holdout`` json_meta flag wins — it is the durable
        mirror of ``set_item_state(HOLDOUT)`` — and unset state defaults
        to ``active``.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT CASE
                    WHEN (ip.json_meta->>'holdout')::boolean IS TRUE THEN 'holdout'
                    ELSE COALESCE(ip.json_meta->>'state', 'active')
                END
                FROM hr.item_pool ip
                WHERE ip.item_id = %s
                """,
                (item_id,),
            )
            row = cur.fetchone()
        if row is None or row[0] is None:
            return ItemState.ACTIVE
        return ItemState(row[0])

    def set_item_state(self, item_id: str, state: ItemState) -> int:
        """Persist a state and keep the legacy ``holdout`` flag in sync.

        Returns the affected row count (0 when the item does not exist).
        Callers commit the transaction.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE hr.item_pool ip
                SET json_meta = jsonb_set(
                        jsonb_set(
                            COALESCE(ip.json_meta, '{}'::jsonb),
                            '{state}', %s::jsonb
                        ),
                        '{holdout}', %s::jsonb
                    )
                WHERE ip.item_id = %s
                """,
                (
                    json.dumps(state.value),
                    json.dumps(state is ItemState.HOLDOUT),
                    item_id,
                ),
            )
            rowcount = cur.rowcount
        return rowcount

    # ------------------------------------------------------------------
    # versions and safe-item selection (legacy entry points)
    # ------------------------------------------------------------------
    def get_bank_version(self, bank_code: str, version: str = "latest") -> BenchmarkBankVersion | None:
        """Get a specific version of a benchmark bank."""
        return fetch_bank_version(self._conn, bank_code, version)

    def get_safe_items_for_evaluation(
        self, bank_code: str, count: int, max_exposures: int = 100
    ) -> list[str]:
        """Get items that are safe to use (below exposure threshold).

        Modern ``state`` governance is honored as well as the legacy
        ``holdout`` json_meta flag: only ``active`` items are returned.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT ip.item_id
                FROM hr.battery_item bi
                JOIN hr.battery b ON bi.battery_id = b.battery_id
                JOIN hr.item_pool ip ON bi.item_id = ip.item_id
                LEFT JOIN (
                    SELECT item_id, COUNT(*) as exposures
                    FROM hr.measurement
                    GROUP BY item_id
                ) exp ON ip.item_id = exp.item_id
                WHERE b.battery_code = %s
                  AND (exp.exposures IS NULL OR exp.exposures < %s)
                  AND (ip.json_meta->>'holdout')::boolean IS NOT TRUE
                  AND COALESCE(ip.json_meta->>'state', 'active') = 'active'
                ORDER BY COALESCE(exp.exposures, 0) ASC
                LIMIT %s
                """,
                (bank_code, max_exposures, count),
            )
            rows = cur.fetchall()

        return [row[0] for row in rows]

    def record_evaluation_usage(self, item_ids: list[str]) -> None:
        """Record that items were used in an evaluation (updates exposure counts)."""
        # Exposure is automatically tracked via hr.measurement table
        # This method is a placeholder for any additional bookkeeping
        del item_ids  # unused

    # ------------------------------------------------------------------
    # stratified, exposure-aware, state-aware selection
    # ------------------------------------------------------------------
    def select_items(
        self,
        bank_code: str,
        count: int,
        policy: BankSelectionPolicy | None = None,
    ) -> BankSelectionResult:
        """Draw ``count`` items from the active pool of a bank version.

        Deterministic for a fixed policy: the draw is a seeded, stratified,
        proportional sample across strata (largest-remainder allocation),
        and excluded items are disclosed per reason.

        Raises ``ValueError`` when the bank code/version is unknown.
        """
        policy = policy or BankSelectionPolicy()
        version = self._resolve_version(bank_code, policy.version)

        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT ip.item_id,
                       COALESCE(ip.json_meta->>%s, 'medium') AS stratum,
                       CASE
                           WHEN (ip.json_meta->>'holdout')::boolean IS TRUE THEN 'holdout'
                           ELSE COALESCE(ip.json_meta->>'state', 'active')
                       END AS state,
                       COALESCE(exp.total_exposures, 0) AS total_exposures,
                       COALESCE(exp.unique_models, 0) AS unique_models,
                       exp.last_exposed AS last_exposed
                FROM hr.battery_item bi
                JOIN hr.battery b ON bi.battery_id = b.battery_id
                JOIN hr.item_pool ip ON bi.item_id = ip.item_id
                LEFT JOIN (
                    SELECT m.item_id,
                           COUNT(*) AS total_exposures,
                           COUNT(DISTINCT r.model_id) AS unique_models,
                           MAX(m.created_at) AS last_exposed
                    FROM hr.measurement m
                    JOIN hr.run r ON r.run_id = m.run_id
                    GROUP BY m.item_id
                ) exp ON ip.item_id = exp.item_id
                WHERE b.battery_code = %s AND b.version = %s
                ORDER BY ip.item_id
                """,
                (policy.stratum_key, bank_code, version),
            )
            rows = cur.fetchall()

        return build_selection(
            rows,
            bank_code=bank_code,
            bank_version=version,
            count=count,
            policy=policy,
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _resolve_version(self, bank_code: str, version: str) -> str:
        with self._conn.cursor() as cur:
            if version == "latest":
                cur.execute(
                    """
                    SELECT b.version
                    FROM hr.battery b
                    WHERE b.battery_code = %s
                    ORDER BY b.created_at DESC, b.version DESC
                    LIMIT 1
                    """,
                    (bank_code,),
                )
            else:
                cur.execute(
                    """
                    SELECT b.version
                    FROM hr.battery b
                    WHERE b.battery_code = %s AND b.version = %s
                    """,
                    (bank_code, version),
                )
            row = cur.fetchone()
        if row is None:
            raise ValueError(f"bank {bank_code!r} version {version!r} not found")
        return row[0]


__all__ = [
    "BankSelectionPolicy",
    "BankSelectionResult",
    "BenchmarkBankManager",
    "BenchmarkBankVersion",
    "ItemExclusion",
    "ItemExposure",
    "ItemState",
    "SelectedBankItem",
]