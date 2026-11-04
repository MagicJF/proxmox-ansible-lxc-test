# Proxmox LXC via Ansible

Declarative IaC for the disposable LXC `test-from-ansible` on Proxmox VE, driven entirely over the HTTPS API. No playbook connects to the Proxmox host over SSH.

## Structure

```text
inventory/group_vars/all.yml   # THE desired state — the only file that changes per request
playbooks/preflight.yml        # read-only: platform checks + drift report (no mutation)
playbooks/apply.yml            # converges to the declared state, then verifies via the API
```

The playbooks are a fixed engine with exactly two verbs. A change request is an edit to `inventory/group_vars/all.yml` on a new branch — not a new playbook. Adding a playbook per change would scatter the real state across files that overwrite each other.

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

`inventory/group_vars/all.yml` declares:

| Property | Value | Converged in place by `apply.yml` |
| --- | --- | --- |
| `pve_node` / `pve_vmid` / `pve_hostname` | `Proxmox` / `102` / `test-from-ansible` | no (identity) |
| `pve_cores` | 2 | yes |
| `pve_memory` | 2048 MiB | yes |
| `pve_swap` | 512 MiB | yes |
| `pve_onboot` | false | yes |
| `pve_unprivileged` | true | no (creation-time) |
| `pve_storage` | `local-zfs` | no (creation-time) |
| `pve_rootfs_size` | 8 GiB | no (creation-time, growth is irreversible) |
| `pve_template` | `local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst` | no (creation-time) |

The container is created stopped and without a network interface; change those parameters if the test needs connectivity or an automatically started container.

## How the engine behaves

**preflight.yml** (read-only): asserts the node is online, the declared rootfs storage is active and the declared template is present; then prints live-vs-declared for each in-place property and marks the drift. If the container does not exist yet, it says so instead.

**apply.yml**: reads the live container, then either creates it (absent) or converges it (present). It passes only in-place-changeable properties to `community.proxmox.proxmox` on the update path, because that module PUTs every parameter it receives — sending `disk`/`ostemplate`/`unprivileged` on an update would write them blindly. `state=present` also requires `node` and `hostname`, so those are always sent (hostname unchanged). The final task re-reads the container through the API and asserts the declared values; a task exit status alone is not proof.

Blocking drift — `unprivileged`, rootfs storage or rootfs size differing from the live container — is refused with an explicit message instead of reported as success, since PVE cannot change those in place and rootfs growth cannot be undone.

`community.proxmox.proxmox` declares `supports_check_mode=False`: under `--check` Ansible skips the mutating task and predicts nothing, so both playbooks fail early under check mode rather than implying a dry-run. Use `preflight.yml` for the non-mutating view.

`apply.yml` is idempotent: when live state matches the declared state it reports no changes. To change anything, edit `group_vars/all.yml` (or override for a one-off with `-e`) and run preflight then apply.

## Credentials and cleanup

The initial lab token was created for `root@pam` with privilege separation disabled and no expiry, so it has full root-equivalent API privileges. This is intentionally overprivileged for a disposable MVP: revoke it and create a scoped token before any ongoing use. The SSH key was only for initial discovery/bootstrap; no infrastructure playbook uses SSH.

To remove the disposable LXC later, set the VMID explicitly and use `community.proxmox.proxmox` with `state: absent`; verify the VMID first. Do not remove resources by SSH commands.
