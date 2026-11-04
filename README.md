# Proxmox LXC via Ansible

Declarative IaC for the LXC containers on Proxmox VE, driven entirely over the HTTPS API. No playbook connects to the Proxmox host over SSH.

## Structure

```text
inventory/group_vars/all.yml          # THE desired state — the only file that changes per request
playbooks/preflight.yml               # read-only: platform checks + declared-vs-live report
playbooks/apply.yml                   # converges every declared container, then verifies via the API
playbooks/vars/state.yml              # engine: declared-vs-live translation and normalisation
playbooks/filter_plugins/pve.py       # engine: canonicalises netX/mpX values PVE rewrites
playbooks/tracing/sitecustomize.py    # engine: journals every PVE HTTP call
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

## Local environment

Export these variables before running Ansible:

```text
PROXMOX_HOST=<PVE API hostname or SAN-matching IP>
PROXMOX_TOKEN_ID=<user@realm!token-name>
PROXMOX_TOKEN_SECRET=<token secret>
PROXMOX_CA_PATH=<path to trusted PVE certificate/CA>
```

For this lab, the certificate includes the PVE public IP as a SAN; the LAN address `10.10.10.1` is not in that certificate, so use a SAN-matching name/IP or replace the certificate with one covering the LAN name. Keep the environment file outside the repository and mode `0600`.

## Run

```bash
python3 -m pip install -r requirements.txt
ansible-galaxy collection install -r requirements.yml
ansible-playbook --syntax-check playbooks/preflight.yml
ansible-playbook --syntax-check playbooks/apply.yml
ansible-playbook playbooks/preflight.yml   # read-only: checks + what would change
ansible-playbook playbooks/apply.yml       # converge + verify
```

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
