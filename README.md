# Proxmox LXC via Ansible

Declarative IaC for the LXC containers and QEMU VMs on Proxmox VE, driven entirely over the HTTPS API. No playbook connects to the Proxmox host over SSH.

## Structure

```text
inventory/group_vars/all.yml           # THE desired state — the only file that changes per request
ansible.cfg                            # pipelining, SSH reuse, forks, plugin and log paths
.ansible-lint                          # the lint rules that are enforced, and why the rest are not
playbooks/preflight.yml                # read-only: platform checks + declared-vs-live report
playbooks/apply.yml                    # converges every declared guest, then verifies via the API
playbooks/connect.yml                  # converges the control node's PATH into the guest LAN
playbooks/remove.yml                   # the deliberate destroy, refused while a guest is declared
playbooks/pve_engine.j2                # engine: declared-vs-live analysis, as Jinja macros
playbooks/tasks/                       # engine: shared steps (segments, disk import, state validation)
playbooks/tools/pve_read.py            # engine: reads every guest + platform facts in ONE process
playbooks/tools/framework_check.py     # the framework's own test: convergent, and sees drift
playbooks/tools/wg_endpoint_setup.sh   # layer 2 path: the endpoint bootstrap, run once on the PVE host
playbooks/tracing/sitecustomize.py     # engine: journals every PVE HTTP call
filter_plugins/pve.py                  # engine: canonicalisation filters used by the macros
filter_plugins/state.py                # engine: cross-reference checks on the declared state
configure/audit.yml                    # layer 2: read-only inspection of a guest, changes nothing
configure/site.yml                     # layer 2: converges the inside of managed guests
configure/tasks/                       # layer 2: shared target building, access policy + the path
configure/roles/<app>/                 # layer 2: one role per application, or per kind of guest
logs/                                  # run logs and the API journal (gitignored, kept via .gitkeep)
```

Layer 1 is a fixed engine with a handful of verbs — `preflight` (read-only), `apply` (converge), `connect` (the path) and `remove` (the deliberate destroy) — all reading the same declared state. A change request is an edit to `inventory/group_vars/all.yml` on a new branch, not a new playbook: adding a playbook per change would scatter the real state across files that overwrite each other. `connect` owns the one thing that is not a guest, the control node's network path to the guest LAN (see [The path layer 2 runs on](#the-path-layer-2-runs-on)); `remove` owns the one thing the engine will not do on its own (see [Adding and removing a guest](#adding-and-removing-a-guest)).

`group_vars/all.yml` is a group_vars *file*, so its variables sit at the top level; nesting them under an `all:` key just defines one variable named `all` and breaks every reference.

## Requirements

- Ansible Core 2.17+.
- `community.proxmox` collection 2.1.0 (install with `ansible-galaxy collection install -r requirements.yml`).
- Python dependencies required by the collection: `proxmoxer >= 2.3` and `requests` (install with `python3 -m pip install -r requirements.txt`).
- A Proxmox API token, available only in the local environment or Ansible Vault; never commit it.
- A trusted certificate/CA file for the PVE API. Set `PROXMOX_CA_PATH` to that local file and `PROXMOX_HOST` to a host/IP present in the certificate SAN.

## Credentials

Two sources, in this order of precedence:

1. **`playbooks/vars/secrets.yml`** — hardcoded values. Both playbooks load it in `pre_tasks` when the file exists, so no environment set-up is needed at all. **It is gitignored on purpose**: this repository is public and the PVE token is root-equivalent with no expiry. The file is created mode `0600`. Do not commit it, and do not copy its contents into any tracked file, issue or commit message.
2. **`PROXMOX_*` environment variables** — the fallback, used when the file is absent. This is the path to prefer once the temporary hardcoding is removed.

To go back to environment-only credentials, delete `playbooks/vars/secrets.yml`; nothing else needs changing.

Never commit these values. If the token ever reaches a tracked file or the remote, rotate it immediately (the engine reads whatever is in `secrets.yml`, so rotating is a one-line edit).

### Environment variables (fallback path)

```text
PROXMOX_HOST=<PVE API hostname or SAN-matching IP>
PROXMOX_TOKEN_ID=<user@realm!token-name>
PROXMOX_TOKEN_SECRET=<token secret>
PROXMOX_CA_PATH=<path to trusted PVE certificate/CA>
```

For this lab, the certificate includes the PVE public IP as a SAN; the LAN address `10.10.10.1` is not in that certificate, so use a SAN-matching name/IP or replace the certificate with one covering the LAN name.

## Run

```bash
python3 -m pip install -r requirements.txt
ansible-galaxy collection install -r requirements.yml
ansible-playbook --syntax-check playbooks/preflight.yml
ansible-playbook --syntax-check playbooks/apply.yml
ansible-playbook playbooks/preflight.yml   # read-only: checks + what would change
ansible-playbook playbooks/apply.yml       # converge + verify
ansible-playbook playbooks/connect.yml      # converge the path layer 2 needs (usually automatic)
```

For interactive iteration, `apply` alone is the verb: it reads the declared state, applies
what differs, then re-reads and verifies by API in the same run. Run `preflight` when you
suspect drift, not as a routine step — see [Performance and how to iterate fast](#performance-and-how-to-iterate-fast).

## Declared state

`inventory/group_vars/all.yml` holds `pve_node`, a `pve_defaults` block, and `pve_containers`: **the only containers this repository manages**. A container absent from that list is never read, created or modified — the pre-existing guests `100` and `105` are deliberately out of scope. Every key of `pve_defaults` can be overridden per container.

| vmid | hostname | cores | memory | swap | rootfs | status | what it is |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 167 | `ct-forge-5f2a` | 5 | 2560 MiB | 896 MiB | 7 GiB | started | pre-existing, adopted. On the **public** bridge, no layer 2 |
| 131 | `wiki-tiddly` | 1 | 1024 MiB | 512 MiB | 6 GiB | started | the worked TiddlyWiki example below |
| 141 | `ws1` | 1 | 512 MiB | 256 MiB | 6 GiB | started | workstation 1 of the connectivity lab |
| 142 | `ws2` | 1 | 512 MiB | 256 MiB | 6 GiB | started | workstation 2 of the connectivity lab |
| 143 | `lab-bootstrap` | 1 | 1024 MiB | 256 MiB | 4 GiB | started | the router's first address |

`pve_config_targets` is a separate, shorter list: guests whose **shape** this repository does not manage but whose **inside** it does. `101` (`caddy`, `10.10.10.2`) sits there with `manage: false`, so layer 2 inspects it and changes nothing. A guest belongs in one list or the other and never both — the state validator refuses an entry that is in the two.

Declared per container, inherited from `pve_defaults` unless overridden:

| Property | Declared as | Converged by `apply.yml` |
| --- | --- | --- |
| `vmid`, `hostname` | identity | no — changing them means "recreate" |
| `ostype` | `debian` | no — creation-time |
| `storage`, `rootfs_size`, `template`, `template_storage` | `local-zfs`, 4-8 GiB, Debian 13 | no — creation-time, rootfs growth is irreversible |
| `unprivileged` | `true` | no — creation-time |
| `cores`, `memory`, `swap`, `cpuunits` | 1-5, 512-2560 MiB, 256-896 MiB, 1024 | yes |
| `onboot` | `false` | yes |
| `status` | `stopped` by default, `started` per guest | yes — the container is stopped/started to match |
| `cmode` | `shell` | yes |
| `timezone` | `host` | yes |
| `description` | see file | yes |
| `tags` | `[ansible-managed, lab]`, plus per-guest tags | yes |
| `features`, `startup` | `[]` (must not exist) | yes — deleted if present |
| `netif` | `{}` by default; declared per guest | yes — declared interfaces are created, undeclared ones are deleted |
| `mounts`, `mount_devices` | `{}`, `[]` (must not exist) | yes — deleted if present |
| `nameserver`, `searchdomain`, `hookscript` | `null` | yes — deleted if present |

**Absence is state.** A setting declared as `null`/empty must not exist in the container, and the engine deletes it if it is there. That is how a hand-added interface, mount or `features=` line gets reverted.

### Adding and removing a guest

Adding one is: write the entry, run `apply` (shape) and then `configure/site.yml` (inside). Removing one is **two steps in this order**, because the engine never destroys anything:

1. **Un-declare it** — delete the whole entry from `pve_containers` (or `pve_vms`) on a branch, commit and push. The next `apply` stops managing it; the container itself keeps running, untouched.
2. **Destroy it** deliberately — PVE UI → *More → Destroy*, `pct destroy <vmid>` on the host, or `ansible-playbook playbooks/remove.yml -e hostname=<name> -e confirm=<name>`. `remove.yml` refuses while the guest is still declared, which is exactly why the order is not a preference.

Doing it the other way round is how a deleted guest comes back: destroy a guest that is still declared and the next `apply` recreates it.

**Delete the whole entry, or none of it.** YAML keeps the *last* value of a duplicated key without saying so, so an entry whose identity lines were removed while its `manage:`/`config:` block stayed does not disappear — it silently attaches to the entry above and reconfigures **that** guest (a wiki guest inheriting an `apache:` block, say). Three guards make that a loud failure instead of a silent one:

- the reader parses the declared state with a **duplicate-key-rejecting loader** and stops with the key and line number;
- every entry must carry its identity (`vmid` plus `hostname`/`name`), or the run refuses;
- the **state validator** (`filter_plugins/state.py`) refuses a state whose references do not resolve — an interface the router does not have, an endpoint route the tunnel does not carry, a reservation for a machine nobody declared — before either verb reads or writes anything. See [Checking the framework itself](#checking-the-framework-itself).

Never leave a guest entry commented out as a scratchpad: comment it out *entirely* or delete it, so the file never holds half an entry.

### Not declarable here

These are deliberately out of the file, not forgotten:

- `password` — a creation-time secret; putting it in Git is the one thing worse than an unsaved secret. Inject it from a vault at creation time if ever needed.
- `pubkey` — applied by PVE only at creation, not forwarded on update.
- `clone` / `clone_type` — an alternative creation path, unused.
- `pool` — no pool exists on this node.
- `force`, `purge`, `destroy_unreferenced_disks` — destructive control flags; the engine never sets them.
- raw `lxc.*` config keys (cgroup limits, bind mounts) — not exposed by the collection's module.
- `cpus` (CPU limit) — the module maps it to `cpuunits` on creation but to `cpulimit` on update, so it cannot hold one meaning; declare `cpuunits` instead.

## QEMU VMs

`pve_vm_defaults` + `pve_vms` declare VMs the same way, with one structural difference: **`proxmox_kvm` refuses to update `net`, `scsi`, `ide`, `sata`, `virtio`, `efidisk0` and `tpmstate0`** — updating them can regenerate a MAC or recreate a disk and lose data. So for a VM the network, the disks and the CD-ROM are *creation-time* properties, exactly like `bios`, `machine`, `scsihw` and `ostype`: drift in any of them makes `apply.yml` fail for that VM with an explicit message instead of converging.

Converged in place for VMs: `cores`, `sockets`, `memory`, `cpu`, `vga`, `boot`, `onboot`, `description`, `tags`, and the power state.

| VM | vmid | shape | role |
| --- | --- | --- | --- |
| `lab-chr` | 140 | 2 vCPU, 512 MiB, `pc`/SeaBIOS (the vendor image is i440fx-era, not `q35`), three virtio NICs (`vmbr1` plus one per lab cable), the imported CHR image as `scsi0`, serial console | the switch of the connectivity lab |
| `pve-nested` | 120 | 4 vCPU x 1 socket, 4096 MiB, 32 GiB disk on `local-zfs`, `q35`/SeaBIOS, one virtio NIC on `vmbr1`, CD-ROM on `proxmox-ve_9.2-1.iso` | a nested Proxmox VE — **commented out** in `pve_vms`, kept as the template for a VM that boots an installer ISO |

Note the bridge: `vmbr1` is the internal LAN (`10.10.10.0/24`) shared with every other lab guest, while `vmbr0` is the **public** bridge (`54.36.168.83/24` on `eno3`). Attaching a lab guest to `vmbr0` exposes it to the internet.

`preflight.yml` additionally asserts that every ISO a VM declares is actually present on the node before anything is created. A VM that imports a **vendor image** instead of building a disk from a size is a special case: PVE accepts `import-from` only at creation time, so `playbooks/tasks/disk_import.yml` grows the imported volume afterwards, and `apply` refuses loudly rather than pretending it can attach one later.

The nested PVE installation is **not** automated: `preflight`/`apply` would leave that VM booting the installer from the CD-ROM (declared `boot: order=ide2;scsi0`), and the installation is finished by hand from the noVNC console in the PVE UI. Nothing in this repository claims such a guest is installed.

## Layer 2 — configuring the INSIDE of a guest

Layer 1 provisions a guest through the Proxmox API. That API cannot run anything inside a guest: `POST /nodes/{node}/lxc/{vmid}/exec` answers **501 Method not implemented**. So a Caddyfile, a systemd unit or a cron job is out of layer 1's reach by construction, and layer 2 exists for exactly that boundary: it reaches the guest over SSH and converges what runs inside it.

The methodology, in one line per rule:

- **One declared state, two layers.** `inventory/group_vars/all.yml` stays the single source of truth: layer 1 reads the guest's shape, layer 2 reads the guest's `config:` block. Addresses are never duplicated — layer 2 derives the SSH target from the interface declaration with the `pve_declared_ipv4` filter, or from the guest's own `address:` when its NIC is DHCP.
- **A guest is configured over the protocol it speaks.** Most guests are Linux, so layer 2 reaches them over SSH. A RouterOS guest is not a POSIX shell — SSH lands in its own CLI — so it declares `config.routeros` and is converged over its **REST API** with `ansible.builtin.uri`, from the control node, with no SSH involved. The target's `kind` (`ssh` or `api`) is derived from which block the guest declares, and each kind gets its own play, because the two cannot share a connection.
- **A guest may have no `config:` at all.** `ws1` and `ws2` in the lab below declare only a shape: they are PCs whose inside nobody configures, so they are not layer-2 targets. What they need — an address — comes from the router, and the router declares it.
- **Read before you write.** `configure/audit.yml` inspects a guest and changes nothing. Run it before declaring `manage: true` on a guest that already exists.
- **Convergence is opt-in per guest.** A `config:` block plus `manage: true` means layer 2 owns the inside; `manage: false` (or absent) means audit only. A guest is never configured by accident.
- **Each layer verifies what it declares.** Layer 1 re-reads through the API. Layer 2 validates inside the guest (`caddy validate`, `systemctl is-active`, the timers) *and* from outside (an HTTP request asserting the declared status and body).
- **Access has a home.** Layer 1 injects the automation key **at birth** (`pubkey` in `pve_defaults` — PVE writes it into `/root/.ssh/authorized_keys` at creation and never forwards it on update). For a guest that already exists, the key is granted once from the PVE console and the run tells you the exact command, with the public key in the message.
- **So does the path.** A key authorises a connection that already arrived. If the control node is not attached to the guest LAN, every guest looks unreachable and no key helps, so the route is declared (`pve_tunnel`) and converged — automatically, from inside this verb, when no guest answers on :22.

```bash
ansible-playbook configure/audit.yml   # read-only: what is inside every target
ansible-playbook configure/site.yml    # converge every guest declared manage: true
```

### The path layer 2 runs on

The PVE API answers on the public address from anywhere; the guests sit on an internal bridge. A control node outside that bridge therefore reaches **no** guest at all — which looks like an authentication failure and is really a missing route. The framework anticipates it: `pve_tunnel` declares the path, and layer 2 converges it before it needs it.

`pve_tunnel` describes it in the one declared-state file: interface name, `endpoint` (`host:port`), the endpoint's **public** key, this node's tunnel address, the guest prefix to route through it, and where the private key lives.

The tunnel is to the **host**, not to each guest: one peer with `AllowedIPs = <guest LAN>` covers the whole network, so a container created tomorrow is reachable the moment it exists and nothing is configured per guest. What makes the LAN reachable is the host forwarding and NATting the tunnel subnet onto the guest bridge.

A network that lives **behind a router inside the lab** needs one thing more. The endpoint can reach the router — it is on the guest bridge — but has no idea where the networks behind it are, so without a route the control node reaches the router and nothing past it. `pve_tunnel.endpoint_routes` declares those routes, and `wg_endpoint_setup.sh` takes them as its last argument so they live and die with the tunnel rather than being a separate thing to keep in sync. The state validator refuses a route that is not also in `guest_network`: the endpoint would hold it and the control node would never send that traffic into the tunnel at all.

One thing is deliberately *not* automated, and it must stay that way: the endpoint lives on the Proxmox host, and the only API-only way to run code there would be a `hookscript` (root on the hypervisor, and this node's storages do not even offer the required `snippets` content type). The API administers guests; it does not get a backdoor to root on the host. So the endpoint is the one versioned, idempotent paste an operator performs per lab — see below.

```bash
ansible-playbook playbooks/connect.yml   # converge the path on its own
```

`configure/site.yml` runs the same task file (`configure/tasks/tunnel.yml`) automatically when its probe finds no target answering on :22, so a normal run self-heals: detect the missing route → bring the interface up → wait for the handshake → prove the guest LAN answers → converge the guests. If the path is already there, nothing is touched.

Keys follow the SSH convention: the private half is a file outside the repository (generated on the first run when absent, never printed), only the public half travels, and `connect.yml` prints it.

One half cannot be automated from a control node, and the framework says so instead of pretending: the **endpoint** lives on the Proxmox host, which the API cannot configure (`exec` answers 501). `playbooks/tools/wg_endpoint_setup.sh` bootstraps it once and idempotently, keyed by the public key the framework prints, and returns the endpoint's public key for `pve_tunnel.endpoint_public_key`.

`preflight.yml` reports the path — `up` or `DOWN`, and how many guests answer on :22 — without changing anything, so a layer-2 problem is visible before you run the configure verb.

What the second verb does, per guest, is decided by the declaration:

```yaml
  - vmid: 121
    hostname: caddy-lab
    cores: 1
    memory: 512
    netif:
      net0: "name=eth0,bridge=vmbr1,gw=10.10.10.1,ip=10.10.10.21/24,ip6=auto"
    manage: true                      # layer 2 may converge this guest
    config:
      caddy:
        sites:
          - address: ":80"
            respond: "hola desde la capa 2 - caddy-lab"
            expect_body: "hola desde la capa 2 - caddy-lab"
          - address: ":8080"
            root: /srv/www
            file_server: true
            index_html: "contenido declarado en group_vars"
            expect_body: "contenido declarado"
        timers:
          - name: caddy-validate
            on_calendar: "daily"
            exec_start: "/usr/bin/caddy validate --config /etc/caddy/Caddyfile"
```

The role installs Caddy from the official apt repository (key + deb822 source, no `curl | bash`), renders the Caddyfile from those sites with `validate:` so a broken file can never reach the running service, writes declared index content, declares periodic jobs as **systemd timers** rather than crontabs, and then proves it: `caddy validate`, `systemctl is-active`, every declared timer active, and an HTTP probe from the control node asserting the declared status and body.

Adding another application means another role under `configure/roles/` and another block under `config:` — not another repository, and not a `curl | bash` script nobody can diff.

### A worked example: a TiddlyWiki server

`pve_containers` declares VMID 131 `wiki-tiddly` (1 vCPU / 1024 MiB / 6 GiB, static
`10.10.10.31/24` on the LAN bridge, `manage: true`) with:

```yaml
    config:
      tiddlywiki:
        wiki_dir: /srv/tiddlywiki
        port: 8080
        version: "5.4.1"
        service_user: tiddlywiki
        listen_host: "0.0.0.0"
      http:
        - address: ":8080"
          expect_status: 200
          expect_body: "TiddlyWiki"
```

`apply` creates the guest (which is born SSH-reachable through `pubkey`), and
`configure/site.yml` installs Node.js, installs the pinned engine, runs
`tiddlywiki /srv/tiddlywiki --init server` once, declares
`tiddlywiki.service` (`Restart=always`, `enabled`), and then proves it: the live
process must be *this* wiki folder on *this* port, the guest must answer HTTP 200,
`anonymous` and the control node must also get HTTP 200 with the declared body.
A role is included only when the guest declares its config key, so a managed guest
that declares no `caddy` block never gets Caddy.

Two decisions worth knowing:

- **The engine is installed from the official npm tarball, not through Debian's
  `npm` package**, because `apt install npm` on Debian 13 pulls ~400 packages
  (the GTK/X stack, through its module dependencies) for a wiki that needs
  neither. The role asserts the tarball declares no runtime dependencies, so a
  future release that grows some fails loudly instead of quietly needing a
  package manager.
- **The bind address is declared, not defaulted.** A TiddlyWiki server edition is
  writable and unauthenticated, so `listen_host` is an explicit exposure decision
  in the manifest and the guest is expected to sit behind a network that is not
  public.

### A worked example: two workstations and a switch

The point of this one is that **each workstation is a PC**: one NIC, no management
leg, and every bit of switching and addressing done by a MikroTik CHR, as it
would be on real hardware. The lab must not behave like a virtualised network.

```
          vmbr1  (gestión, 10.10.10.0/24)
            │
         ether1
      ┌───────────┐
      │  lab-chr  │  ether2 ──── vmbr2 ──── ws1     ← el "latiguillo RJ45"
      │  (switch) │  ether3 ──── vmbr3 ──── ws2
      └───────────┘

      bridge "lan" = ether2 + ether3      ← esto ES el switch
      10.30.0.1/24 + DHCP con una reserva por MAC
```

Three declarations carry it:

```yaml
pve_bridges:                      # un cable por bridge, sin puertos
  - {iface: vmbr2, comments: "lab cable: lab-chr ether2 <-> ws1"}
  - {iface: vmbr3, comments: "lab cable: lab-chr ether3 <-> ws2"}

# in pve_chr_defaults (merged only for entries declaring `chr: true`)
pve_chr_defaults:
  cores: 2
  memory: 512
  machine: pc                    # the vendor image is i440fx-era, not q35
  cloudinit: false
  boot: "order=scsi0"
  serial: {serial0: socket}      # the console that respects a non-US keyboard
  vga: serial0
  scsi: {scsi0: "local-zfs:8"}
  disk_import: [{key: scsi0, source: "local:import/chr-7.25.1.raw"}]

# in pve_vms — el switch
  - vmid: 140
    name: lab-chr
    chr: true
    net:
      net0: "virtio={{ lab_router_mac }},bridge=vmbr1"   # ether1: gestión
      net1: "virtio,bridge=vmbr2"                        # ether2: a ws1
      net2: "virtio,bridge=vmbr3"                        # ether3: a ws2
    config:
      routeros:
        identity: lab-chr
        dhcp_client: false
        addresses:
          - {address: "{{ lab_router_mgmt }}/24", interface: ether1}
          - {address: "{{ lab_gateway }}/24",     interface: "{{ lab_bridge_name }}"}
        bridges: [{name: "{{ lab_bridge_name }}", ports: [ether2, ether3]}]
        dhcp_servers:
          - name: lab
            interface: "{{ lab_bridge_name }}"
            address_pool: "10.30.0.10-10.30.0.254"
            network: "{{ lab_network }}"
            gateway: "{{ lab_gateway }}"
            dns_server: "{{ lab_gateway }}"
            # derivado de las workstations: su `address:` y la MAC de su net0
            leases: "{{ ... }}"

# in pve_containers — una workstation: UNA sola interfaz, y es la del lab
  - vmid: 141
    hostname: ws1
    address: "10.30.0.11"        # la reserva que el CHR deriva de aquí
    netif: {net0: "name=eth0,bridge=vmbr2,hwaddr=BC:24:11:00:00:51,ip=dhcp"}
```

**The switch is a bridge inside RouterOS**, not a Proxmox one: `ether2` and
`ether3` are ports of the same RouterOS bridge, so the frames are switched by the
CHR. `ether1` stays outside it — the management port of a switch, with the
management address of a switch.

**The router is converged over its REST API**, with `ansible.builtin.uri`
(`configure/roles/routeros`). It reads every table it can touch, computes an
explicit list of REST calls, prints that plan before sending anything, sends one
call per change, and then re-reads the router: a 200 is not proof that the router
stored what was declared. A router already in the declared state costs **zero**
calls.

The workstation declares `ip=dhcp` and the CHR derives its reservation from the
workstation's own entry — its `address:` and the MAC on its `net0` — so no value
is written twice. That is what a real switch does with a DHCP reservation.

Three consequences worth stating plainly:

- **The workstations have no management leg.** Layer 2 reaches them *through the
  lab*, so the control node's traffic has to be ROUTED to the network behind the
  router: `pve_tunnel.endpoint_routes` declares it and
  `playbooks/tools/wg_endpoint_setup.sh` applies it, in the same one-time paste
  that already exists. It is the only manual step left, because the PVE API
  cannot configure the host's routing table. Until it exists, a workstation
  declared `manage: true` fails its reachability check — and with `serial: 1`,
  one such guest stops every batch queued behind it.
- **A bridge per cable is not decoration.** If two guests shared one bridge,
  Proxmox would be doing the switching and the router would be a picture of a
  network. One bridge = one cable.
- **A CHR has no address at all when it boots**, so its API does not exist yet
  and nothing about it can be declared. See the next section.

### The one thing the API cannot do: a router's first address

A freshly imported CHR boots with a DHCP client on `ether1` and **no address**,
and as a VM it cannot take one from the host's DHCP server: the offer arrives
with an unresolved UDP checksum and the guest's kernel drops it. Without an
address its API does not exist, so there is nothing to configure it with.

`lab-bootstrap` (vmid 143) is the machine that hands over that single address. It
lives on the same segment as the router's management leg and runs dnsmasq bound to
that one MAC, with `ethtool -K eth0 tx off gso off tso off` — without which its
own offer would carry the same broken checksum.

The same defect that blocks the host's server is what makes this deterministic:
the router only ever sees this one offer, so there is no race between two DHCP
servers. After the router has taken the address, the container never intervenes
again — everything else is REST. It is a bootstrap, not a second configuration
path.

### Why not the community-scripts installers

Scripts like `community-scripts.org`'s run on the **PVE host as root**, drive `pct create` and then `pct exec` into the guest, and their "update" path is `apt upgrade` plus a GitHub release fetch. They are imperatives with no declared state: nothing to diff, nothing to verify, and they need the host shell this repository deliberately avoids. If one is ever used, run it once by hand and let layer 2 take over the inside afterwards — never both at once, or they fight over the same files.

## The key at birth, by default

Every guest this engine creates is born with the automation key already in place, with no
manual step anywhere:

- **Containers** use PVE's LXC `pubkey` parameter. It is a *birth-time* property: PVE writes it
  into `/root/.ssh/authorized_keys` at creation and the module never forwards it on update, so
  layer 1 can set it and layer 2 (`configure/`) is what verifies it from the inside.
- **VMs** have no equivalent, so the same key travels through cloud-init: `ciuser` plus
  `sshkeys` and a cloud-init drive (`<storage>:cloudinit`). It is on by default
  (`pve_vm_defaults.cloudinit: true`) and off per guest for an image that does not run
  cloud-init — the Proxmox VE installer ISO, for instance, which is why the `pve-nested`
  template in `pve_vms` declares `cloudinit: false`.

The single source of the key is the key FILE, not a copy of its text: `pve_ssh_pubkey_path`
points at the `.pub` next to the private key `configure/` uses, and `pve_defaults.pubkey` is a
`lookup('file', ...)` of it. A guest opts out with `pubkey: ""`.

Verified end to end: a guest created by `apply` answered over SSH as root on the first attempt,
with the `# --- BEGIN PVE ---` marker and the automation key in `authorized_keys`.

## Performance and how to iterate fast

Measured on the lab control node (a container on the PVE host itself, 4 vCPU, no CPU quota):

| Measurement | Result |
|---|---|
| PVE API per call, via `python-requests` | **15-80 ms** |
| All API reads of every declared guest, in **one** Python process | **0.34 s** |
| Those same reads spread across one Ansible task per guest | ~48 s of pure overhead |
| `ansible --version` (start-up alone) | 3.9 s |
| Marginal cost per task (measured from 1 to 8 tasks) | ~3 s/task + ~9 s fixed |
| `apply` before consolidation, nothing to change | 219 s |
| **`apply` now, nothing to change** | **41 s** |
| **`preflight` now** | **30 s** |
| `apply` creating a guest end to end | 105 s |

How it was done, without dropping a single check: **the execution boundaries were removed, not
the verification.** One reader process (`tools/pve_read.py`) performs every guest read *and*
every platform prerequisite (node online, storage active, template present, ISO present) that
used to cost a task each. One templating pass (`pve_engine.j2` macros) analyses all guests,
replacing the per-guest `set_fact` loop that cost a module execution per guest. Journal markers
and the git revision moved into the reader. The last two runs' numbers came out of the same
suite of checks: the platform prerequisites, the creation-time refusal, the declared-vs-live
comparison and the independent re-read verification are all still there.

A single Ansible module execution costs seconds here, so **the runtime is the number of module
executions, not the size of the work**. Every `loop` iteration is one execution, which is why a
task looping over five guests pays five times. That is the whole lever:

- **`apply` is the iteration verb.** It reads, applies and verifies through the API in one run.
- **To iterate on ONE guest, `--limit` already works — and it is the only selective run that
  stays honest.** `ansible-playbook configure/site.yml --limit localhost,lab-chr` runs the engine
  and converges exactly that guest (measured: `lab-chr ok=24 changed=0`, while the plays for the
  other guests report `skipping: no hosts matched`). `localhost` is required, because it is the
  play that builds the target inventory. Tags would buy the same convenience while inviting runs
  that skip part of a guest's declaration; a `--limit` names a guest, and a guest is converged
  whole or not at all.
- **Do not re-run just to prove idempotence.** `apply` already re-reads and prints the
  declared-versus-live comparison; a second run doubles the wall time for information you
  already have. Reserve the repeat for changes that touch creation-time properties or power.
- **Verification is not what costs.** `validate:`, the external HTTP probe and the journal
  assert are nearly free inside a task. What costs is a second verb, and a task — or a loop
  iteration — per item.

`ansible.cfg` already carries `pipelining`, SSH connection reuse (`ControlMaster`/
`ControlPersist`), `forks = 20` and `display_skipped_hosts = False`. Those remove per-task SSH
*setup* only; they do not reduce the ~3 s of Ansible/Python overhead per execution, which is why
fewer, coarser tasks remain the real lever.

Two measurement traps, both hit for real while building this:

- **`curl` lies about this API.** Every `curl` request to `:8006` takes a constant ~3.05 s of
  TTFB — with a valid token, with a bad one (401), with `--http1.1` and against the bare IP —
  while `python-requests`, which is what Ansible uses, answers in 15-80 ms. Benchmark with the
  same client the engine uses before concluding the API is slow.
- **A run is not "faster" if its outcome changed.** A 104 s → 32 s improvement turned out to be
  a run that aborted early (`failed=1`) because its target guest had been deleted. A speed-up is
  only a speed-up when the work done and the result are identical.
- **A Jinja `{% %}` block produces TEXT, a single `{{ }}` expression keeps its type.** Task vars
  built with `{% set %}`/`{% for %}` come back as strings, so `| length` counts characters and an
  assert comparing a list with `0` never passes. Build *lists* for assertions from one `{{ }}`
  expression and *messages* from the text blocks.
- **`for k, v in x | dict2items` is wrong.** `dict2items` yields items, so that unpacks the
  item's own `key`/`value` names and `v` becomes the string `"value"`. Use `x.items()`.

## Checking the framework itself

Four checks exist, and each answers a question the others cannot.

**The declared state has to make sense as a whole.** `filter_plugins/state.py`
validates the manifest's cross-references — that a RouterOS interface exists,
that an endpoint route is also in `guest_network`, that a DHCP reservation names a
declared machine, that a guest is not in both `pve_containers` and
`pve_config_targets` — and `playbooks/tasks/validate_state.yml` makes BOTH verbs
fail on it, before anything is read or written.

This is the highest-value check here, because the platform does not catch a
dangling reference: **RouterOS accepts an address on an interface that does not
exist and simply does not create it.** The API answers 200, the run is green, and
the router silently lacks the address. The failure then surfaces two layers later,
in a connectivity test, looking like a network problem.

The checks live in a filter rather than inside `playbooks/tools/pve_read.py` on
purpose: the reader parses the manifest RAW, so it sees `"{{ lab_bridge_name }}"`
as a literal string and every templated value would be a false positive. Ansible
resolves the Jinja before it calls a filter.

**Is it still convergent?** `framework_check.py invariant` runs a verb twice and
requires the second run to report `changed=0` and to cover the same hosts. It
reads Ansible's JSON callback instead of the human recap, because from a formatted
table you can read a count but not trust a LIST of hosts — and telling "the guest
was skipped" apart from "the guest was never in the play" is the whole point. Use
`--verb configure/site.yml` for the host-coverage half: the plays of `apply` and
`preflight` run on the control node, so their recap cannot show the guests, and
the script says so rather than claiming a coverage it did not check.

**Does it still see and repair a difference?** `framework_check.py drill` mutates
one guest out of band through the PVE API and requires the full sequence:
preflight clean → preflight reports DRIFT → apply repairs it → preflight clean
again. `changed=0` alone proves neither half of that. The mutation is a
`description`, which the declaration converges back, so the drill cleans up after
itself.

**`ansible-lint` stays green.** `.ansible-lint` forces the rules that matter and
records why each of the others is skipped. A linter with standing findings teaches
you to ignore it, which is worse than not running one. It is a convention ratchet,
not a correctness gate: it cannot see inside a `lookup('template')`, so it does
not catch the class of bug that actually costs a run here.

```
ansible-lint
python3 playbooks/tools/framework_check.py invariant --verb configure/site.yml
python3 playbooks/tools/framework_check.py drill
```

## How the engine behaves

Both playbooks build an effective spec per guest as `pve_defaults | combine(entry)`, read every guest and the platform facts in one process, and analyse the whole set in a single templating pass (`pve_engine.j2`), so no task has to be correlated with an API result by index any more.

**preflight.yml** (read-only) asserts the node is online and, per container, that its rootfs storage is active and its template is present; then prints `declared=… live=…`, what would be written, what would be deleted, the power transition and any creation-time drift, each container marked `IN SYNC` or `DRIFT`.

**apply.yml** refuses creation-time drift, creates the containers that do not exist, converges only the settings that actually differ, converges the power state, then re-reads every container and re-derives the whole comparison from that fresh read. A task exit status is not accepted as proof.

Normalisation is the subtle part (`playbooks/pve_engine.j2`), because PVE does not store values verbatim:

| What PVE does | How the engine handles it |
| --- | --- |
| reorders `tags` (`probe;manual` → `manual;probe`) | compares tags as a sorted set |
| appends a newline to `description` | compares trimmed |
| reorders the properties inside a `netX`/`mpX` value | canonicalised by `pve_canon_attachments` |
| adds `hwaddr=…` and `type=veth` to a `netX` value | drops both, or every run would PUT forever |
| resolves a mount's volume shorthand (`local:5` → `local:167/vm-167-disk-0.raw` + `size=5G`) | compares the storage, the size and the mount path instead |
| adds a CD-ROM `size=` (the media file size, e.g. `size=1666190K` for a 1.6 GB ISO) | ignored for ISO-backed attachments |
| puts a VM NIC's generated MAC inside the model property (`virtio=BC:24:…`) | compares `model=virtio`, MAC ignored |
| reports a running container as `running`, while the module's power vocabulary is `started` | power compared in the API's vocabulary |
| returns numbers where a template produced an int | forces `string` on both sides |

Without that normalisation the engine reports phantom drift and stops being idempotent.

Only the drifted keys are sent on the update path: `community.proxmox.proxmox` PUTs every parameter it receives, not just the ones it found different, so sending `disk`/`ostemplate`/`unprivileged` on an update would write them blindly. `state=present` also requires `node` and `hostname`, so those are always sent (hostname unchanged).

`community.proxmox.proxmox` declares `supports_check_mode=False`: under `--check` Ansible skips the mutating task and predicts nothing, so `apply.yml` fails early under check mode rather than implying a dry-run. Use `preflight.yml` for the non-mutating view.

`apply.yml` is idempotent: when live state matches the declared state it reports no changes and changes no config `digest`. Each run makes one HTTPS call per task per container against the node, so a full run takes roughly a minute over a remote API.

## Logging and API observability

Every run writes two files under `logs/` (gitignored — it describes real infrastructure and this repository is public):

**`logs/ansible.log`** — the full human-readable play output, appended by Ansible (`log_path` in `ansible.cfg`). Task results coming from the API tasks stay censored because they carry `no_log`.

**`logs/pve-api.jsonl`** — the machine-readable journal of the traffic that actually crossed the wire, one JSON object per line:

```json
{"ts": "...", "event": "api_call", "method": "PUT", "url": "https://…/nodes/Proxmox/lxc/141/config",
 "request_body": {"cores": 1, "delete": "net0", "tags": "ansible-managed;lab;netlab", "hostname": "ws1"},
 "status": 200, "response_body": "{\"data\":null}", "seconds": 0.186, "pid": 2457156}
```

`event` is `run_start` (timestamp, playbook, git revision, container list), `tracer_armed` (written once per module process — the per-process proof the trace is alive), `api_call`, `tracer_error` (a failure inside the tracer, with the exception), or `run_end`, so a reviewer can group the calls of one run and tie them to a code revision.

**The trace is checked, not trusted.** The tracer never fails silently: a write failure falls back to a module warning and an arming failure is recorded as `tracer_error`. On top of that, both playbooks compare the journal size at the start and end of the run and **fail the run** if it did not grow, so a trace that quietly stopped working cannot leave you with empty logs. `grep tracer_error logs/pve-api.jsonl` returning nothing is what "observability is working" looks like.

How it is armed: the playbooks put `playbooks/tracing` on `PYTHONPATH` and set `PVE_HTTP_TRACE`. CPython imports `sitecustomize` from there at interpreter start-up, so the trace is active in every module process — the modules themselves are untouched. The wrapper sits on `requests.Session.request`, which is what proxmoxer uses.

What it deliberately never records: request headers, so the API token never reaches the file; request-body fields whose name looks like a secret are redacted as well. Verified by grepping the files for the token: zero occurrences.

Reading it:

```bash
# every call of one run, in order
python3 -c "
import json
for l in open('logs/pve-api.jsonl'):
    r = json.loads(l)
    print(r['ts'], r['event'], r.get('method',''), r.get('status',''), r.get('url','').split('/api2/json')[-1], r.get('request_body',''))"

# only the mutations, with PVE's answer
grep '"method": "PUT"' logs/pve-api.jsonl | python3 -m json.tool --json-lines

# everything PVE rejected
grep -E '"status": 4[0-9][0-9]|"status": 5[0-9][0-9]' logs/pve-api.jsonl
```

Both files grow without bound; Ansible never rotates them and neither does the engine. Trim them when they get large — the response bodies of cluster-wide reads are the bulk of it.

The `logs/` directory must exist before Ansible starts (`log_path` cannot create it), which is why `logs/.gitkeep` is committed.

## Credentials and cleanup

The initial lab token was created for `root@pam` with privilege separation disabled and no expiry, so it has full root-equivalent API privileges. This is intentionally overprivileged for a disposable MVP: revoke it and create a scoped token before any ongoing use. The SSH key was only for initial discovery/bootstrap; no infrastructure playbook uses SSH.

To remove a guest, **un-declare it first and destroy it second** — see [Adding and removing a guest](#adding-and-removing-a-guest). `ansible-playbook playbooks/remove.yml -e hostname=<name> -e confirm=<name>` does the second step and refuses while the guest is still declared. Do not remove resources by SSH commands, and do not destroy a guest the manifest still declares: the next `apply` would recreate it.
