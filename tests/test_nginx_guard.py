"""SYNTHETIC infrastructure fixtures. No real NGINX, Certbot, HTTPS or SSH call runs."""

import hashlib
import importlib.util
import json
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if not SCRIPTS.exists():
    SCRIPTS = Path("/app/scripts")


def module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


g = module("guarded_nginx_reload")
r = module("renew_quantix_tls")
OLD = ["/etc/nginx/nginx.conf", "/home/dokku/bgm/nginx.conf"]
WARNING = '2026/11/22 00:00:00 [warn] 12#12: conflicting server name "synthetic.invalid" on 0.0.0.0:443, ignored\n'


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def environment(monkeypatch):
    state = SimpleNamespace(
        files=[*OLD, g.VHOST],
        contents={
            OLD[0]: "SYNTHETIC main",
            OLD[1]: "SYNTHETIC protected BGM",
            g.VHOST: "SYNTHETIC own host",
        },
        stderr=WARNING,
        syntax_code=0,
        clock="yes",
        mtimes={OLD[0]: 900, OLD[1]: 900, g.VHOST: 1100},
        sent=[],
        calls=[],
        https=[],
        master=12345,
        new_generation=True,
    )
    baseline = {
        "loaded_files_in_order": OLD.copy(),
        "loaded_file_sha256": {p: sha(state.contents[p]) for p in OLD},
        "warnings": g.warnings(WARNING),
        "quantix_vhost_sha256": sha(state.contents[g.VHOST]),
    }

    def runner(args, **kwargs):
        state.calls.append(args)
        if args[0] == "timedatectl":
            return SimpleNamespace(returncode=0, stdout=state.clock, stderr="")
        if args == ["nginx", "-T"]:
            output = "\n".join(
                f"# configuration file {p}:\n{state.contents.get(p, 'SYNTHETIC omitted')}"
                for p in state.files
            )
            return SimpleNamespace(
                returncode=state.syntax_code, stdout=output, stderr=state.stderr
            )
        if args == ["nginx", "-t"]:
            return SimpleNamespace(
                returncode=state.syntax_code, stdout="", stderr=state.stderr
            )
        raise AssertionError(f"Unexpected SYNTHETIC runner call: {args[0]}")

    monkeypatch.setattr(g, "run", runner)
    monkeypatch.setattr(g, "digest", lambda p: sha(state.contents[p]))
    monkeypatch.setattr(g, "modified_at", lambda p: state.mtimes[p])
    monkeypatch.setattr(g, "master_pid", lambda: state.master)
    monkeypatch.setattr(g, "worker_started_at", lambda ticks: ticks)
    monkeypatch.setattr(
        g,
        "worker_generation",
        lambda master: 1100 if state.sent and state.new_generation else 1000,
    )
    monkeypatch.setattr(
        g, "protected_https", lambda: state.https.append("SYNTHETIC 200")
    )
    monkeypatch.setattr(g.os, "kill", lambda pid, sig: state.sent.append((pid, sig)))
    return state, baseline


def test_dry_check_validates_without_any_signal(environment):
    state, baseline = environment
    result = g.checked_reload(baseline, baseline["quantix_vhost_sha256"])
    assert result["status"] == "checks_passed" and result["applied"] is False
    assert state.sent == [] and state.https == ["SYNTHETIC 200"]














@pytest.fixture
def lineage(tmp_path, monkeypatch):
    config = tmp_path / "synthetic-certbot/config"
    config.mkdir(parents=True)
    fullchain = config / "live" / r.DOMAIN / "fullchain.pem"
    fullchain.parent.mkdir(parents=True)
    fullchain.write_bytes(b"SYNTHETIC public certificate before")
    renewal = config / "renewal" / f"{r.DOMAIN}.conf"
    renewal.parent.mkdir()
    cli = config.parent / "cli.ini"
    cli.write_text("# SYNTHETIC empty isolated config\n")
    webroot = tmp_path / "synthetic-webroot"
    text = "\n".join(
        [
            f"archive_dir = {config}/archive/{r.DOMAIN}",
            f"cert = {fullchain.parent}/cert.pem",
            f"privkey = {fullchain.parent}/privkey.pem",
            f"chain = {fullchain.parent}/chain.pem",
            f"fullchain = {fullchain}",
            "[renewalparams]",
            "authenticator = webroot",
            "server = https://acme-v02.api.letsencrypt.org/directory",
            f"webroot_path = {webroot},",
            "[[webroot_map]]",
            f"{r.DOMAIN} = {webroot}",
        ]
    )
    renewal.write_text(text)
    for name, path in {
        "CONFIG": config,
        "WORK": config.parent / "work",
        "LOGS": config.parent / "logs",
        "FULLCHAIN": fullchain,
        "RENEWAL": renewal,
        "CLI_CONFIG": cli,
        "WEBROOT": webroot,
    }.items():
        monkeypatch.setattr(r, name, path)
    state = SimpleNamespace(
        calls=[], guards=[], changed=False, failed=False, names=r.DOMAIN
    )

    def runner(args, **kwargs):
        state.calls.append(args)
        if args[0] == "openssl":
            return SimpleNamespace(
                returncode=0,
                stdout=f"X509v3 Subject Alternative Name:\n DNS:{state.names}\n",
                stderr="",
            )
        assert args[:2] == ["certbot", "renew"]
        if state.changed:
            fullchain.write_bytes(b"SYNTHETIC public certificate after")
        return SimpleNamespace(
            returncode=1 if state.failed else 0,
            stdout="",
            stderr="SYNTHETIC secret-like provider text",
        )

    monkeypatch.setattr(r, "run", runner)
    monkeypatch.setattr(
        r,
        "checked_reload",
        lambda b, c, apply=False: state.guards.append(apply) or {"applied": apply},
    )
    return state, renewal, cli
































