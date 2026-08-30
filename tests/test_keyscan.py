"""Tests for outbound secret scanning (spec §12.1)."""
import pytest

from hr.keyscan import SecretLeakError, redact, scan_outbound, patterns


# ---------------------------------------------------------------------------
# Synthetic leak patterns — fake strings of the SAME SHAPE as real secrets.
# NEVER real key material.
# ---------------------------------------------------------------------------
SYNTH_SK_SP = "sk-sp-AAAAAAAAAAAAAAAAAAAA1234BBBBBBBB"
SYNTH_SK_KIMI = "sk-kimi-CCCCCCCCCCCCCCCCDDDD1111EEEEEEEE"
SYNTH_SK_ANT = "sk-ant-FFFFFFFFFFFFFFFFGGGG2222HHHHHHHH"
SYNTH_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ."
    "DOdEbm5lY2hlY2tzMTIzNDU2Nzg5MDEyMzQ1Njc4"
)
SYNTH_PG = "postgres://admin:s3cretPaSsW0rD@db.example.com:5432/hr_prod"
SYNTH_BEARER = "Bearer eHl6LmFiYy5kZWYuaGlnaGVudHJvcHlUb2tlbjEyMw"
SYNTH_PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA..."
# High-entropy 24-char pseudo-random string (entropy > 4.0 bits/char).
SYNTH_PASSWORD_ASSIGN = (
    "DB_PASSWORD='OhbVrpoiVgRV5IfLBcbfnoGM' and more text"
)
SYNTH_ENV_EXPORT = 'AWS_SECRET_ACCESS_KEY="abcdefghij1234567890klmn"'


class TestScanOutbound:
    def test_no_leak_passes(self):
        scan_outbound("This is a perfectly normal text with no secrets.")

    def test_sk_sp_detected(self):
        with pytest.raises(SecretLeakError) as exc:
            scan_outbound(f"got key {SYNTH_SK_SP}")
        assert "sk_sp" in exc.value.patterns

    def test_sk_kimi_detected(self):
        with pytest.raises(SecretLeakError) as exc:
            scan_outbound(f"kimi key: {SYNTH_SK_KIMI}")
        assert "sk_kimi" in exc.value.patterns

    def test_sk_ant_detected(self):
        with pytest.raises(SecretLeakError) as exc:
            scan_outbound(f"token {SYNTH_SK_ANT}")
        assert "sk_ant" in exc.value.patterns

    def test_jwt_detected(self):
        with pytest.raises(SecretLeakError) as exc:
            scan_outbound(f"Authorization: {SYNTH_JWT}")
        assert "jwt" in exc.value.patterns

    def test_postgres_url_detected(self):
        with pytest.raises(SecretLeakError) as exc:
            scan_outbound(f"dsn={SYNTH_PG}")
        assert "postgres_url" in exc.value.patterns

    def test_bearer_token_detected(self):
        with pytest.raises(SecretLeakError) as exc:
            scan_outbound(f"curl -H 'Authorization: {SYNTH_BEARER}'")
        assert "bearer_token" in exc.value.patterns

    def test_pem_detected(self):
        with pytest.raises(SecretLeakError) as exc:
            scan_outbound(SYNTH_PEM)
        assert "pem_private_key" in exc.value.patterns

    def test_high_entropy_password_assignment_detected(self):
        with pytest.raises(SecretLeakError) as exc:
            scan_outbound(SYNTH_PASSWORD_ASSIGN)
        assert "password_assignment" in exc.value.patterns

    def test_low_entropy_password_ignored(self):
        # Common words should not trigger the password grader.
        scan_outbound("DB_PASSWORD='hunter2' (very low entropy)")


class TestRedact:
    def test_redact_replaces_pattern(self):
        text = f"my key is {SYNTH_SK_SP} okay?"
        out = redact(text)
        assert SYNTH_SK_SP not in out
        assert "REDACTED" in out

    def test_redact_jwt_does_not_echo(self):
        # Ensure the JWT token itself is NOT echoed back in the output.
        out = redact(f"jwt {SYNTH_JWT}")
        assert SYNTH_JWT not in out

    def test_redact_no_false_positive(self):
        text = "This is a plain sentence."
        assert redact(text) == text

    def test_patterns_never_echo_secret_content_in_exception(self):
        # The error's str should not contain the secret itself — only the
        # PATTERN NAMES.
        try:
            scan_outbound(f"secret={SYNTH_SK_KIMI}")
        except SecretLeakError as exc:
            msg = str(exc)
            assert SYNTH_SK_KIMI not in msg
            assert "sk_kimi" in msg


class TestPatterns:
    def test_all_pattern_classes_registered(self):
        names = patterns()
        for expected in [
            "jwt",
            "postgres_url",
            "bearer_token",
            "pem_private_key",
            "sk_sp",
            "sk_kimi",
            "sk_ant",
            "api_key_prefix",
            "password_assignment",
            "env_secret_export",
        ]:
            assert expected in names
