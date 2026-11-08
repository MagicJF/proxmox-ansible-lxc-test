#!/usr/bin/env python3
"""Espera a que los equipos RouterOS de un escalón contesten su API.

`startall` devuelve una tarea, y la tarea termina cuando Proxmox ha DADO la orden de
arranque — que no es lo mismo que el invitado estar listo. Un CHR tarda decenas de
segundos en levantar RouterOS, y hasta entonces no atiende ni su API ni el DHCP de su
sede, que es justo lo que el escalón siguiente necesita para pedir su dirección.

Así que la prueba de que este escalón está listo no es que su tarea acabara: es que
cada equipo contesta. Y se espera en UN solo proceso para todos, no con una tarea de
Ansible por equipo — lo mismo que el framework hace al leer la API de Proxmox.

    routeros_wait.py <segundos> <ip> [<ip> ...]

Sale 0 cuando contestan todos, 1 diciendo cuáles faltan, 2 si se usa mal.
"""

import os
import sys
import time

import requests
import yaml

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SECRETS = os.path.join(REPO, "playbooks/vars/secrets.yml")
ROUTEROS_DEFAULTS = os.path.join(REPO, "configure/roles/routeros/defaults/main.yml")


def credenciales():
    secretos = yaml.safe_load(open(SECRETS)) or {}
    valores = yaml.safe_load(open(ROUTEROS_DEFAULTS)) or {}
    return (
        valores.get("routeros_user", "admin"),
        secretos.get("pve_routeros_password", ""),
        valores.get("routeros_scheme", "http"),
        valores.get("routeros_port", 80),
    )


def main():
    if len(sys.argv) < 3:
        print("uso: routeros_wait.py <segundos> <ip> [<ip> ...]")
        return 2
    limite = int(sys.argv[1])
    equipos = sys.argv[2:]
    usuario, clave, esquema, puerto = credenciales()

    pendientes = list(equipos)
    arranque = time.time()
    while pendientes and (time.time() - arranque) < limite:
        pendientes = []
        for ip in equipos:
            try:
                respuesta = requests.get(
                    f"{esquema}://{ip}:{puerto}/rest/system/identity",
                    auth=(usuario, clave),
                    timeout=5,
                )
                if respuesta.status_code != 200:
                    pendientes.append(ip)
            except Exception:
                pendientes.append(ip)
        if pendientes:
            time.sleep(5)

    if pendientes:
        print(
            f"FALLO: tras {limite} s de margen no contestan su API: "
            f"{', '.join(pendientes)}. Proxmox dio la orden de arranque, pero el "
            "equipo no ha levantado."
        )
        return 1

    print(
        f"OK: los {len(equipos)} equipos de red contestan su API, tras "
        f"{time.time() - arranque:.0f} s."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
