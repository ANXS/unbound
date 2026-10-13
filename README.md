# ANXS.unbound

[![Molecule CI](https://github.com/ANXS/unbound/actions/workflows/ci.yml/badge.svg)](https://github.com/ANXS/unbound/actions/workflows/ci.yml)

Install and configure the [unbound](https://nlnetlabs.nl/projects/unbound/)
DNS resolver, optionally forwarding named zones to other resolvers and
optionally pointing the host's own `/etc/resolv.conf` at it.

Defaults are deliberately conservative: unbound listens on `127.0.0.1`
only, accepts queries from `127.0.0.1/8` only, forwards nothing, and does
not touch `/etc/resolv.conf`. Every widening is opt-in.

## Requirements

- Debian 12 (bookworm) or 13 (trixie), or Ubuntu 20.04, 22.04 or 24.04
- ansible-core 2.17 or newer (the role uses
  `ansible.builtin.systemd_service`)
- systemd

## Role variables

### Listening and access

| Variable | Default | Purpose |
| --- | --- | --- |
| `unbound_interfaces` | `["127.0.0.1"]` | Addresses to listen on. `127.0.0.1` is always emitted regardless, because `unbound_resolv` points the host at it. |
| `unbound_acl` | `[{network: "127.0.0.1/8"}]` | `access-control` entries. Each is `{network, permission}`; `permission` defaults to `allow`. |
| `unbound_port` | `53` | Listening port. |
| `unbound_udp_listen` / `unbound_tcp_listen` | `true` / `true` | `do-udp` / `do-tcp`. |
| `unbound_ipv6` | `false` | `do-ip6`. |

### Forwarding

| Variable | Default | Purpose |
| --- | --- | --- |
| `unbound_forward_zones` | `[]` | Per-zone forwarding. Each entry is `{name, addresses}`; addresses accept `IP` or `IP@port`. Set back to `[]` to remove forwarding entirely. |
| `unbound_forward_default` | `false` | Forward the root zone, i.e. act as a caching stub rather than a recursive resolver. |
| `unbound_forward_default_addresses` | `[]` | Upstreams for the root zone. |

Every entry in `unbound_forward_zones` automatically gets two companion
lines. Neither is cosmetic, and both defend against the same shape of
failure: a forward zone that is present and valid and still returns
nothing.

- **`private-domain: <zone>`** — unbound's `private-address` rebind
  protection **drops RFC1918 answers** for any zone not declared
  `private-domain`, and an internal resolver is exactly the thing that
  answers with RFC1918 addresses. Symptom without it: `NOERROR` with
  zero answers.
- **`local-zone: "<zone>." transparent`** — unbound ships **built-in
  local zones** and consults them **before** forward zones, so a forward
  zone whose name falls under one is answered authoritatively by unbound
  itself and the forwarder is never contacted. Symptom without it:
  `NXDOMAIN` with `aa` set and an AUTHORITY SOA naming `localhost.`.

The built-ins, as of unbound 1.26, are `localhost`, the `127.0.0.1` and
`::1` reverse zones, `home.arpa`, `resolver.arpa`, `service.arpa`,
`onion`, `test`, `invalid`, and the AS112 zones. **The AS112 set covers
the RFC1918 reverse zones** — `10.in-addr.arpa`, `16.172.in-addr.arpa`
through `31.172.in-addr.arpa`, `168.192.in-addr.arpa` — so forwarding a
reverse zone so that internal PTR lookups work needs this as much as a
forward zone does.

`transparent` is emitted on the caller's own zone rather than `nodefault`
on the built-in, because `unbound.conf(5)` says *"Use nodefault if you
use exactly that zone, if you want to use a subzone, use transparent"* —
and `transparent` is additionally a no-op for a zone nothing shadows. So
there is no built-in list to keep in sync inside this role.

### DNSSEC

| Variable | Default | Purpose |
| --- | --- | --- |
| `unbound_insecure_domains` | `[]` | Zones to exempt from validation (`domain-insecure`). |
| `unbound_root_hints_fetch` | `false` | Download root hints and set `root-file`. |
| `unbound_root_hints_url` | `https://www.internic.net/domain/named.root` | Where to fetch them from. |

`unbound_insecure_domains` is needed when forwarding an internal zone
whose **parent is signed** but which carries no DS record of its own and
is served unsigned by the internal resolver. unbound cannot prove such a
zone insecure, so it returns SERVFAIL — with a forward zone that is
otherwise entirely correct. Do not reach for this on a zone that is
genuinely signed; there, the failure is the system working.

### The system resolver

| Variable | Default | Purpose |
| --- | --- | --- |
| `unbound_resolv` | `false` | Replace `/etc/resolv.conf` with `nameserver 127.0.0.1`. |
| `unbound_resolv_search` | `[]` | `search` domains for the generated file. **Set this if the host uses short names** — see below. |
| `unbound_service` | `true` | Enable, start and (with `unbound_resolv`) redirect. Set false to render configuration only. |

`unbound_resolv` replaces `/etc/resolv.conf` rather than writing through
it, so a systemd-resolved symlink is superseded rather than followed. The
role also masks `unbound-resolvconf`, which would otherwise rewrite the
file from DHCP lease data on the next renewal.

`unbound_resolv` **replaces** `/etc/resolv.conf`, so leaving
`unbound_resolv_search` empty discards whatever `search` domains DHCP or
cloud-init supplied. FQDNs keep resolving, so the symptom is that bare
hostnames stop working — which looks like an application bug, not a DNS
change. The role cannot infer the previous value; set it explicitly.

Ordering is deliberate: configuration is rendered, handlers are flushed,
unbound is **proved** alive on the far side of that flush, and only then is
`/etc/resolv.conf` repointed.

The proof matters more than the order. `service: started` is a no-op on a
host where unbound is already running, so on a config-change run it reports
health for the *previous* configuration — and `ExecReload` on Debian and
Ubuntu is a bare `kill -HUP`, which returns 0 the instant the signal lands
and says nothing about whether unbound survived re-reading its config. So
the role waits for the configured port and round-trips
`unbound-control status` *after* the flush. Without that, a config unbound
accepts at parse time but dies on at reload gives a green play and a host
pointed at a dead resolver.

Related: the preflight asserts at the top of `tasks/main.yml` refuse three
input combinations that pass `unbound-checkconf` and then break DNS anyway —
a non-53 `unbound_port` together with `unbound_resolv` (`resolv.conf` has no
syntax for a port), and a `forward-zone` with an empty address list (accepted
by the parser, SERVFAILs every query, with no fallback to recursion because
`forward-first` defaults to no). `unbound-checkconf` is a parser, not a
health check.

Note that `meta: flush_handlers` flushes every pending handler in the
play, not only this role's. That is the cost of guaranteeing the resolver
is live before the host is pointed at it.

### Other

| Variable | Default | Purpose |
| --- | --- | --- |
| `unbound_private_domains` | `[]` | Extra `private-domain` entries beyond the forward zones. |
| `unbound_verbosity` | `0` | `verbosity`. |
| `unbound_prefetch` | `false` | `prefetch`. |
| `unbound_rrset_roundrobin` | `false` | `rrset-roundrobin`. |
| `unbound_user` / `unbound_group` | `unbound` / `unbound` | Service account. |
| `unbound_config_directory` | `/etc/unbound` | Configuration root. |
| `unbound_directory` | `/var/lib/unbound` | unbound's working directory. |

## Example

A host that resolves an internal zone through an internal resolver and
uses unbound for everything else:

```yaml
- hosts: servers
  become: true
  roles:
    - role: ANXS.unbound
      vars:
        unbound_resolv: true
        unbound_forward_zones:
          - name: "internal.example.com"
            addresses:
              - "10.0.0.2"
        unbound_insecure_domains:
          - "internal.example.com"
```

## Configuration layout

`unbound.conf` contains only an `include:` of
`unbound.conf.d/*.conf`, so the distribution's own drop-ins keep working
and callers can add fragments the role knows nothing about. The role
renders `dns.conf`, `network.conf`, `server.conf` and — only when
forwarding is configured — `10-forwards.conf`.

Every rendered file passes through `validate: unbound-checkconf`, so a
configuration unbound cannot parse fails the task instead of being
installed and reloaded into a resolver that then will not start.

## Testing

```
make lint              # yamllint + ansible-lint, production profile
make test-ubuntu2404   # one OS target
make test-all          # the five-target matrix
make test-forwards     # forward-zone lifecycle scenario
make test              # all of the above
make test-keep         # converge only, container left up for diagnosis
```

Two Molecule scenarios:

- **`default`** — the five-target matrix. Stands up a dnsmasq stub
  answering an RFC1918 address for a forwarded zone and asserts unbound
  **resolves** it, both directly and through `getent`. Includes an
  idempotence step.
- **`forwards`** — one OS target. Adds forwarding, proves it answers,
  removes it, proves it stops answering, then restores it. No
  idempotence step: removing a file is a change on every run.

Two things a future contributor will otherwise rediscover the hard way:

1. **Two of the three fixture zones deliberately avoid reserved names,
   and the third deliberately uses one.** `allowed`/`blocked` live under
   `example.com` (RFC 2606) so they exercise plain forwarding;
   `shadowed.test` sits under the built-in `test.` zone specifically to
   prove the `local-zone ... transparent` emission works. Do not
   "simplify" the first two onto `.invalid` — they would then be testing
   the same thing as the third and nothing would cover plain forwarding.
2. **The forward-zone lifecycle test sets its variables as block vars,
   not `include_role` params.** Role params sit at precedence 20 and
   registered variables at 19, so a role that registers a result over one
   of its own input names is invisible to a test that passes the variable
   as a param — the test passes whether the bug is present or not.

## License

MIT
