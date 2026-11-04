"""Ansible filters for comparing declared settings with what PVE actually stores.

PVE never stores a network interface or a mount point verbatim:

* it appends `hwaddr=<generated>` and `type=veth` to a `netX` value;
* it resolves a mount's volume shorthand (`local:5`) into a real volume id
  (`local:167/vm-167-disk-0.raw`) and reports the size as `size=5G`;
* it reorders the comma-separated properties.

So the declared value is a *specification* and the live value is its *resolved
form*; comparing them literally reports permanent drift and destroys idempotency.
`pve_canon_attachments` reduces both sides to a comparable canonical form:

    local:5,mp=/mnt/data
        -> mp=/mnt/data,size=5G,storage=local
    local:167/vm-167-disk-0.raw,mp=/mnt/data,size=5G
        -> mp=/mnt/data,size=5G,storage=local
    name=eth0,bridge=vmbr0,ip=dhcp
        -> bridge=vmbr0,ip=dhcp,name=eth0
    name=eth0,bridge=vmbr0,hwaddr=BC:24:...,ip=dhcp,type=veth
        -> bridge=vmbr0,ip=dhcp,name=eth0

Deliberately ignored: `hwaddr` (regenerated per container), `type=veth` (PVE's
own default) and the resolved volume id (PVE's business). An explicitly declared
`hwaddr` or `type=veth` is therefore not compared either — document it rather
than pretend otherwise.
"""

import re

from ansible.errors import AnsibleFilterError

_ATTACHMENT_KEY = re.compile(r"^(net|mp)[0-9]+$")
_VOLUME_SHORTHAND = re.compile(r"^([^:,]+):([0-9]+)$")
_VOLUME_RESOLVED = re.compile(r"^([^:,]+):(.+)$")
_IGNORED = ("hwaddr=", "type=veth")


def pve_attachments(config):
    """Return only the netX/mpX entries of a PVE config dict."""
    if not isinstance(config, dict):
        raise AnsibleFilterError("pve_attachments expects a dict")
    return {key: value for key, value in config.items() if _ATTACHMENT_KEY.match(str(key))}


def pve_canon_attachments(mapping):
    """Canonicalise the values of a {netX|mpX: "prop=val,..."} mapping."""
    if not isinstance(mapping, dict):
        raise AnsibleFilterError("pve_canon_attachments expects a dict")
    canonical = {}
    for key, raw in mapping.items():
        parts = []
        for part in str(raw).split(","):
            part = part.strip()
            if not part or part.startswith(_IGNORED):
                continue
            shorthand = _VOLUME_SHORTHAND.match(part)
            if shorthand:
                # local:5 -> the storage it lives on plus the declared size
                parts.append(f"storage={shorthand.group(1)}")
                parts.append(f"size={shorthand.group(2)}G")
                continue
            resolved = _VOLUME_RESOLVED.match(part)
            if resolved and not part.startswith(("mp=", "size=")):
                # local:167/vm-167-disk-0.raw -> only the storage is comparable
                parts.append(f"storage={resolved.group(1)}")
                continue
            parts.append(part)
        canonical[key] = ",".join(sorted(set(parts)))
    return canonical


class FilterModule(object):
    def filters(self):
        return {
            "pve_attachments": pve_attachments,
            "pve_canon_attachments": pve_canon_attachments,
        }
