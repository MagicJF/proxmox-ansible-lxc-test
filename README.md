# Proxmox LXC via Ansible

Disposable MVP that provisions `test-from-ansible` as an LXC on Proxmox VE using the HTTPS API only. No playbook connects to the Proxmox host over SSH.

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
ansible-playbook --syntax-check playbooks/create_lxc.yml
ansible-playbook playbooks/preflight.yml
ansible-playbook playbooks/create_lxc.yml
```

`preflight.yml` is the non-mutating dry-run: it reads node, VMID/name, storage, and template state through Ansible/API tasks and asserts the selected values are usable. The creation module explicitly does **not** support Ansible check mode (`--check`), so check mode cannot predict a create; it must not be treated as a successful dry-run. Apply only after preflight passes.

The playbook uses VMID `102`, node `Proxmox`, template `local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst`, rootfs on `local-zfs` at 8 GiB, 1 vCPU, 1 GiB RAM and 512 MiB swap. It creates the container stopped and without a network interface; change those parameters if the test needs connectivity or an automatically started container.

## Credentials and cleanup

The initial lab token was created for `root@pam` with privilege separation disabled and no expiry, so it has full root-equivalent API privileges. This is intentionally overprivileged for a disposable MVP: revoke it and create a scoped token before any ongoing use. The SSH key was only for initial discovery/bootstrap; no infrastructure playbook uses SSH.

To remove the disposable LXC later, use the same API credentials with `community.proxmox.proxmox` and `state: absent` for VMID `102`; verify the VMID first. Do not remove resources by SSH commands.
