"""Testinfra tests for the anxs-unbound role.

The organising principle: a DNS resolver is verified by RESOLVING
something through it. Asserting that a forward-zone stanza is present in
a config file is the check that stays green while the resolver returns
nothing.

Fixtures come from molecule/default/prepare.yml (the stub resolver) and
molecule/default/converge.yml (the forward zone). Keep these constants in
sync with both.
"""

import re

# Zone forwarded to the stub, and the RFC1918 address the stub answers
# with. The address is in 192.168.0.0/16 on purpose: it is inside the
# role's default private-address list, so this only resolves if the
# forward zone is paired with a private-domain.
#
# example.com, NOT .invalid -- unbound ships built-in local-zones for
# every RFC 6761 special-use name and consults them BEFORE forward
# zones. See the long comment in prepare.yml.
ALLOWED_ZONE = "allowed.molecule.example.com"
ALLOWED_ANSWER = "192.168.77.7"

# Served by the same stub, but NOT forwarded. Proves the forward zone is
# scoped rather than global.
BLOCKED_ZONE = "blocked.molecule.example.com"
BLOCKED_ANSWER = "192.168.77.8"

# Forwarded, and sitting under `.test`, which IS one of unbound's
# built-in local zones. Built-ins are consulted BEFORE forward zones, so
# this only resolves because the role emits
# `local-zone: "shadowed.test." transparent`.
SHADOWED_ZONE = "shadowed.test"
# TEST-NET-3, NOT RFC1918, and that is the point: a private answer would also
# fail whenever the private-domain pairing broke, making this fixture a
# superset of `allowed` instead of an isolated test of whether
# `local-zone ... transparent` defeats the built-in `test.` zone.
# Must match stub_answer_shadowed in molecule/default/prepare.yml.
SHADOWED_ANSWER = "203.0.113.9"

FORWARDS_FILE = "/etc/unbound/unbound.conf.d/10-forwards.conf"
MAIN_CONFIG = "/etc/unbound/unbound.conf"

# Written by prepare.yml. The stub listens on the container's routable
# address rather than loopback, because the role defaults to
# `do-not-query-localhost: yes` and a stub on 127.0.0.1 could only be
# reached by overriding that -- which would test a configuration no real
# host runs.
STUB_ADDR_FILE = "/var/tmp/molecule-stub-addr"
STUB_PORT = 5353


def stub_address(host):
    """The address prepare.yml put the stub resolver on."""
    f = host.file(STUB_ADDR_FILE)
    assert f.exists, (
        f"HARNESS problem: {STUB_ADDR_FILE} is missing, so no test can ask "
        f"the stub anything directly. prepare.yml should have written it."
    )
    return f.content_string.strip()


def dig(host, name, rrtype="A", server="127.0.0.1", port=53):
    """Query a resolver and keep the response HEADER.

    Deliberately not `dig +short`, which prints an empty string for
    NXDOMAIN, NODATA and SERVFAIL alike. The header carries the status and
    the `aa` flag, which is what tells those three apart.
    """
    return host.run(
        "dig +nocmd +noall +comments +answer +time=2 +tries=2 @%s -p %s %s %s",
        server,
        str(port),
        name,
        rrtype,
    )


def dig_status(output):
    """Pull the rcode out of a dig response header."""
    match = re.search(r"status:\s*([A-Z]+)", output)
    return match.group(1) if match else "NO-STATUS-LINE"


def dig_authoritative(output):
    """True when the answer carries `aa`.

    An authoritative answer from a recursive resolver means the answer
    came from a local-zone, not from the network. That is the signature
    of a forward zone being shadowed.
    """
    match = re.search(r"^;; flags: ([a-z ]+);", output, re.MULTILINE)
    return bool(match) and "aa" in match.group(1).split()


def dig_report(result):
    """One-line diagnosis for an assertion message."""
    return (
        f"status={dig_status(result.stdout)} "
        f"authoritative={dig_authoritative(result.stdout)} "
        f"rc={result.rc} stdout={result.stdout!r} stderr={result.stderr!r}"
    )


# --- packaging and identity ----------------------------------------------


def test_unbound_package_installed(host):
    """Verify unbound is installed."""
    assert host.package("unbound").is_installed


def test_unbound_user_exists(host):
    """Verify the ROLE's user settings applied, not the package's.

    Debian's unbound.postinst already creates this user with shell
    /usr/sbin/nologin before the role's task runs. /bin/false is what the
    role sets and the package does not, so it is the one observable that
    distinguishes "the role ran" from "the package ran".
    """
    user = host.user("unbound")
    assert user.exists
    assert user.shell == "/bin/false", (
        f"unbound's shell is {user.shell!r}, which is the packaging default. "
        f"The role sets /bin/false, so this means its user task did not run."
    )
    assert user.home == "/var/lib/unbound"


def test_root_key_present(host):
    """Verify the DNSSEC trust anchor is in place."""
    f = host.file("/var/lib/unbound/root.key")
    assert f.exists
    assert f.user == "unbound"


def test_control_key_generated(host):
    """Verify unbound-control-setup ran.

    The role guards this with `creates:` rather than a separate stat
    task, so a missing key here means the guard points at the wrong path
    and the command is being skipped on a fresh host.
    """
    assert host.file("/etc/unbound/unbound_server.key").exists


# --- config shape (necessary, not sufficient) ----------------------------


def test_forwards_file_permissions(host):
    """Verify the forwards file is not group-writable.

    unbound never needs to write its own configuration.
    """
    f = host.file(FORWARDS_FILE)
    assert f.exists
    assert f.user == "root"
    assert f.group == "unbound"
    assert f.mode == 0o644


def test_installed_config_passes_checkconf(host):
    """Verify unbound-checkconf accepts the config as installed.

    The role validates each file at template time, on a temp copy. This
    checks the assembled result, includes resolved -- which on Debian and
    Ubuntu also pulls in the distribution's own drop-ins.
    """
    result = host.run("unbound-checkconf %s", MAIN_CONFIG)
    assert result.rc == 0, f"unbound-checkconf failed: {result.stderr}"


# --- the resolver actually resolves --------------------------------------


def test_unbound_service_running(host):
    """Verify unbound is running and enabled."""
    svc = host.service("unbound")
    assert svc.is_running
    assert svc.is_enabled


def test_unbound_listening_on_loopback(host):
    """Verify unbound is bound to 127.0.0.1:53."""
    assert host.socket("udp://127.0.0.1:53").is_listening
    assert host.socket("tcp://127.0.0.1:53").is_listening


def test_forward_zone_returns_the_rfc1918_answer(host):
    """Verify a forwarded internal zone resolves to its RFC1918 address.

    THE test. It fails if any of these is wrong, and every one of them
    leaves a config file that reads correctly:

      * the forward zone was never rendered or never loaded;
      * the forward zone is not paired with a private-domain, so
        private-address drops the answer -> NOERROR, zero answers,
        authoritative=False;
      * the zone name sits under one of unbound's built-in local-zones,
        which are consulted first -> NXDOMAIN, authoritative=True;
      * DNSSEC validation refuses a zone it cannot prove insecure ->
        SERVFAIL;
      * do-not-query-localhost or access-control blocks the query path.

    The status/authoritative pair in the failure message distinguishes
    them. Do not replace this with `dig +short`.
    """
    result = dig(host, f"host.{ALLOWED_ZONE}")
    assert ALLOWED_ANSWER in result.stdout, (
        f"unbound did not return {ALLOWED_ANSWER} for host.{ALLOWED_ZONE}. "
        f"{dig_report(result)}\n"
        f"  NXDOMAIN + authoritative=True -> a built-in local-zone is "
        f"shadowing the forward zone; the fixture name is under a "
        f"reserved TLD.\n"
        f"  NOERROR + zero answers -> private-address stripped an RFC1918 "
        f"record because the forward zone has no matching "
        f"private-domain.\n"
        f"  SERVFAIL -> DNSSEC; the zone needs unbound_insecure_domains."
    )


def test_cache_min_ttl_floors_zero_ttl_answers(host):
    """Verify the cache floor changes the real TTL-zero fixture's behaviour.

    dnsmasq's --address answers carry TTL 0, the same shape as the
    upstream resolvers this option exists for. Both queries must come back
    with a positive TTL, so a rendered-but-unused option fails here.
    """
    observed = []
    for _ in range(2):
        result = dig(host, f"host.{ALLOWED_ZONE}")
        match = re.search(
            rf"host[.]{re.escape(ALLOWED_ZONE)}[.]\s+(\d+)\s+IN\s+A\s+"
            rf"{re.escape(ALLOWED_ANSWER)}",
            result.stdout,
        )
        assert match, (
            f"could not read TTL from the forwarded answer. {dig_report(result)}"
        )
        observed.append(int(match.group(1)))

    assert all(1 <= ttl <= 30 for ttl in observed), (
        f"cache-min-ttl did not floor the TTL-zero fixture: observed {observed}"
    )


def test_forward_zone_under_a_builtin_local_zone_resolves(host):
    """Verify a forward zone under one of unbound's built-in local zones works.

    unbound ships default local zones -- localhost, the 127.0.0.1 and ::1
    reverse zones, home.arpa, resolver.arpa, service.arpa, onion, test,
    invalid, and the AS112 zones -- and consults them BEFORE forward
    zones. A forward zone whose name falls under one is answered
    authoritatively by unbound itself and the forwarder is never
    contacted. The role emits `local-zone: "<zone>." transparent` for
    every forward zone to prevent that.

    The AS112 set is why this matters outside a test: it covers the
    RFC1918 reverse zones, so forwarding 168.192.in-addr.arpa for
    internal PTR lookups hits this and nothing else in the config looks
    wrong.

    Failure signature without the transparent line: NXDOMAIN, `aa` set,
    AUTHORITY SOA naming `localhost.`.
    """
    result = dig(host, f"host.{SHADOWED_ZONE}")
    assert SHADOWED_ANSWER in result.stdout, (
        f"unbound did not return {SHADOWED_ANSWER} for "
        f"host.{SHADOWED_ZONE}. {dig_report(result)}\n"
        f"  NXDOMAIN + authoritative=True means the built-in local zone "
        f"for `test.` is still shadowing this forward zone -- check that "
        f"10-forwards.conf emits a `local-zone: ... transparent` line "
        f"per forward zone."
    )


def test_unforwarded_zone_does_not_reach_the_stub(host):
    """Verify the forward zone is scoped to the name it declares.

    The stub answers for this zone too, but the role was never told to
    forward it, so unbound must not return the stub's address.

    The negative assertion is last and deliberately not alone: on its own,
    `BLOCKED_ANSWER not in stdout` cannot fail, because a dead resolver or a
    missing dig also produces an empty string.

    The positive controls, in order:
      1. the stub really does serve this zone, asked directly -- so the
         fixture cannot be silently disarmed;
      2. unbound really did answer us, with a status we recognise -- so a
         dead resolver fails here instead of passing.

    Any rcode other than the stub's address is a pass. NXDOMAIN (recursion
    reached the real example.com) and SERVFAIL (no outbound DNS) both mean
    the query did not go to the stub; asserting one specific rcode would
    make this depend on whether CI can reach the root.
    """
    direct = dig(
        host,
        f"host.{BLOCKED_ZONE}",
        server=stub_address(host),
        port=STUB_PORT,
    )
    assert BLOCKED_ANSWER in direct.stdout, (
        f"HARNESS problem, not a role problem: the stub does not serve "
        f"{BLOCKED_ZONE}, so this test's trap is disarmed and it would pass "
        f"no matter what the role did. {dig_report(direct)}"
    )

    result = dig(host, f"host.{BLOCKED_ZONE}")
    assert result.rc == 0, (
        f"dig itself failed against unbound, so the negative assertion "
        f"below would pass on an empty string. {dig_report(result)}"
    )
    assert dig_status(result.stdout) in ("NXDOMAIN", "SERVFAIL", "NOERROR"), (
        f"unbound returned no recognisable status, so it is not answering "
        f"and this test cannot mean anything. {dig_report(result)}"
    )
    assert BLOCKED_ANSWER not in result.stdout, (
        f"unbound returned the stub's answer for {BLOCKED_ZONE}, which is "
        f"not in unbound_forward_zones. The forward zone is matching more "
        f"than it declares -- check that the forward-zone name in "
        f"10-forwards.conf has not been broadened to a parent. "
        f"{dig_report(result)}"
    )


def test_root_hints_directive_is_valid(host):
    """Verify the fetched root hints are real.

    The converge sets `unbound_root_hints_fetch` against a file:// URL, and
    every template carries `validate: unbound-checkconf`, which errors on an
    unknown directive -- so reaching this test at all proves the emitted
    keyword was accepted. What is left to check is the artifact.
    """
    hints = host.file("/etc/unbound/root.hints")
    assert hints.exists, "root hints were not fetched"
    assert "root-servers.net" in hints.content_string


def test_unbound_survives_a_reload(host):
    """Verify reload is a real operation and does not drop the resolver.

    The role notifies a handler that reloads rather than restarts, so if
    this unit has no working ExecReload every config change either fails
    the play or silently leaves the old config live.
    """
    reload_result = host.run("systemctl reload unbound")
    assert reload_result.rc == 0, (
        f"systemctl reload unbound failed, so the role's reload handler "
        f"cannot work: {reload_result.stderr}"
    )
    assert host.service("unbound").is_running
    after = dig(host, f"host.{ALLOWED_ZONE}")
    assert ALLOWED_ANSWER in after.stdout, (
        f"unbound stopped answering after a reload. {dig_report(after)}"
    )


# --- the system resolver -------------------------------------------------


def test_resolv_conf_points_at_unbound(host):
    """Verify /etc/resolv.conf is a real file naming the local resolver."""
    f = host.file("/etc/resolv.conf")
    assert f.exists
    assert not f.is_symlink, (
        "/etc/resolv.conf is a symlink; the role wrote through it instead "
        "of replacing it, so whatever owns the link target still wins"
    )
    assert f.mode == 0o644, "resolv.conf must not be group-writable"
    assert "nameserver 127.0.0.1" in f.content_string


def test_system_resolver_path_resolves_the_forward_zone(host):
    """Verify the whole path works, not just a direct query to unbound.

    getent goes through nsswitch and /etc/resolv.conf, which is what
    every program on the host actually does. A resolver that answers on
    127.0.0.1 while resolv.conf points somewhere else is a resolver
    nothing uses.
    """
    result = host.run("getent hosts %s", f"host.{ALLOWED_ZONE}")
    assert result.rc == 0, (
        f"getent could not resolve host.{ALLOWED_ZONE} through the system "
        f"resolver. Direct query said: "
        f"{dig_report(dig(host, f'host.{ALLOWED_ZONE}'))}"
    )
    assert ALLOWED_ANSWER in result.stdout


def test_unbound_resolvconf_helper_is_masked(host):
    """Verify the DHCP resolv.conf helper cannot fight the role.

    unbound-resolvconf rewrites /etc/resolv.conf from lease data. Left
    enabled, it undoes the role on the next lease renewal -- long after
    any play has finished reporting success.
    """
    result = host.run("systemctl is-enabled unbound-resolvconf")
    assert "masked" in result.stdout, (
        f"unbound-resolvconf is not masked: {result.stdout!r} "
        f"{result.stderr!r}"
    )
