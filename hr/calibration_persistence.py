from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

from hr.calibration_models import CalibrationReport, Measurement

class CalibrationPersistenceMixin(Protocol):
    resume: bool
    db: Any | None
    pool_hash: str
    _recorded_pairs: set[tuple[str, str]]
    _recorded_measurements: list[Measurement]

    def _load_recorded_pairs(self) -> None:
        """Preload anchor and item pairs persisted for the current pool."""
        if not (self.resume and self.db is not None):
            return
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT anchor, item_id, battery, tier, item_type, score,
                           passed, tokens_in, tokens_out, latency_ms,
                           infra_failure, evidence_json
                      FROM hr.calibration_event
                     WHERE pool_hash = %s
                       AND kind = 'anchor_measurement'
                       AND item_type IS NOT NULL
                       AND passed IS NOT NULL
                    """,
                    (self.pool_hash,),
                )
                for row in cur.fetchall():
                    # Anchor rows missing the measurement core (tier/score)
                    # carry no reconstructible signal — skip them instead
                    # of crashing on int(None)/float(None).
                    if row[3] is None or row[5] is None:
                        continue
                    detail_value = row[11]
                    if isinstance(detail_value, dict):
                        detail = detail_value
                    elif isinstance(detail_value, str):
                        # psycopg2 already parsed the JSON document: a JSON
                        # *string* arrives as its inner text. Parse it only
                        # when it still decodes (e.g. nested-JSON strings);
                        # otherwise keep the verbatim text.
                        try:
                            detail = json.loads(detail_value)
                        except json.JSONDecodeError:
                            detail = detail_value
                    else:
                        detail = {}
                    self._recorded_measurements.append(
                        Measurement(
                            anchor=str(row[0]),
                            item_key=str(row[1]),
                            battery=str(row[2]),
                            tier=int(row[3]),
                            item_type=str(row[4]),
                            score=float(row[5]),
                            passed=bool(row[6]),
                            tokens_in=int(row[7] or 0),
                            tokens_out=int(row[8] or 0),
                            latency_ms=int(row[9] or 0),
                            infra_failure=str(row[10]) if row[10] else None,
                            detail=detail,
                        )
                    )
                self._recorded_pairs = {
                    (measurement.anchor, measurement.item_key)
                    for measurement in self._recorded_measurements
                }

    def _persist(self, report: CalibrationReport) -> None:
        if self.db is None:
            return
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                for m in report.measurements:
                    event_key = "|".join(
                        (report.pool_hash, m.anchor, m.item_key)
                    ).encode("utf-8")
                    cur.execute(
                        """
                        INSERT INTO hr.calibration_event
                            (event_id, item_id, kind, pool_hash, anchor, battery,
                             tier, item_type, score, passed, tokens_in, tokens_out, latency_ms,
                             infra_failure, evidence_json)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s::jsonb)
                        ON CONFLICT (event_id) DO UPDATE SET
                            item_type = EXCLUDED.item_type,
                            score = EXCLUDED.score,
                            passed = EXCLUDED.passed,
                            tokens_in = EXCLUDED.tokens_in,
                            tokens_out = EXCLUDED.tokens_out,
                            latency_ms = EXCLUDED.latency_ms,
                            infra_failure = EXCLUDED.infra_failure,
                            evidence_json = EXCLUDED.evidence_json
                        """,
                        (
                            f"cal-{hashlib.sha256(event_key).hexdigest()}",
                            m.item_key,
                            "anchor_measurement",
                            report.pool_hash,
                            m.anchor,
                            m.battery,
                            m.tier,
                            m.item_type,
                            m.score,
                            m.passed,
                            m.tokens_in,
                            m.tokens_out,
                            m.latency_ms,
                            m.infra_failure,
                            json.dumps(m.detail, sort_keys=True),
                        ),
                    )
            conn.commit()


__all__ = ["CalibrationPersistenceMixin"]
