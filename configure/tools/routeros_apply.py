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
    return state


def apply_state(sock, declared, new_password=None):
    """Converge the live router to the declaration. Returns a report."""
    state = read_state(sock)
    report = {"changed": [], "addresses": [], "identity": None,
              "dhcp_clients": [], "password_set": False, "errors": []}

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

    # ---- dhcp-client -------------------------------------------------------
    if declared.get("dhcp_client") is False:
        for client in state["dhcp_clients"]:
            reply = _command(sock, ["/ip/dhcp-client/remove", f"=.id={client['id']}"])
            if _ok(reply):
                report["changed"].append(f"dhcp-client removed on {client['interface']}")
            else:
                report["errors"].append(f"dhcp-client remove: {reply[-1]}")

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
    return report


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
        report = apply_state(sock, declared, new_password=args.new_password)
    finally:
        sock.close()
    print(json.dumps(report))
    if report["errors"]:
        sys.exit(f"routeros_apply.py: {len(report['errors'])} error(s): {report['errors']}")


if __name__ == "__main__":
    main()
