"""Where `penny board` listens: loopback unless asked, a warning for --host."""

import pytest

from penny import board


def test_loopback_by_default(monkeypatch, capsys):
    monkeypatch.setattr(board, "tailscale_ip", lambda: pytest.fail("no Tailscale lookup by default"))
    assert board.bind_address() == ("127.0.0.1", set())
    assert capsys.readouterr().err == ""


def test_host_binds_but_warns(capsys):
    assert board.bind_address("0.0.0.0") == ("0.0.0.0", set())
    assert "warning" in capsys.readouterr().err
    board.bind_address("127.0.0.1")
    assert capsys.readouterr().err == ""  # loopback said out loud is still loopback


def test_tailscale_binds_its_ipv4_and_names(monkeypatch):
    monkeypatch.setattr(board, "tailscale_ip", lambda: "100.64.0.7")
    monkeypatch.setattr(board, "tailscale_names", lambda: {"board.example", "board"})
    assert board.bind_address(tailscale=True) == ("100.64.0.7", {"board.example", "board"})


def test_tailscale_without_an_address_exits(monkeypatch):
    monkeypatch.setattr(board, "tailscale_ip", lambda: None)
    with pytest.raises(SystemExit, match="Tailscale"):
        board.bind_address(tailscale=True)
