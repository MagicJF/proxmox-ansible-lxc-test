# Proxmox LXC via Ansible

Declarative IaC for the LXC containers on Proxmox VE, driven entirely over the HTTPS API. No playbook connects to the Proxmox host over SSH.

## Structure

```text
inventory/group_vars/all.yml   # THE desired state — the only file that changes per request
playbooks/preflight.yml        # read-only: platform checks + per-container drift report
playbooks/apply.yml            # converges every declared container, then verifies via the API
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

`inventory/group_vars/all.yml` holds `pve_node`, a `pve_defaults` block for creation-time values, and `pve_containers`: **the only containers this repository manages**. A container absent from that list is never read, created or modified — the pre-existing guests `100`, `101` and `105` are deliberately out of scope.

| Entry | vmid | hostname | cores | memory |
| --- | --- | --- | --- | --- |
| 1 | 102 | `test-from-ansible` | 2 | 2048 MiB |
| 2 | 103 | `3cores` | 3 | 2048 MiB |

Inherited from `pve_defaults` unless overridden per entry: `storage: local-zfs`, `rootfs_size: 8` GiB, `template: local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst` (on `local`), `unprivileged: true`, `onboot: false`, `swap: 512` MiB.

| Property | Converged in place by `apply.yml` |
| --- | --- |
| `vmid`, `hostname` | no (identity) |
| `cores`, `memory`, `swap`, `onboot` | yes |
| `unprivileged`, `storage`, `rootfs_size`, `template` | no (creation-time) |

Containers are created stopped and without a network interface; add those parameters if a container needs connectivity or should start automatically.

## How the engine behaves

Both playbooks build an effective spec per container as `pve_defaults | combine(entry)` and loop over `pve_containers`, tracking the iteration with `loop_control.index_var` so each task reads back the matching API result.

**preflight.yml** (read-only): asserts the node is online and, per container, that its rootfs storage is active and its template is present; then prints `live -> declared` for every in-place property marked `in sync` or `DRIFT`. Containers that do not exist yet are reported as "will be created" instead. Exits non-zero only on a real blocker.

**apply.yml**: reads every declared container, then creates the absent ones and converges the existing ones, and finally re-reads each and asserts the declared values — a task exit status alone is not proof. The update path sends only in-place-changeable properties, because `community.proxmox.proxmox` PUTs every parameter it receives: passing `disk`/`ostemplate`/`unprivileged` on an update would write them blindly. `state=present` also requires `node` and `hostname`, so those are always sent (hostname unchanged).

Blocking drift — `unprivileged`, rootfs storage or rootfs size differing from the live container — makes the run fail with an explicit message for that container instead of reporting a success the API never applied, since PVE cannot change those in place and rootfs growth cannot be undone.

`community.proxmox.proxmox` declares `supports_check_mode=False`: under `--check` Ansible skips the mutating task and predicts nothing, so `apply.yml` fails early under check mode rather than implying a dry-run. Use `preflight.yml` for the non-mutating view.

`apply.yml` is idempotent: when live state matches the declared state it reports no changes. To change anything, edit `group_vars/all.yml` (or override for a one-off with `-e`) and run preflight then apply.

## Credentials and cleanup

The initial lab token was created for `root@pam` with privilege separation disabled and no expiry, so it has full root-equivalent API privileges. This is intentionally overprivileged for a disposable MVP: revoke it and create a scoped token before any ongoing use. The SSH key was only for initial discovery/bootstrap; no infrastructure playbook uses SSH.

To remove a disposable container later, delete its entry from `pve_containers` and use `community.proxmox.proxmox` with `state: absent` for that VMID; verify the VMID first. Do not remove resources by SSH commands.
