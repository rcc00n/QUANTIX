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


def test_apply_sends_only_one_hup_and_preserves_master(environment, monkeypatch):
    state, baseline = environment
    monkeypatch.setattr(g.os, "geteuid", lambda: 0)
    result = g.checked_reload(baseline, baseline["quantix_vhost_sha256"], apply=True)
    assert result["applied"] and state.sent == [(12345, signal.SIGHUP)]
    assert len(state.https) == 2
    assert all("restart" not in args and "stop" not in args for args in state.calls)


@pytest.mark.parametrize(
    "change,code",
    [
        ("protected_hash", "PROTECTED_CONFIG_HASH_CHANGED"),
        ("own_hash", "QUANTIX_VHOST_NOT_REVIEWED"),
        ("include_order", "PROTECTED_INCLUDE_ORDER_CHANGED"),
        ("extra_include", "PROTECTED_INCLUDE_ORDER_CHANGED"),
        ("extra_warning", "NGINX_WARNING_MULTISET_CHANGED"),
        ("missing_own", "EXACTLY_ONE_QUANTIX_VHOST_REQUIRED"),
        ("pending_edit", "PENDING_UNRELATED_CONFIG_EDIT"),
        ("syntax", "NGINX_SYNTAX_INVALID"),
        ("clock", "VPS_CLOCK_NOT_SYNCHRONIZED"),
    ],
)
def test_unrelated_or_unreviewed_changes_fail_closed_before_hup(
    environment, change, code
):
    state, baseline = environment
    if change == "protected_hash":
        state.contents[OLD[1]] = "SYNTHETIC unrelated edit"
    elif change == "own_hash":
        state.contents[g.VHOST] = "SYNTHETIC unreviewed edit"
    elif change == "include_order":
        state.files = [OLD[1], OLD[0], g.VHOST]
    elif change == "extra_include":
        state.files.append("/etc/nginx/conf.d/synthetic-other.conf")
    elif change == "extra_warning":
        state.stderr += WARNING
    elif change == "missing_own":
        state.files = OLD.copy()
    elif change == "pending_edit":
        state.mtimes[OLD[1]] = 1001
    elif change == "syntax":
        state.syntax_code = 1
    elif change == "clock":
        state.clock = "no"
    with pytest.raises(g.GuardError, match=code):
        g.checked_reload(baseline, baseline["quantix_vhost_sha256"], apply=True)
    assert state.sent == []


def test_unrelated_change_during_https_check_prevents_signal(environment, monkeypatch):
    state, baseline = environment
    monkeypatch.setattr(g.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        g, "protected_https", lambda: state.contents.update({OLD[1]: "SYNTHETIC race"})
    )
    with pytest.raises(g.GuardError, match="PROTECTED_CONFIG_HASH_CHANGED"):
        g.checked_reload(baseline, baseline["quantix_vhost_sha256"], apply=True)
    assert state.sent == []


def test_failed_postcheck_explicitly_reports_that_reload_already_happened(
    environment, monkeypatch
):
    state, baseline = environment
    monkeypatch.setattr(g.os, "geteuid", lambda: 0)

    def https():
        if state.sent:
            raise g.GuardError("PROTECTED_HTTPS_NOT_HEALTHY")

    monkeypatch.setattr(g, "protected_https", https)
    with pytest.raises(g.GuardError) as exc:
        g.checked_reload(baseline, baseline["quantix_vhost_sha256"], apply=True)
    assert exc.value.reload_applied is True and state.sent == [(12345, signal.SIGHUP)]


def test_hup_without_new_workers_is_not_reported_as_success(environment, monkeypatch):
    state, baseline = environment
    monkeypatch.setattr(g.os, "geteuid", lambda: 0)
    state.new_generation = False
    with pytest.raises(
        g.GuardError, match="NEW_NGINX_WORKER_GENERATION_NOT_OBSERVED"
    ) as exc:
        g.checked_reload(
            baseline, baseline["quantix_vhost_sha256"], apply=True, timeout=0
        )
    assert exc.value.reload_applied and len(state.sent) == 1


def test_baseline_rejects_symlinks_and_writable_files(tmp_path):
    baseline = {
        "loaded_files_in_order": OLD,
        "loaded_file_sha256": {p: sha("SYNTHETIC") for p in OLD},
        "warnings": [],
    }
    path = tmp_path / "synthetic-baseline.json"
    path.write_text(json.dumps(baseline))
    path.chmod(0o600)
    assert g.load_baseline(path) == baseline
    link = tmp_path / "synthetic-link.json"
    link.symlink_to(path)
    with pytest.raises(g.GuardError, match="BASELINE_PERMISSIONS_INVALID"):
        g.load_baseline(link)
    path.chmod(0o666)
    with pytest.raises(g.GuardError, match="BASELINE_PERMISSIONS_INVALID"):
        g.load_baseline(path)


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


def test_renewal_is_dry_by_default_and_never_calls_certbot(lineage):
    state, _, _ = lineage
    result = r.renew({"quantix_vhost_sha256": sha("SYNTHETIC")})
    assert not result["applied"] and state.guards == [False]
    assert [args[0] for args in state.calls] == ["openssl"]


def test_unchanged_certificate_does_not_reload(lineage):
    state, _, _ = lineage
    result = r.renew({"quantix_vhost_sha256": sha("SYNTHETIC")}, apply=True)
    assert result["status"] == "certificate_unchanged" and state.guards == [False]
    command = next(c for c in state.calls if c[0] == "certbot")
    assert command[command.index("--cert-name") + 1] == r.DOMAIN
    assert "--no-directory-hooks" in command and "--config" in command
    assert "--nginx" not in command and "--standalone" not in command
    assert "/etc/letsencrypt" not in " ".join(command)


def test_changed_certificate_uses_guarded_reload_only(lineage):
    state, _, _ = lineage
    state.changed = True
    result = r.renew({"quantix_vhost_sha256": sha("SYNTHETIC")}, apply=True)
    assert result["certificate_changed"] and state.guards == [False, True]


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("hook", "QUANTIX_RENEWAL_HOOKS_NOT_ALLOWED"),
        ("installer", "QUANTIX_RENEWAL_MUST_USE_WEBROOT"),
        ("outside_path", "QUANTIX_RENEWAL_PATH_NOT_ISOLATED"),
        ("outside_webroot", "QUANTIX_WEBROOT_NOT_ISOLATED"),
        ("staging_ca", "QUANTIX_RENEWAL_CA_NOT_PRODUCTION_LETSENCRYPT"),
        ("global_cli", "QUANTIX_CLI_CONFIG_MUST_BE_EMPTY"),
        ("other_san", "QUANTIX_CERTIFICATE_NAMES_NOT_EXCLUSIVE"),
    ],
)
def test_unsafe_renewal_configuration_never_calls_certbot(lineage, mutation, code):
    state, renewal, cli = lineage
    text = renewal.read_text()
    if mutation == "hook":
        text += "\ndeploy_hook = systemctl restart nginx\n"
    elif mutation == "installer":
        text += "\ninstaller = nginx\n"
    elif mutation == "outside_path":
        text = text.replace(
            str(r.FULLCHAIN), "/etc/letsencrypt/live/synthetic/fullchain.pem"
        )
    elif mutation == "outside_webroot":
        text = text.replace(str(r.WEBROOT), "/var/www/synthetic-other-app")
    elif mutation == "staging_ca":
        text = text.replace(
            "acme-v02.api.letsencrypt.org", "acme-staging-v02.api.letsencrypt.org"
        )
    elif mutation == "global_cli":
        cli.write_text("deploy-hook = systemctl restart nginx\n")
    elif mutation == "other_san":
        state.names = r.DOMAIN + ", DNS:synthetic-other.invalid"
    renewal.write_text(text)
    with pytest.raises(g.GuardError, match=code):
        r.renew({"quantix_vhost_sha256": sha("SYNTHETIC")}, apply=True)
    assert all(c[0] != "certbot" for c in state.calls) and state.guards == [False]


def test_certbot_failure_cannot_reload_or_expose_provider_text(lineage):
    state, _, _ = lineage
    state.failed = True
    with pytest.raises(g.GuardError, match="QUANTIX_CERTBOT_RENEWAL_FAILED") as exc:
        r.renew({"quantix_vhost_sha256": sha("SYNTHETIC")}, apply=True)
    assert state.guards == [False] and "secret-like" not in str(exc.value)


def test_renewed_certificate_pending_reload_survives_guard_failure(
    lineage, monkeypatch
):
    state, _, _ = lineage
    state.changed = True

    def blocked(baseline, checksum, *, apply=False):
        state.guards.append(apply)
        if apply:
            raise g.GuardError("PROTECTED_CONFIG_HASH_CHANGED")
        return {"applied": False}

    monkeypatch.setattr(r, "checked_reload", blocked)
    with pytest.raises(g.GuardError, match="PROTECTED_CONFIG_HASH_CHANGED"):
        r.renew({"quantix_vhost_sha256": sha("SYNTHETIC")}, apply=True)
    assert r.pending_hash() == r.certificate_hash()
    state.changed = False
    state.guards = []
    monkeypatch.setattr(
        r,
        "checked_reload",
        lambda b, c, apply=False: state.guards.append(apply) or {"applied": apply},
    )
    result = r.renew({"quantix_vhost_sha256": sha("SYNTHETIC")}, apply=True)
    assert state.guards == [False, True] and result["pending_reload_recovered"]
    assert not result["certificate_changed"] and not r.pending_path().exists()


def test_failed_certbot_after_file_change_retains_pending_evidence_without_reload(
    lineage,
):
    state, _, _ = lineage
    state.changed = True
    state.failed = True
    with pytest.raises(g.GuardError, match="QUANTIX_CERTBOT_RENEWAL_FAILED"):
        r.renew({"quantix_vhost_sha256": sha("SYNTHETIC")}, apply=True)
    assert state.guards == [False] and r.pending_hash() == r.certificate_hash()


def test_pending_certificate_hash_mismatch_fails_before_certbot(lineage):
    state, _, _ = lineage
    r.mark_pending(sha("SYNTHETIC unexpected material"))
    with pytest.raises(g.GuardError, match="PENDING_CERTIFICATE_HASH_MISMATCH"):
        r.renew({"quantix_vhost_sha256": sha("SYNTHETIC")}, apply=True)
    assert all(c[0] != "certbot" for c in state.calls)


def test_master_change_after_hup_is_an_explicit_failed_postcondition(
    environment, monkeypatch
):
    state, baseline = environment
    monkeypatch.setattr(g.os, "geteuid", lambda: 0)

    def signal_target(pid, sig):
        state.sent.append((pid, sig))
        state.master = 54321

    monkeypatch.setattr(g.os, "kill", signal_target)
    with pytest.raises(g.GuardError, match="NGINX_MASTER_CHANGED_AFTER_HUP") as exc:
        g.checked_reload(baseline, baseline["quantix_vhost_sha256"], apply=True)
    assert exc.value.reload_applied and state.sent == [(12345, signal.SIGHUP)]


def test_protected_bgm_cannot_pass_by_redirecting_to_owner_site(monkeypatch):
    class WrongSite:
        status = 200

        def geturl(self):
            return "https://raccncode.com/"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        g.urllib.request,
        "build_opener",
        lambda *args: SimpleNamespace(open=lambda *a, **k: WrongSite()),
    )
    with pytest.raises(g.GuardError, match="PROTECTED_HTTPS_NOT_HEALTHY"):
        g.protected_https()


def test_worker_generation_uses_stable_process_ticks_not_wall_clock_jitter(monkeypatch):
    fields = ["S", "12345"] + ["0"] * 17 + ["987654"]
    raw = "77 (nginx) " + " ".join(fields)
    monkeypatch.setattr(
        g, "run", lambda args: SimpleNamespace(returncode=0, stdout="77\n", stderr="")
    )
    monkeypatch.setattr(g, "Path", lambda *args: SimpleNamespace(read_text=lambda: raw))
    monkeypatch.setattr(
        g.time,
        "time",
        lambda: (_ for _ in ()).throw(AssertionError("No wall time for generation")),
    )
    assert g.worker_generation(12345) == 987654
    assert g.worker_generation(12345) == 987654


def test_world_writable_private_cli_is_rejected_even_if_empty(lineage):
    state, _, cli = lineage
    cli.chmod(0o666)
    with pytest.raises(g.GuardError, match="QUANTIX_TLS_CONFIG_PERMISSIONS_INVALID"):
        r.renew({"quantix_vhost_sha256": sha("SYNTHETIC")}, apply=True)
    assert state.guards == [False] and not state.calls


def test_newer_protected_certificate_or_key_aborts_before_hup(environment, monkeypatch):
    state, baseline = environment
    state.contents[OLD[1]] = (
        "server { ssl_certificate /synthetic/protected-cert.pem; ssl_certificate_key /synthetic/protected-key.pem; }"
    )
    baseline["loaded_file_sha256"][OLD[1]] = sha(state.contents[OLD[1]])
    monkeypatch.setattr(
        g, "tls_modified_at", lambda path: 1001 if "key" in path else 900
    )
    with pytest.raises(g.GuardError, match="PENDING_PROTECTED_TLS_MATERIAL"):
        g.checked_reload(baseline, baseline["quantix_vhost_sha256"], apply=True)
    assert state.sent == []


def test_old_literal_tls_metadata_passes_without_reading_key_contents(
    environment, monkeypatch
):
    state, baseline = environment
    state.contents[OLD[1]] = (
        'server { ssl_certificate\n "/synthetic/cert with spaces.pem"; ssl_certificate_key /synthetic/protected.key; }'
    )
    baseline["loaded_file_sha256"][OLD[1]] = sha(state.contents[OLD[1]])
    observed = []
    monkeypatch.setattr(g, "tls_modified_at", lambda path: observed.append(path) or 900)
    # The fixture digest accepts only .conf paths: hashing key bytes would fail this test.
    assert (
        g.checked_reload(baseline, baseline["quantix_vhost_sha256"])["status"]
        == "checks_passed"
    )
    assert set(observed) == {
        "/synthetic/cert with spaces.pem",
        "/synthetic/protected.key",
    }
    assert state.sent == []




