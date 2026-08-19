from __future__ import annotations

import json
from typing import Any

import psycopg2
import psycopg2.extras

from hr.config import load_settings
from hr.models import (
    BenchmarkResult,
    EvaluationReport,
    ModelProfile,
    ResearchFinding,
    RoleAssignment,
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS hr_models (
    id SERIAL PRIMARY KEY,
    provider VARCHAR(100) NOT NULL,
    model_id VARCHAR(200) NOT NULL,
    display_name VARCHAR(300),
    context_window INT,
    max_output INT,
    supports_vision BOOLEAN DEFAULT FALSE,
    supports_thinking BOOLEAN DEFAULT FALSE,
    api_base_url TEXT,
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(provider, model_id)
);

CREATE TABLE IF NOT EXISTS hr_benchmarks (
    id SERIAL PRIMARY KEY,
    model_fk INT REFERENCES hr_models(id) ON DELETE CASCADE,
    benchmark_name VARCHAR(100) NOT NULL,
    score FLOAT,
    latency_ms INT,
    tokens_per_sec FLOAT,
    raw_output TEXT,
    passed BOOLEAN,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS hr_research (
    id SERIAL PRIMARY KEY,
    model_fk INT REFERENCES hr_models(id) ON DELETE CASCADE,
    source_url TEXT,
    finding TEXT NOT NULL,
    category VARCHAR(50),
    confidence FLOAT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS hr_assignments (
    id SERIAL PRIMARY KEY,
    model_fk INT REFERENCES hr_models(id) ON DELETE CASCADE,
    role VARCHAR(100) NOT NULL,
    fit_score FLOAT,
    rationale TEXT,
    is_active BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS hr_reports (
    id SERIAL PRIMARY KEY,
    model_fk INT REFERENCES hr_models(id) ON DELETE CASCADE,
    overall_score FLOAT,
    pros JSONB,
    cons JSONB,
    recommended_roles JSONB,
    summary TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
"""


def get_connection() -> psycopg2.extensions.connection:
    """Return a new psycopg2 connection from current settings."""
    s = load_settings()
    if s.dsn:
        return psycopg2.connect(s.dsn)
    return psycopg2.connect(
        host=s.db_host,
        port=s.db_port,
        dbname=s.db_name,
        user=s.db_user,
        password=s.db_password,
    )


def init_schema() -> None:
    """Create all HR tables if they do not exist."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


def upsert_model(profile: ModelProfile) -> int:
    """INSERT or UPDATE a model row; returns the model id."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO hr_models
                    (provider, model_id, display_name, context_window, max_output,
                     supports_vision, supports_thinking, api_base_url, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (provider, model_id) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    context_window = EXCLUDED.context_window,
                    max_output = EXCLUDED.max_output,
                    supports_vision = EXCLUDED.supports_vision,
                    supports_thinking = EXCLUDED.supports_thinking,
                    api_base_url = EXCLUDED.api_base_url,
                    notes = EXCLUDED.notes,
                    updated_at = NOW()
                RETURNING id
                """,
                (
                    profile.provider,
                    profile.model_id,
                    profile.display_name,
                    profile.context_window,
                    profile.max_output,
                    profile.supports_vision,
                    profile.supports_thinking,
                    profile.api_base_url,
                    profile.notes,
                ),
            )
            model_id: int = cur.fetchone()[0]
        conn.commit()
        return model_id
    finally:
        conn.close()


def save_benchmark(result: BenchmarkResult) -> None:
    """Insert a benchmark result record."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO hr_benchmarks
                    (model_fk, benchmark_name, score, latency_ms, tokens_per_sec,
                     raw_output, passed)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    result.model_fk,
                    result.benchmark_name,
                    result.score,
                    result.latency_ms,
                    result.tokens_per_sec,
                    result.raw_output,
                    result.passed,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def save_research(finding: ResearchFinding) -> None:
    """Insert a research finding record."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO hr_research
                    (model_fk, source_url, finding, category, confidence)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    finding.model_fk,
                    finding.source_url,
                    finding.finding,
                    finding.category,
                    finding.confidence,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def save_assignment(assignment: RoleAssignment) -> None:
    """Insert a role assignment record."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO hr_assignments
                    (model_fk, role, fit_score, rationale, is_active)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    assignment.model_fk,
                    assignment.role,
                    assignment.fit_score,
                    assignment.rationale,
                    assignment.is_active,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def save_report(report: EvaluationReport) -> None:
    """Insert an evaluation report record."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO hr_reports
                    (model_fk, overall_score, pros, cons, recommended_roles, summary)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    report.model_fk,
                    report.overall_score,
                    json.dumps(report.pros),
                    json.dumps(report.cons),
                    json.dumps([r.value for r in report.recommended_roles]),
                    report.summary,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def get_model(provider: str, model_id: str) -> int | None:
    """Return the model id for a given provider/model_id, or None."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM hr_models WHERE provider = %s AND model_id = %s",
                (provider, model_id),
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def list_models() -> list[ModelProfile]:
    """Return all registered models."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT provider, model_id, display_name, context_window, max_output,
                       supports_vision, supports_thinking, api_base_url, notes
                FROM hr_models ORDER BY provider, model_id
                """
            )
            rows: list[dict[str, Any]] = cur.fetchall()
        return [ModelProfile(**row) for row in rows]
    finally:
        conn.close()


def get_assignments() -> list[RoleAssignment]:
    """Return all active role assignments."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT model_fk, role, fit_score, rationale, is_active
                FROM hr_assignments WHERE is_active = TRUE
                ORDER BY role
                """
            )
            rows: list[dict[str, Any]] = cur.fetchall()
        return [RoleAssignment(**row) for row in rows]
    finally:
        conn.close()
