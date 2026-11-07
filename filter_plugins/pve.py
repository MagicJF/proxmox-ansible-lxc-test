"""Ansible filters for comparing declared settings with what PVE actually stores.

PVE never stores an interface, a mount point or a disk verbatim:

* it appends `hwaddr=<generated>` and `type=veth` to a container `netX` value;
* a VM NIC carries its generated MAC inside the model property
  (`virtio=BC:24:...`), not next to it;
* it resolves a volume shorthand (`local:5`, `local-zfs:32`) into a real volume
  id (`local:167/vm-167-disk-0.raw`, `local-zfs:vm-120-disk-0`) and reports the
  size as `size=5G`;
* it reorders the comma-separated properties.

So the declared value is a *specification* and the live value its *resolved form*;
comparing them literally reports permanent drift and destroys idempotency.
`pve_canon_attachments` reduces both sides to a comparable canonical form:

    local:5,mp=/mnt/data                                 -> mp=/mnt/data,size=5G,storage=local
    local:167/vm-167-disk-0.raw,mp=/mnt/data,size=5G      -> mp=/mnt/data,size=5G,storage=local
    local-zfs:32                                          -> size=32G,storage=local-zfs
    local-zfs:vm-120-disk-0,size=32G                      -> size=32G,storage=local-zfs
    name=eth0,bridge=vmbr0,ip=dhcp                        -> bridge=vmbr0,ip=dhcp,name=eth0
    name=eth0,bridge=vmbr0,hwaddr=BC:24:...,ip=dhcp       -> bridge=vmbr0,ip=dhcp,name=eth0
    virtio,bridge=vmbr0                                   -> bridge=vmbr0,model=virtio
    virtio=BC:24:11:CF:A3:0B,bridge=vmbr0                 -> bridge=vmbr0,model=virtio
    local:iso/proxmox-ve_9.2-1.iso,media=cdrom            -> local:iso/proxmox-ve_9.2-1.iso,media=cdrom
    local:iso/proxmox-ve_9.2-1.iso,media=cdrom,size=1666190K -> local:iso/proxmox-ve_9.2-1.iso,media=cdrom

Deliberately ignored: `hwaddr` and a VM NIC's generated MAC (regenerated per
guest), `type=veth` (PVE's own default) and the resolved volume id. An explicitly
declared MAC or `type=veth` is therefore not compared either — documented rather
than pretended otherwise.

A CD-ROM volume (`<storage>:iso/...`, `<storage>:/...) is NOT a generated volume
id, so it is kept verbatim: which ISO is attached must stay visible to the
comparison.
"""

import re

from ansible.errors import AnsibleFilterError

# Container and VM attachment keys: interfaces, mount points, every disk bus.
_ATTACHMENT_KEY = re.compile(r"^(net|mp|scsi|virtio|sata|ide|efidisk|tpmstate|serial)[0-9]+$")

_VOLUME_SHORTHAND = re.compile(r"^([^:,]+):([0-9]+)$")
# Only PVE-generated volume ids are reduced to their storage; an ISO path is not.
# Covers `local-zfs:vm-120-disk-0`, `local-zfs:subvol-102-disk-0` and
# `local:167/vm-167-disk-0.raw` while leaving `local:iso/debian.iso` intact.
_GENERATED_VOLUME = re.compile(r"^([^:,]+):(?:.*/)?(?:vm|subvol|base)-\d+-.+$")
_NIC_MODEL_WITH_MAC = re.compile(
    r"^(virtio|e1000|e1000e|rtl8139|vmxnet3|i82551|i82557b|i82559er|ne2k_pci|ne2k_isa|pcnet)"
    r"=(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$"
)
# A VM NIC declared without a MAC is just the model name (`virtio`).
_NIC_MODEL_ONLY = re.compile(
    r"^(virtio|e1000|e1000e|rtl8139|vmxnet3|i82551|i82557b|i82559er|ne2k_pci|ne2k_isa|pcnet)$"
)
_IGNORED_PREFIXES = ("hwaddr=", "type=veth")


def pve_attachments(config):
    """Return only the interface/mount/disk entries of a PVE config dict."""
    if not isinstance(config, dict):
        raise AnsibleFilterError("pve_attachments expects a dict")
    return {key: value for key, value in config.items() if _ATTACHMENT_KEY.match(str(key))}


def pve_canon_attachments(mapping):
    """Canonicalise the values of a {net0|mp0|scsi0|ide2|...: "prop=val,..."} mapping."""
    if not isinstance(mapping, dict):
        raise AnsibleFilterError("pve_canon_attachments expects a dict")
    canonical = {}
    for key, raw in mapping.items():
        value = str(raw)
        # On a CD-ROM, `size=` is the size PVE reports for the media file, not a
        # declared capacity (a 1.6 GB ISO comes back as size=1666190K), so it is
        # dropped for ISO-backed attachments.
        iso_backed = ":iso/" in value or "media=cdrom" in value
        parts = []
        for part in value.split(","):
            part = part.strip()
            if not part or part.startswith(_IGNORED_PREFIXES):
                continue
            if iso_backed and part.startswith("size="):
                continue
            nic = _NIC_MODEL_WITH_MAC.match(part)
            if nic:
                # virtio=BC:24:... -> the model, without the generated MAC
                parts.append(f"model={nic.group(1)}")
                continue
            if _NIC_MODEL_ONLY.match(part):
                # a NIC declared without a MAC is the bare model name
                parts.append(f"model={part}")
                continue
            shorthand = _VOLUME_SHORTHAND.match(part)
            if shorthand:
                # local:5 -> the storage it lives on plus the declared size
                parts.append(f"storage={shorthand.group(1)}")
                parts.append(f"size={shorthand.group(2)}G")
                continue
            generated = _GENERATED_VOLUME.match(part)
            if generated:
                # local:167/vm-167-disk-0.raw -> only the storage is comparable
                parts.append(f"storage={generated.group(1)}")
                continue
            parts.append(part)
        canonical[key] = ",".join(sorted(set(parts)))
    return canonical


def pve_disk_specs(scsi, disk_import):
    """Turn the disks that come from a vendor image into the API's import syntax.

    proxmox_kvm can only build a disk from a SIZE, and the PVE API cannot
    decompress an image. A guest that boots a vendor image therefore declares
    the disk as what it must END UP being (`local-zfs:8`) plus a `disk_import`
    entry naming the uploaded image, and this filter rewrites that one key into
    the creation-time syntax `<storage>:0,import-from=<volid>`. PVE creates the
    volume at the IMAGE's size, so the declared size is applied afterwards by
    playbooks/tasks/disk_import.yml.

    A key without an import entry is returned untouched, so a VM can mix an
    imported image with ordinary disks.
    """
    if not isinstance(scsi, dict):
        raise AnsibleFilterError("pve_disk_specs expects a dict of disks")
    imports = {}
    for entry in disk_import or []:
        if not isinstance(entry, dict) or "key" not in entry or "source" not in entry:
            raise AnsibleFilterError(
                "pve_disk_specs: every disk_import entry needs a `key` and a `source`"
            )
        if entry["key"] not in scsi:
            raise AnsibleFilterError(
                f"pve_disk_specs: disk_import names {entry['key']}, "
                f"which is not declared in the guest's disks ({sorted(scsi)})"
            )
        imports[entry["key"]] = entry["source"]
    if not imports:
        return scsi
    rewritten = {}
    for key, value in scsi.items():
        source = imports.get(key)
        # `<storage>:<size>` -> `<storage>:0,import-from=<volid>`: size 0 means
        # "take the image's size", which is what the import writes.
        rewritten[key] = (
            f"{str(value).split(':')[0]}:0,import-from={source}" if source else value
        )
    return rewritten


def pve_declared_ipv4(netif):
    """Return the first static IPv4 declared in a container's netif mapping.

    Layer 2 (configuration) needs a reachable address, and the address is already
    declared inside net0 (`ip=10.10.10.21/24`). Deriving it here keeps one source
    of truth instead of repeating the address in a second field. Returns None when
    the interface is DHCP-only or absent.
    """
    if not isinstance(netif, dict):
        raise AnsibleFilterError("pve_declared_ipv4 expects a dict")
    for value in netif.values():
        for part in str(value).split(","):
            if part.strip().startswith("ip="):
                address = part.split("=", 1)[1].strip()
                if address and address != "dhcp":
                    return address.split("/")[0]
    return None


class FilterModule(object):
    def filters(self):
        return {
            "pve_attachments": pve_attachments,
            "pve_canon_attachments": pve_canon_attachments,
            "pve_declared_ipv4": pve_declared_ipv4,
            "pve_disk_specs": pve_disk_specs,
        }
