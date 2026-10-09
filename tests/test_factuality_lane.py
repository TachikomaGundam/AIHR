"""B2 factuality battery: scorer matrix, offline runner, live registration."""
from __future__ import annotations

from types import SimpleNamespace

from hr.bench.prompts import FACTUALITY_CARDS


def _pair(card_i: int, *, key: bool, fabricate: bool) -> tuple[str, str]:
    card = FACTUALITY_CARDS[card_i]
    ans = str(card["key"]) if key else "not stated"
    trap = (str(card["forbidden"][0]) if card["forbidden"] else "yes") if fabricate else "not stated"
    return ans, trap


def test_scorer_balanced_matrix() -> None:
    from hr.bench.scorers import score_factuality

    perfect = score_factuality([_pair(i, key=True, fabricate=False) for i in range(8)])
    assert perfect.score == 100.0 and perfect.passed
    always_refuse = score_factuality([_pair(i, key=False, fabricate=False) for i in range(8)])
    assert always_refuse.score == 0.0  # no free pass for stonewalling
    always_fabricate = score_factuality([_pair(i, key=True, fabricate=True) for i in range(8)])
    assert always_fabricate.score == 0.0  # no free pass for confabulating
    assert perfect.raw_output.startswith("8/8")
    labels = [lab for lab, _ in perfect.item_scores]
    assert labels == [f"f{i:02d}" for i in range(8)]


class _StubEngine:
    """Offline runner harness: scripts 16 responses (answer, trap) x 8."""

    _timeout_s = 5

    def __init__(self, responder):
        self.responder = responder
        self.calls = 0

    def _chat(self, model_id, adapter, caps, cr):
        self.calls += 1
        prompt = cr.messages[0]["content"]
        return SimpleNamespace(
            text=self.responder(prompt, self.calls), thinking="", tool_calls=[],
            raw={}, latency_ms=50, tokens_in=100, tokens_out=20,
        )


def test_runner_end_to_end_offline() -> None:
    from hr.bench.engine_runners import EngineRunnersMixin

    def responder(prompt: str, call_no: int) -> str:
        # trap calls carry the exact trap instruction marker
        if "say exactly 'not stated'" in prompt:
            return "not stated"
        import re
        m = re.search(r"How many drivers|captain.*name|people are in the archive|"
                      r"beta product called|minutes do trams|PTA founded|"
                      r"tall is the mast|roast", prompt)
        return {1: "42", 2: "Vasik", 3: "6", 4: "Plumspace", 5: "6",
                6: "1974", 7: "87", 8: "Harbor Dark"}.get((call_no + 1) // 2, "42")

    eng = _StubEngine(responder)
    res = EngineRunnersMixin._run_factuality(eng, "m/x", None, None)
    assert eng.calls == 16
    assert res.outcome.score == 100.0, res.outcome.item_scores
    assert res.tokens_out == 320


def test_runner_persists_both_sides_of_every_card() -> None:
    """#12 (round-7 field audit): response_text carried only trap lines, so
    every key_hit verdict was the scorer's self-report, blind-replayable by
    nobody. Both sides of all 8 cards must live in the persisted text."""
    from hr.bench.engine_runners import EngineRunnersMixin

    def responder(prompt: str, call_no: int) -> str:
        if "say exactly 'not stated'" in prompt:
            return "not stated"
        return {1: "42 drivers on Portside", 2: "captain Elin Vasik",
                3: "6 people in archive", 4: "Plumspace beta",
                5: "every 6 minutes", 6: "founded 1974",
                7: "87 meters", 8: "Harbor Dark roast"}[(call_no + 1) // 2]

    eng = _StubEngine(responder)
    res = EngineRunnersMixin._run_factuality(eng, "m/x", None, None)
    assert res.response_text.count("[ans] ") == 8
    assert res.response_text.count("[trap] ") == 8
    for card in FACTUALITY_CARDS:
        assert str(card["key"]).lower() in res.response_text.lower()


def test_factuality_registered_in_live_schema(scratch_db) -> None:
    from hr.bench import LivebenchEngine
    import psycopg2

    _name, dsn = scratch_db
    conn = psycopg2.connect(dsn)
    try:
        LivebenchEngine().ensure_registered(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM hr.battery_item bi "
                "JOIN hr.battery b USING(battery_id) "
                "WHERE b.battery_code = 'livebench_factuality'"
            )
            assert cur.fetchone()[0] == 8
    finally:
        conn.close()
