import logging

from solders.keypair import Keypair

from phantom.config import install_redaction, load_env_file
from phantom.wallet import load_keypair


def test_env_file_parsing(tmp_path):
    p = tmp_path / ".env"
    p.write_text('# comment\nA=1\nB="two words"\n\nC = x=y\n')
    assert load_env_file(p) == {"A": "1", "B": "two words", "C": "x=y"}


def test_secrets_are_redacted_from_logs(caplog):
    install_redaction(["supersecret123"])
    with caplog.at_level(logging.INFO):
        logging.getLogger("phantom.test").info("token is supersecret123 ok")
    assert "supersecret123" not in caplog.text
    assert "[REDACTED]" in caplog.text


def test_keypair_must_match_the_expected_address():
    kp = Keypair()
    assert load_keypair(str(kp), str(kp.pubkey())).pubkey() == kp.pubkey()
    try:
        load_keypair(str(kp), str(Keypair().pubkey()))
        raise AssertionError("expected mismatch error")
    except ValueError as e:
        assert str(kp) not in str(e)  # never echo the secret
