"""Cross-reference checks for the declared state itself.

The strict loader in `playbooks/tools/pve_read.py` refuses a key declared TWICE.
It cannot refuse a key that POINTS at something that does not exist, and that is
the failure this framework is most exposed to, because the manifest describes
several machines at once and their names cross over:

* a RouterOS address hangs off an interface whose name is declared in another
  block of the same entry (`net0` becomes `ether1`, and a bridge is addressed by
  its NAME, never by the word "bridge");
* a tunnel route names a next hop that a router declares further down the file;
* a DHCP reservation names a workstation declared above it;
* a guest appears in both `pve_containers` and `pve_config_targets`.

None of those is caught by the platform, and that is the expensive part.
RouterOS ACCEPTS an address on an interface name that does not exist and simply
does not create it: the API answers 200, the run reports success, and the router
silently lacks the address. The failure then surfaces two layers later, in a
connectivity test, looking like a network problem. Checking here turns a domain
error into a parse error, before a single API call.

Why a filter and not a check inside the reader: `pve_read.py` parses the
manifest RAW, so it sees `"{{ lab_bridge_name }}"` as a literal string and every
templated value becomes a false positive. Ansible resolves Jinja before it calls
a filter, so a filter sees what the run will actually act on. This is also the
pattern the repository already uses for logic reachable from Jinja
(`pve_declared_ipv4`, `pve_disk_specs`).
"""

import re

from ansible.errors import AnsibleFilterError

_HWADDR = re.compile(r"hwaddr=([0-9A-Fa-f:]{17})")
_NET_KEY = re.compile(r"^net\d+$")


def _ethers(spec):
    """The RouterOS interface names a guest's NICs become.

    PVE numbers a VM's NICs from 0 (`net0`, `net1`); RouterOS numbers its
    ethernet interfaces from 1 (`ether1`, `ether2`). The mapping is
    `netN -> ether(N+1)`, and it is the most load-bearing implicit convention in
    the declared state: every RouterOS address, bridge port and DHCP server hangs
    off one of these names.
    """
    net = spec.get("net") or {}
    if not isinstance(net, dict):
        return set()
    return {f"ether{i + 1}" for i, key in enumerate(sorted(k for k in net if _NET_KEY.match(str(k))))}


def pve_state_problems(state):
    """Return the list of dangling references inside the declared state.

    Takes a dict of the ALREADY RESOLVED state: `containers`, `vms`, `targets`,
    `bridges`, `tunnel`. An empty list means every reference resolves. Callers
    assert on its length, so a problem is a run that stops before it changes
    anything.
    """
    if not isinstance(state, dict):
        raise AnsibleFilterError("pve_state_problems expects a dict of declared state")

    containers = state.get("containers") or []
    vms = state.get("vms") or []
    targets = state.get("targets") or []
    bridges = state.get("bridges") or []
    tunnel = state.get("tunnel") or {}
    problems = []

    def err(msg):
        problems.append(msg)

    def who(entry):
        return entry.get("hostname") or entry.get("name") or f"vmid {entry.get('vmid')}"

    # ---- identities that must be unique across the whole declared state -----
    seen = {}
    for label, entries in (("pve_containers", containers), ("pve_vms", vms)):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            vmid = entry.get("vmid")
            if vmid in seen:
                err(f"vmid {vmid} is declared twice, in {seen[vmid]} and again in {label}. "
                    f"Two entries for one guest fight over it and the last one wins silently: "
                    f"delete the one you did not mean.")
            seen[vmid] = label

    container_ids = {c.get("vmid") for c in containers if isinstance(c, dict)}
    for t in targets:
        if isinstance(t, dict) and t.get("vmid") in container_ids:
            err(f"vmid {t.get('vmid')} is declared BOTH in pve_containers (which gives it its "
                f"shape, layer 1) and in pve_config_targets (which configures it from inside, "
                f"layer 2). A guest belongs in exactly one of them: move it into pve_containers "
                f"with its shape declared faithfully, or leave it as a target only.")

    names = [str(b.get("iface")) for b in bridges if isinstance(b, dict) and b.get("iface")]
    for name in sorted({n for n in names if names.count(n) > 1}):
        err(f"segment {name} is declared twice in pve_bridges.")

    # ---- a guest layer 2 must reach needs an address it can derive ---------
    for entry in list(containers) + list(vms):
        if not isinstance(entry, dict) or not entry.get("config") or entry.get("address"):
            continue
        # A RouterOS guest derives its address from its own declaration instead.
        if (entry.get("config") or {}).get("routeros"):
            continue
        netif = entry.get("netif") or entry.get("net") or {}
        if isinstance(netif, dict) and any("ip=dhcp" in str(v) for v in netif.values()):
            err(f"{who(entry)} declares a `config` block, so layer 2 has to reach it, but its "
                f"NIC takes its address by DHCP and no explicit `address:` is declared. Layer 2 "
                f"cannot derive where to reach it: declare a static IPv4 in its net0, or set an "
                f"`address:` on the entry.")

    # ---- RouterOS: every referenced interface must exist on that router ----
    for entry in vms:
        if not isinstance(entry, dict):
            continue
        ros = (entry.get("config") or {}).get("routeros")
        if not ros:
            continue
        ethers = _ethers(entry)
        declared_bridges = [str(b.get("name")) for b in (ros.get("bridges") or [])
                            if isinstance(b, dict) and b.get("name")]
        known = ethers | set(declared_bridges)

        def check_iface(iface, what):
            if iface not in known:
                err(f"{who(entry)}: {what} names interface {iface!r}, which this router does not "
                    f"have. Its NICs are {sorted(ethers) or '(none declared)'} and its bridges are "
                    f"{sorted(declared_bridges) or '(none declared)'}. A bridge is addressed by its "
                    f"NAME, not by the word 'bridge', and RouterOS would accept this and silently "
                    f"not create it.")

        for address in ros.get("addresses") or []:
            if isinstance(address, dict) and address.get("interface"):
                check_iface(address["interface"], "an address")
        for server in ros.get("dhcp_servers") or []:
            if isinstance(server, dict) and server.get("interface"):
                check_iface(server["interface"], "a DHCP server")
        for bridge in ros.get("bridges") or []:
            if not isinstance(bridge, dict):
                continue
            for port in bridge.get("ports") or []:
                if port not in ethers:
                    err(f"{who(entry)}: bridge {bridge.get('name')!r} lists port {port!r}, which is "
                        f"not one of its NICs ({sorted(ethers) or '(none declared)'}). The ports "
                        f"ARE the switch.")

    # ---- the tunnel must carry every network the endpoint is told to route -
    guest_network = [str(n) for n in (tunnel.get("guest_network") or [])]
    routes = tunnel.get("endpoint_routes") or []
    if routes and not guest_network:
        err("pve_tunnel.endpoint_routes is declared but pve_tunnel.guest_network is empty, so the "
            "control node would never send that traffic into the tunnel: the endpoint's route "
            "would exist and never be used.")
    declared_via = set()
    for entry in vms:
        ros = ((entry.get("config") or {}).get("routeros") or {}) if isinstance(entry, dict) else {}
        for address in ros.get("addresses") or []:
            if isinstance(address, dict) and address.get("address"):
                declared_via.add(str(address["address"]).split("/")[0])
    for route in routes:
        if not isinstance(route, dict):
            continue
        to, via = route.get("to"), route.get("via")
        if to and guest_network and str(to) not in guest_network:
            err(f"pve_tunnel.endpoint_routes routes {to} at the endpoint, but it is not in "
                f"pve_tunnel.guest_network ({guest_network}), so the control node never routes it "
                f"into the tunnel. Declare it in both, or neither.")
        if via and str(via) not in declared_via:
            err(f"pve_tunnel.endpoint_routes sends {to} via {via}, but no router in this state "
                f"declares that address as its own. Declared: {sorted(declared_via) or '(none)'}.")

    # ---- a DHCP reservation must match the machine it is reserved for ------
    by_address = {}
    for c in containers:
        if isinstance(c, dict) and c.get("address"):
            netif = c.get("netif") or {}
            macs = set()
            if isinstance(netif, dict):
                for value in netif.values():
                    macs.update(m.lower() for m in _HWADDR.findall(str(value)))
            by_address[str(c["address"])] = {"hostname": who(c), "macs": macs}
    for entry in vms:
        if not isinstance(entry, dict):
            continue
        ros = (entry.get("config") or {}).get("routeros") or {}
        for server in ros.get("dhcp_servers") or []:
            if not isinstance(server, dict):
                continue
            for lease in server.get("leases") or []:
                if not isinstance(lease, dict):
                    continue
                owner = by_address.get(str(lease.get("address")))
                if owner is None:
                    err(f"reservation {lease.get('address')} (MAC {lease.get('mac_address')}) is "
                        f"declared on the router, but no guest declares that `address:` — an "
                        f"address nothing will ever use.")
                elif owner["macs"] and str(lease.get("mac_address", "")).lower() not in owner["macs"]:
                    err(f"reservation {lease.get('address')} claims MAC {lease.get('mac_address')}, "
                        f"but {owner['hostname']} declares {sorted(owner['macs'])} on its NIC, so "
                        f"the reservation would never be taken.")
    return problems


class FilterModule(object):
    def filters(self):
        return {
            "pve_state_problems": pve_state_problems,
        }
