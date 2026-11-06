# Proxmox LXC via Ansible

Declarative IaC for the LXC containers and QEMU VMs on Proxmox VE, driven entirely over the HTTPS API. No playbook connects to the Proxmox host over SSH.

## Structure

```text
inventory/group_vars/all.yml          # THE desired state — the only file that changes per request
playbooks/preflight.yml               # read-only: platform checks + declared-vs-live report
playbooks/apply.yml                   # converges every declared container, then verifies via the API
playbooks/vars/state.yml              # engine: declared-vs-live translation and normalisation
playbooks/tracing/sitecustomize.py    # engine: journals every PVE HTTP call
filter_plugins/pve.py                 # engine: filters shared by both layers (wired in ansible.cfg)
configure/audit.yml                   # layer 2: read-only inspection of a guest, changes nothing
configure/site.yml                    # layer 2: converges the inside of managed guests over SSH
configure/tasks/                      # layer 2: shared target building + access policy
configure/roles/<app>/                # layer 2: one role per application
filter_plugins/pve.py                 # engine: filters shared by both layers (via ansible.cfg)
logs/                                 # run logs and the API journal (gitignored, kept via .gitkeep)
```

The playbooks are a fixed engine with exactly two verbs, both looping over the container list. A change request is an edit to `inventory/group_vars/all.yml` on a new branch — not a new playbook. Adding a playbook per change would scatter the real state across files that overwrite each other.

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
```

For interactive iteration, `apply` alone is the verb: it reads the declared state, applies
what differs, then re-reads and verifies by API in the same run. Run `preflight` when you
suspect drift, not as a routine step — see [Performance and how to iterate fast](#performance-and-how-to-iterate-fast).

## Declared state

`inventory/group_vars/all.yml` holds `pve_node`, a `pve_defaults` block, and `pve_containers`: **the only containers this repository manages**. A container absent from that list is never read, created or modified — the pre-existing guests `100`, `101` and `105` are deliberately out of scope. Every key of `pve_defaults` can be overridden per container.

| Entry | vmid | hostname | cores | memory | swap | status |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 102 | `test-from-ansible` | 2 | 2048 MiB | 512 MiB | stopped |
| 2 | 103 | `3cores` | 3 | 2048 MiB | 512 MiB | stopped |

Declared per container, inherited from `pve_defaults` unless overridden:

| Property | Declared as | Converged by `apply.yml` |
| --- | --- | --- |
| `vmid`, `hostname` | identity | no — changing them means "recreate" |
| `ostype` | `debian` | no — creation-time |
| `storage`, `rootfs_size`, `template`, `template_storage` | `local-zfs`, 8 GiB, Debian 13 | no — creation-time, rootfs growth is irreversible |
| `unprivileged` | `true` | no — creation-time |
| `cores`, `memory`, `swap`, `cpuunits` | 2-3, 2048, 512, 1024 | yes |
| `onboot` | `false` | yes |
| `status` | `stopped` | yes — the container is stopped/started to match |
| `cmode` | `shell` | yes |
| `timezone` | `host` | yes |
| `description` | see file | yes |
| `tags` | `[ansible-managed, lab]` | yes |
| `features`, `startup` | `[]` (must not exist) | yes — deleted if present |
| `netif` | `{}` (no interface declared) | yes — declared interfaces are created, undeclared ones are deleted |
| `mounts`, `mount_devices` | `{}`, `[]` (must not exist) | yes — deleted if present |
| `nameserver`, `searchdomain` | `null` | yes — deleted if present |

**Absence is state.** A setting declared as `null`/empty must not exist in the container, and the engine deletes it if it is there. That is how a hand-added interface, mount or `features=` line gets reverted.

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
| `pve-nested` | 120 | 4 vCPU x 1 socket, 4096 MiB, 32 GiB disk on `local-zfs`, `q35`/SeaBIOS, one virtio NIC on `vmbr1`, CD-ROM on `proxmox-ve_9.2-1.iso` | a nested Proxmox VE |

Note the bridge: `vmbr1` is the internal LAN (`10.10.10.0/24`) shared with every other lab guest, while `vmbr0` is the **public** bridge (`54.36.168.83/24` on `eno3`). Attaching a lab guest to `vmbr0` exposes it to the internet.

`preflight.yml` additionally asserts that every ISO a VM declares is actually present on the node before anything is created.

The nested PVE installation is **not** automated: `preflight`/`apply` leave the VM booting the installer from the CD-ROM (declared `boot: order=ide2;scsi0`), and the installation is finished by hand from the noVNC console in the PVE UI. Nothing in this repository claims the guest is installed.

## Layer 2 — configuring the INSIDE of a guest

Layer 1 provisions a guest through the Proxmox API. That API cannot run anything inside a guest: `POST /nodes/{node}/lxc/{vmid}/exec` answers **501 Method not implemented**. So a Caddyfile, a systemd unit or a cron job is out of layer 1's reach by construction, and layer 2 exists for exactly that boundary: it reaches the guest over SSH and converges what runs inside it.

The methodology, in one line per rule:

- **One declared state, two layers.** `inventory/group_vars/all.yml` stays the single source of truth: layer 1 reads the guest's shape, layer 2 reads the guest's `config:` block. Addresses are never duplicated — layer 2 derives the SSH target from the interface declaration with the `pve_declared_ipv4` filter.
- **Read before you write.** `configure/audit.yml` inspects a guest and changes nothing. Run it before declaring `manage: true` on a guest that already exists.
- **Convergence is opt-in per guest.** A `config:` block plus `manage: true` means layer 2 owns the inside; `manage: false` (or absent) means audit only. A guest is never configured by accident.
- **Each layer verifies what it declares.** Layer 1 re-reads through the API. Layer 2 validates inside the guest (`caddy validate`, `systemctl is-active`, the timers) *and* from outside (an HTTP request asserting the declared status and body).
- **Access has a home.** Layer 1 injects the automation key **at birth** (`pubkey` in `pve_defaults` — PVE writes it into `/root/.ssh/authorized_keys` at creation and never forwards it on update). For a guest that already exists, the key is granted once from the PVE console and the run tells you the exact command, with the public key in the message.

```bash
ansible-playbook configure/audit.yml   # read-only: what is inside every target
ansible-playbook configure/site.yml    # converge every guest declared manage: true
```

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

### Why not the community-scripts installers

Scripts like `community-scripts.org`'s run on the **PVE host as root**, drive `pct create` and then `pct exec` into the guest, and their "update" path is `apt upgrade` plus a GitHub release fetch. They are imperatives with no declared state: nothing to diff, nothing to verify, and they need the host shell this repository deliberately avoids. If one is ever used, run it once by hand and let layer 2 take over the inside afterwards — never both at once, or they fight over the same files.

## Performance and how to iterate fast

Measured on the lab control node (a container on the PVE host itself, 4 vCPU, no CPU quota):

| Measurement | Result |
|---|---|
| `apply` with nothing to change | **219 s** for ~26 executed tasks |
| PVE API per call, via `python-requests` | **15-80 ms** |
| All 12 API reads of every declared guest, in **one** Python process | **0.34 s** |
| Those same 12 reads spread across 12 Ansible tasks | ~48 s of pure overhead |
| `ansible --version` (start-up alone) | 3.9 s |
| Marginal cost per task (measured from 1 to 8 tasks) | ~3 s/task + ~9 s fixed |

A single Ansible module execution costs seconds here, so **the runtime is the number of module
executions, not the size of the work**. Every `loop` iteration is one execution, which is why a
task looping over five guests pays five times. That is the whole lever:

- **`apply` is the iteration verb.** It reads, applies and verifies through the API in one run.
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

## How the engine behaves

Both playbooks build an effective spec per container as `pve_defaults | combine(entry)`, analyse each container against a fresh API read, and loop with `loop_control.index_var` to correlate every task with its own API result.

**preflight.yml** (read-only) asserts the node is online and, per container, that its rootfs storage is active and its template is present; then prints `declared=… live=…`, what would be written, what would be deleted, the power transition and any creation-time drift, each container marked `IN SYNC` or `DRIFT`.

**apply.yml** refuses creation-time drift, creates the containers that do not exist, converges only the settings that actually differ, converges the power state, then re-reads every container and re-derives the whole comparison from that fresh read. A task exit status is not accepted as proof.

Normalisation is the subtle part (`playbooks/vars/state.yml`), because PVE does not store values verbatim:

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
{"ts": "...", "event": "api_call", "method": "PUT", "url": "https://…/nodes/Proxmox/lxc/102/config",
 "request_body": {"cores": 2, "delete": "net0", "tags": "ansible-managed;lab", "hostname": "test-from-ansible"},
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

To remove a container, delete its entry from `pve_containers` and use `community.proxmox.proxmox` with `state: absent` for that VMID; verify the VMID first. Do not remove resources by SSH commands.
