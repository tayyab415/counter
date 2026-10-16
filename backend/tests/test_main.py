"""main() must listen on $PORT: Cloud Run injects it; local dev keeps 8000."""
from counter_backend import main as main_mod


def test_server_port_defaults_to_8000(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    assert main_mod.server_port() == 8000


def test_server_port_reads_port_env(monkeypatch):
    monkeypatch.setenv("PORT", "8080")
    assert main_mod.server_port() == 8080
