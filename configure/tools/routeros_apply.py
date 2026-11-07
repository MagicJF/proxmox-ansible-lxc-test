#!/usr/bin/env python3
"""Apply the declared RouterOS state through the RouterOS API.

Why a tool instead of a collection: `community.routeros` needs `librouteros` on
the control node, and this repository keeps the control node's dependencies to
what Ansible itself needs. The RouterOS API protocol is small (length-prefixed
words over TCP/8728) and is implemented here with the standard library only —
the same reason `pve_read.py` talks to the Proxmox API itself instead of
shelling out to a CLI.

It is IDEMPOTENT and it verifies what it declares:

  * addresses: adds the declared ones that are missing, removes the ones that
    are NOT declared (never dynamic ones, which belong to a DHCP client);
  * dhcp-client: removed when the declaration says `dhcp_client: false`;
  * identity: set from the declaration;
  * password: set when `--new-password` is given and the login still uses the
    factory-empty one (the CHR ships with no password).

Nothing is echoed: the password is never printed, and the report contains only
addresses, the identity and the DHCP client state.

Usage (normally called by configure/roles/routeros, not by hand):
    routeros_apply.py --host 10.10.10.50 --declared '<json>' [--password X] [--new-password Y]
"""

import argparse
import hashlib
import json
import socket
import struct
import sys
import time

PORT = 8728


# --------------------------------------------------------------------------- #
# The API protocol: words are length-prefixed, a sentence ends with an empty
# word, and a reply is one or more sentences ending in !done / !trap / !fatal.
# --------------------------------------------------------------------------- #
def _encode(word):
    data = word.encode()
    length = len(data)
    if length < 0x80:
        head = bytes([length])
    elif length < 0x4000:
        head = bytes([(length >> 8) | 0x80, length & 0xFF])
    elif length < 0x200000:
        head = bytes([(length >> 16) | 0xC0, (length >> 8) & 0xFF, length & 0xFF])
    elif length < 0x10000000:
        head = bytes([(length >> 24) | 0xE0, (length >> 16) & 0xFF,
                      (length >> 8) & 0xFF, length & 0xFF])
    else:
        head = b"\xF0" + struct.pack(">I", length)
    return head + data


def _read_word(sock):
    first = sock.recv(1)
    if not first:
        return None
    length = first[0]
    if not length & 0x80:
        size = length
    elif length & 0xC0 == 0x80:
        size = ((length & 0x3F) << 8) + sock.recv(1)[0]
    elif length & 0xE0 == 0xC0:
        size = ((length & 0x1F) << 16) + struct.unpack(">H", sock.recv(2))[0]
    elif length & 0xF0 == 0xE0:
        size = ((length & 0x0F) << 24) + struct.unpack(">I", sock.recv(3) + b"\x00")[0]
    else:
        size = struct.unpack(">I", sock.recv(4))[0]
    return sock.recv(size).decode(errors="replace") if size else ""


def _read_sentence(sock):
    words = []
    while True:
        word = _read_word(sock)
        if word is None or word == "":
            return words
        words.append(word)


def _command(sock, words, timeout=10):
    """Send one command and read every sentence up to !done / !fatal.

    A failed command answers `!trap` and THEN `!done`, so stopping at the trap
    leaves that `!done` in the buffer and every later command reads the previous
    reply — which shows up as an empty, silently wrong verification.
    """
    sock.settimeout(timeout)
    for word in words:
        sock.sendall(_encode(word))
    sock.sendall(b"\x00")
    sentences = []
    while True:
        sentence = _read_sentence(sock)
        if not sentence:
            return sentences
        sentences.append(sentence)
        if sentence[0] in ("!done", "!fatal"):
            return sentences


def _flatten(sentences):
    return [word for sentence in sentences for word in sentence]


def _ok(sentences):
    return bool(sentences) and sentences[-1][0] == "!done"


def _run_script(sock, name, lines, timeout=8):
    """Create a script ON the router and run it there.

    The API has no `[find]`, so a stale script with the same name is removed by
    `.id` after a print. `run` is allowed to time out: it is the step that may
    take the address away, and the script keeps running on the router whatever
    happens to this connection.
    """
    for sentence in _command(sock, ["/system/script/print"]):
        if sentence[0] != "!re":
            continue
        item = dict(w[1:].split("=", 1) for w in sentence[1:] if w.startswith("="))
        if item.get("name") == name:
            _command(sock, ["/system/script/remove", f"=.id={item['.id']}"])
    _command(sock, ["/system/script/add", f"=name={name}",
                    "=source=" + "\n".join(lines)])
    try:
        _command(sock, ["/system/script/run", f"=number={name}"], timeout=timeout)
    except OSError:
        pass


def _detach_from_dhcp(sock, declared, reconnect, report):
    """Remove the DHCP client and install the declared addresses server-side.

    The obvious order cannot work over the API. A declared address is normally
    the very one the DHCP client holds — the bootstrap hands the router the
    address the declaration names — so adding it first answers
    `failure: already have such address` (verified on chr-c, `!trap`), and
    removing the client first drops the address this session runs over: the
    socket dies before the add is ever sent, and the router is left with no
    address at all. That is a one-way door — the API is gone, MAC-telnet did not
    interoperate with RouterOS 7.25, and the serial console cannot be driven
    from a script.

    A RouterOS script runs on the router, so the removal and the address land
    together whether or not this connection survives them. Afterwards the tool
    reconnects to the declared address and carries on.
    """
    lines = ["/ip/dhcp-client/remove [find]"]
    for address, interface in sorted({(a["address"], a["interface"])
                                      for a in declared.get("addresses", [])}):
        lines.append(f"/ip/address/add address={address} interface={interface}")
    _run_script(sock, "hermes-dhcp-detach", lines)
    report["changed"].append(
        "dhcp-client removed and the declared addresses installed (server-side script)")
    return reconnect()


def connect(host, user, password, timeout=10):
    """Log in, handling both the modern and the challenge/response flow."""
    sock = socket.socket()
    sock.settimeout(timeout)
    sock.connect((host, PORT))
    reply = _command(sock, ["/login", f"=name={user}", f"=password={password}"])
    if _ok(reply):
        challenge = [w for w in reply[-1] if w.startswith("=ret=")]
        if challenge:  # old-style login: answer with md5(challenge + password)
            digest = hashlib.md5((challenge[0][5:] + password).encode()).hexdigest()
            reply = _command(sock, ["/login", f"=name={user}", f"=response=00{digest}"])
    if not _ok(reply):
        raise RuntimeError(f"login failed: {reply[-1] if reply else 'no reply'}")
    return sock


# --------------------------------------------------------------------------- #
# Declared state -> RouterOS
# --------------------------------------------------------------------------- #
def read_state(sock):
    state = {"addresses": [], "identity": None, "dhcp_clients": [], "ports": {}}
    for sentence in _command(sock, ["/ip/address/print"]):
        if sentence[0] != "!re":
            continue
        item = dict(w[1:].split("=", 1) for w in sentence[1:] if w.startswith("="))
        state["addresses"].append({
            "id": item.get(".id"), "address": item.get("address"),
            "interface": item.get("interface"), "dynamic": item.get("dynamic") == "true",
        })
    for sentence in _command(sock, ["/system/identity/print"]):
        if sentence[0] == "!re":
            item = dict(w[1:].split("=", 1) for w in sentence[1:] if w.startswith("="))
            state["identity"] = item.get("name")
    for sentence in _command(sock, ["/ip/dhcp-client/print"]):
        if sentence[0] == "!re":
            item = dict(w[1:].split("=", 1) for w in sentence[1:] if w.startswith("="))
            state["dhcp_clients"].append({"id": item.get(".id"), "interface": item.get("interface")})
    for sentence in _command(sock, ["/port/print"]):
        if sentence[0] == "!re":
            item = dict(w[1:].split("=", 1) for w in sentence[1:] if w.startswith("="))
            state["ports"][item.get("name")] = item
    state["consoles"] = []
    for sentence in _command(sock, ["/system/console/print"]):
        if sentence[0] == "!re":
            item = dict(w[1:].split("=", 1) for w in sentence[1:] if w.startswith("="))
            state["consoles"].append(item)
    state["bridges"] = {}
    for sentence in _command(sock, ["/interface/bridge/print"]):
        if sentence[0] == "!re":
            item = dict(w[1:].split("=", 1) for w in sentence[1:] if w.startswith("="))
            state["bridges"][item.get("name")] = {"id": item.get(".id"), "ports": []}
    for sentence in _command(sock, ["/interface/bridge/port/print"]):
        if sentence[0] == "!re":
            item = dict(w[1:].split("=", 1) for w in sentence[1:] if w.startswith("="))
            bridge = state["bridges"].get(item.get("bridge"))
            if bridge is not None:
                bridge["ports"].append({"id": item.get(".id"), "interface": item.get("interface")})
    state["routes"] = []
    for sentence in _command(sock, ["/ip/route/print"]):
        if sentence[0] == "!re":
            item = dict(w[1:].split("=", 1) for w in sentence[1:] if w.startswith("="))
            state["routes"].append(item)
    return state


def apply_state(sock, declared, new_password=None, reconnect=None):
    """Converge the live router to the declaration.

    Returns `(report, sock)`. The socket can be a NEW one: dropping the DHCP
    client takes away the address this session runs over, so the caller gets the
    reconnected socket back instead of one that is known to be dead.
    """
    state = read_state(sock)
    report = {"changed": [], "addresses": [], "identity": None,
              "dhcp_clients": [], "password_set": False, "errors": []}

    # ---- bridges: L2 switching is declared, not assumed ---------------------
    # This runs BEFORE the addresses, because a layer-3 switch declares the
    # site's gateway address on the bridge itself and the interface has to exist
    # first. A router that only bridges declares no address on it at all.
    for bridge in declared.get("bridges", []):
        live = state["bridges"].get(bridge["name"])
        if live is None:
            reply = _command(sock, ["/interface/bridge/add", f"=name={bridge['name']}"])
            if not _ok(reply):
                report["errors"].append(f"bridge {bridge['name']}: {reply[-1]}")
                continue
            report["changed"].append(f"bridge +{bridge['name']}")
            live = {"id": None, "ports": []}
        wanted_ports = list(bridge.get("ports", []))
        live_ports = [p["interface"] for p in live["ports"]]
        for interface in wanted_ports:
            if interface not in live_ports:
                reply = _command(sock, ["/interface/bridge/port/add",
                                        f"=bridge={bridge['name']}", f"=interface={interface}"])
                if _ok(reply):
                    report["changed"].append(f"bridge {bridge['name']} port +{interface}")
                else:
                    report["errors"].append(
                        f"bridge {bridge['name']} port {interface}: {reply[-1]}")
        for port in live["ports"]:
            if port["interface"] not in wanted_ports:
                reply = _command(sock, ["/interface/bridge/port/remove", f"=.id={port['id']}"])
                if _ok(reply):
                    report["changed"].append(f"bridge {bridge['name']} port -{port['interface']}")
                else:
                    report["errors"].append(
                        f"bridge {bridge['name']} port remove: {reply[-1]}")

    # ---- dhcp-client and the address it holds: one server-side script -------
    # The order matters and it is not the obvious one; `_detach_from_dhcp` carries
    # the full account. It never runs once something has failed: taking the
    # address away is the one change that can strand a router, so it happens only
    # while the session is still known to be healthy, and `reconnect` must exist
    # (the caller's job) for the tool to find the router again afterwards.
    if (declared.get("dhcp_client") is False
            and state["dhcp_clients"]
            and not report["errors"]
            and reconnect is not None):
        sock = _detach_from_dhcp(sock, declared, reconnect, report)
        state = read_state(sock)

    # ---- addresses: add what is missing, remove what is not declared --------
    wanted = {(a["address"], a["interface"]) for a in declared.get("addresses", [])}
    live = {(a["address"], a["interface"]): a for a in state["addresses"] if not a["dynamic"]}
    for address, interface in sorted(wanted - set(live)):
        reply = _command(sock, ["/ip/address/add", f"=address={address}", f"=interface={interface}"])
        if _ok(reply):
            report["changed"].append(f"address +{address} {interface}")
        else:
            report["errors"].append(f"add {address} {interface}: {reply[-1]}")
    for key in sorted(set(live) - wanted):
        reply = _command(sock, ["/ip/address/remove", f"=.id={live[key]['id']}"])
        if _ok(reply):
            report["changed"].append(f"address -{key[0]} {key[1]}")
        else:
            report["errors"].append(f"remove {key[0]}: {reply[-1]}")

    # ---- identity ----------------------------------------------------------
    if declared.get("identity") and state["identity"] != declared["identity"]:
        reply = _command(sock, ["/system/identity/set", f"=name={declared['identity']}"])
        if _ok(reply):
            report["changed"].append(f"identity {declared['identity']}")
        else:
            report["errors"].append(f"identity: {reply[-1]}")

    # ---- serial ports: the baud rate is not cosmetic ------------------------
    # QEMU's emulated UART paces the guest's output at the port's baud rate, so
    # a console left at RouterOS's 9600 default moves about a kilobyte per
    # second — visibly sluggish next to a container's pty, which has no UART to
    # pace it. Raising it is the difference between "usable" and "painful".
    for port in declared.get("ports", []):
        live = state["ports"].get(port["name"])
        if live is None:
            report["errors"].append(f"port {port['name']} does not exist on the router")
            continue
        if str(live.get("baud-rate")) != str(port["baud_rate"]):
            # RouterOS refuses to touch a port a console is using ("Cannot
            # change port while in use!"), so the consoles attached to it are
            # detached for the duration of the change and put back afterwards.
            attached = [c for c in state["consoles"] if c.get("port") == port["name"]]
            for console in attached:
                _command(sock, ["/system/console/set",
                                f"=numbers={console.get('.id')}", "=disabled=yes"])
            reply = _command(sock, ["/port/set", f"=numbers={port['name']}",
                                    f"=baud-rate={port['baud_rate']}"])
            for console in attached:
                _command(sock, ["/system/console/set",
                                f"=numbers={console.get('.id')}", "=disabled=no"])
            if _ok(reply):
                report["changed"].append(
                    f"port {port['name']} baud-rate {live.get('baud-rate')} -> {port['baud_rate']}")
            else:
                report["errors"].append(f"port {port['name']}: {reply[-1]}")

    # ---- static routes ------------------------------------------------------
    # Only the STATIC ones are ours to manage: connected routes belong to the
    # interfaces and dynamic ones to whatever protocol produced them, so
    # deleting either would fight the router instead of converging it.
    wanted_routes = {(r["to"], r.get("via")) for r in declared.get("routes", [])}
    live_routes = {(r.get("dst-address"), r.get("gateway")): r
                   for r in state["routes"] if r.get("static") == "true"}
    for to, via in sorted(wanted_routes - set(live_routes)):
        reply = _command(sock, ["/ip/route/add", f"=dst-address={to}", f"=gateway={via}"])
        if _ok(reply):
            report["changed"].append(f"route +{to} via {via}")
        else:
            report["errors"].append(f"route {to} via {via}: {reply[-1]}")
    for key in sorted(set(live_routes) - wanted_routes):
        reply = _command(sock, ["/ip/route/remove", f"=.id={live_routes[key]['.id']}"])
        if _ok(reply):
            report["changed"].append(f"route -{key[0]} via {key[1]}")
        else:
            report["errors"].append(f"route remove {key[0]}: {reply[-1]}")

    # ---- password (only when the router still uses the factory-empty one) ---
    if new_password:
        reply = _command(sock, ["/user/set", "=numbers=0", f"=password={new_password}"])
        if _ok(reply):
            report["password_set"] = True
            report["changed"].append("admin password set")
        else:
            report["errors"].append(f"password: {reply[-1]}")

    # ---- verify by re-reading ---------------------------------------------
    after = read_state(sock)
    report["addresses"] = [f"{a['address']} {a['interface']}" for a in after["addresses"]]
    report["identity"] = after["identity"]
    report["dhcp_clients"] = [c["interface"] for c in after["dhcp_clients"]]
    report["ports"] = {name: p.get("baud-rate") for name, p in after["ports"].items()}
    report["bridges"] = {name: sorted(p["interface"] for p in b["ports"])
                         for name, b in after["bridges"].items()}
    report["routes"] = sorted(f"{r.get('dst-address')} via {r.get('gateway')}"
                              for r in after["routes"] if r.get("static") == "true")
    for bridge in declared.get("bridges", []):
        live_bridge = after["bridges"].get(bridge["name"])
        if live_bridge is None:
            report["errors"].append(f"bridge {bridge['name']} still missing")
            continue
        live_ports = sorted(p["interface"] for p in live_bridge["ports"])
        if live_ports != sorted(bridge.get("ports", [])):
            report["errors"].append(
                f"bridge {bridge['name']} ports are {live_ports}, "
                f"declared {sorted(bridge.get('ports', []))}")
    live_routes_after = {(r.get("dst-address"), r.get("gateway"))
                         for r in after["routes"] if r.get("static") == "true"}
    for to, via in sorted(wanted_routes - live_routes_after):
        report["errors"].append(f"route {to} via {via} still missing")
    for port in declared.get("ports", []):
        if str(after["ports"].get(port["name"], {}).get("baud-rate")) != str(port["baud_rate"]):
            report["errors"].append(
                f"port {port['name']} still at {after['ports'].get(port['name'], {}).get('baud-rate')}, "
                f"declared {port['baud_rate']}")
    missing = wanted - {(a["address"], a["interface"]) for a in after["addresses"]}
    if missing:
        report["errors"].append(f"declared addresses still missing: {sorted(missing)}")
    if declared.get("dhcp_client") is False and after["dhcp_clients"]:
        report["errors"].append(f"dhcp client still present: {report['dhcp_clients']}")
    return report, sock


def main():
    ap = argparse.ArgumentParser(description="Converge a RouterOS device to the declared state.")
    ap.add_argument("--host", required=True)
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="")
    ap.add_argument("--new-password", default=None,
                    help="set the admin password (used once, on the factory-empty login)")
    ap.add_argument("--declared", default="{}", help="JSON with identity/addresses/dhcp_client")
    ap.add_argument("--phase", default="apply", choices=["apply", "read"])
    args = ap.parse_args()

    def reconnect():
        """A fresh session at the same address, waiting for the router to have it.

        The router is briefly without any address while its own script runs, so
        one attempt is not enough. NOTE: this only works because the script puts
        the declared address back; it is not a general retry.
        """
        last = None
        for _ in range(20):
            try:
                return connect(args.host, args.user, args.password)
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(2)
        sys.exit(f"routeros_apply.py: {args.host} did not answer again after the "
                 f"change: {type(last).__name__}: {last}")

    declared = json.loads(args.declared)
    try:
        sock = connect(args.host, args.user, args.password)
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"routeros_apply.py: cannot reach {args.host}: {type(exc).__name__}: {exc}")
    try:
        if args.phase == "read":
            state = read_state(sock)
            print(json.dumps({"identity": state["identity"],
                              "addresses": [f"{a['address']} {a['interface']}" for a in state["addresses"]],
                              "dhcp_clients": [c["interface"] for c in state["dhcp_clients"]]}))
            return
        report, sock = apply_state(sock, declared, new_password=args.new_password,
                                   reconnect=reconnect)
    finally:
        sock.close()
    print(json.dumps(report))
    if report["errors"]:
        sys.exit(f"routeros_apply.py: {len(report['errors'])} error(s): {report['errors']}")


if __name__ == "__main__":
    main()
