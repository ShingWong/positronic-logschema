"""Shared fixtures: a synthetic corpus whose answers are known by construction."""

from __future__ import annotations

import json

import pytest


def build_records(n_sessions=40, lines_per=40, start=1_700_000_000):
    """A synthetic Postfix-shaped corpus.

    The verdicts are known because the generator enforces them:
      - (svc, pid) groups `lines_per` records inside a 2-minute burst  -> session
      - svc alone spreads every session across the whole corpus         -> bucket
      - `q` is masked, so it can never be an identity key
      - `stamp` is a real ISO clock
    """
    recs = []
    t = start
    for s in range(n_sessions):
        svc = "smtpd" if s % 4 else "qmgr"
        pid = 1000 + s
        for k in range(lines_per):
            t += 2
            recs.append({
                "stamp": _iso(t),
                "ident": f"postfix/{svc}",
                "svc": svc,
                "pid": pid,
                "q": f"<HEX{s % 3}>",
                "tok": _token(svc, k, t),
                "file": "maillog",
            })
    return recs


def _iso(t: int) -> str:
    import datetime
    return (datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S"))


def _token(svc: str, k: int, t: int) -> str:
    if svc == "smtpd":
        if k == 0:
            return "connect from <HOST>.us[I:<HEX>]"
        if k == 1:
            return "(<N>-<N>) smtp cmd> EHLO mx<N>.example.net"
        if k == 2:
            return "<HOST>:<N>: client=<HOST>[I:<HEX>]"
        if k == 3:
            return "smtp resp to EHLO: <N> <KEY>\nSIZE <KEY>\nDSN"
        # Order matters and was wrong once: the SASL branch sat above the
        # disconnect branch, and 39 % 7 == 4, so `disconnect` was unreachable
        # and the fixture silently had no disconnect class to match against.
        if k == lines_per_default - 1:
            return "disconnect from <HOST>.us[I:<HEX>]"
        if k % 7 == 4:
            return "warning: <HOST>[I:<HEX>]: SASL LOGIN authentication failed: <EMAIL>"
        return "smtp cmd> MAIL FROM:<<EMAIL>> <KEY>"
    if k == 0:
        return "Q:<HEX>: from=<<EMAIL>>, size=<N>, nrcpt=<N> (queue active)"
    if k % 5 == 1:
        return "Q:<HEX>: removed"
    return "(<N>-<N>) smtp cmd> RCPT TO:<<EMAIL>> <KEY>"


lines_per_default = 40


@pytest.fixture
def records():
    return build_records()


@pytest.fixture
def jsonl(tmp_path, records):
    p = tmp_path / "synthetic.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return str(p)
