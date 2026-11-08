#!/usr/bin/env python3
"""Espera a que los equipos RouterOS de un escalón estén LISTOS, no sólo encendidos.

`startall` devuelve una tarea, y la tarea termina cuando Proxmox ha DADO la orden de
arranque — que no es lo mismo que el invitado estar listo. Un CHR tarda decenas de
segundos en levantar RouterOS, y hasta entonces no atiende ni su API ni el DHCP de su
sede, que es justo lo que el escalón siguiente necesita para pedir su dirección.

Y contestar la API tampoco basta: un equipo puede contestar y no servir para nada.
Así que aquí se comprueba que FUNCIONA, con las señales que da el propio RouterOS:

  - todo puente declarado está `running`                (un puente caído no conmuta)
  - toda interfaz usada —la de un puente o la de una dirección— tiene enlace
  - toda dirección está viva, sobre la interfaz que dice
  - toda ruta está `active`                             (una ruta inactiva no enruta)
  - todo servidor DHCP está encendido, y su pozo existe

Se espera a todo en UN solo proceso, no con una tarea de Ansible por equipo — igual
que el framework hace al leer la API de Proxmox.

    routeros_wait.py <segundos> <ip> [<ip> ...]

Sale 0 cuando todos contestan Y funcionan, 1 diciendo qué falla, 2 si se usa mal.
"""

import os
import sys
import time

import requests
import yaml

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SECRETS = os.path.join(REPO, "playbooks/vars/secrets.yml")
ROUTEROS_DEFAULTS = os.path.join(REPO, "configure/roles/routeros/defaults/main.yml")

# Menús que se consultan para saber si el equipo sirve para algo, en el orden en que
# se necesitan: de los puentes salen las interfaces usadas, y de las direcciones, las
# que deben tener enlace.
MENUS = ("interface/bridge", "interface/bridge/port", "interface/ethernet",
         "ip/address", "ip/route", "ip/dhcp-server", "ip/pool")


def credenciales():
    secretos = yaml.safe_load(open(SECRETS)) or {}
    valores = yaml.safe_load(open(ROUTEROS_DEFAULTS)) or {}
    return (
        valores.get("routeros_user", "admin"),
        secretos.get("pve_routeros_password", ""),
        valores.get("routeros_scheme", "http"),
        valores.get("routeros_port", 80),
    )


def leer(sesion, base, menu):
    """Las entradas de un menú, o None si no se puede leer."""
    try:
        r = sesion.get(f"{base}/rest/{menu}", timeout=8)
        if r.status_code != 200:
            return None
        datos = r.json()
        return datos if isinstance(datos, list) else datos.get("data", [])
    except Exception:
        return None


def funciona(sesion, base, credenciales_http):
    """Lista de problemas; vacía cuando el equipo contesta Y funciona."""
    tablas = {}
    for menu in MENUS:
        tablas[menu] = leer(sesion, base, menu)
    if any(v is None for v in tablas.values()):
        faltan = [m for m, v in tablas.items() if v is None]
        return [f"no se pudo leer {', '.join(faltan)}"]

    problemas = []
    usadas = set()

    for b in tablas["interface/bridge"]:
        usadas.add(b.get("name", ""))
        if str(b.get("disabled")) == "true":
            problemas.append(f"el puente {b.get('name')} está deshabilitado")
        elif str(b.get("running")) != "true":
            problemas.append(f"el puente {b.get('name')} no está en marcha")

    for p in tablas["interface/bridge/port"]:
        usadas.add(p.get("interface", ""))
        if str(p.get("disabled")) == "true":
            problemas.append(f"el puerto {p.get('interface')} está deshabilitado")

    for a in tablas["ip/address"]:
        usadas.add(a.get("interface", ""))
        if str(a.get("disabled")) == "true":
            problemas.append(f"la dirección {a.get('address')} está deshabilitada")
        elif not a.get("actual-interface"):
            problemas.append(
                f"la dirección {a.get('address')} no está viva sobre {a.get('interface')}")

    for r in tablas["ip/route"]:
        if str(r.get("disabled")) == "true":
            continue
        if str(r.get("active")) != "true":
            problemas.append(
                f"la ruta a {r.get('dst-address')} no está activa, así que no enruta")

    for d in tablas["ip/dhcp-server"]:
        if str(d.get("disabled")) == "true":
            problemas.append(f"el servidor DHCP {d.get('name')} está apagado")

    pozos = {p.get("name") for p in tablas["ip/pool"] if str(p.get("disabled")) != "true"}
    for d in tablas["ip/dhcp-server"]:
        if str(d.get("disabled")) == "true":
            continue
        suyo = d.get("address-pool")
        if suyo and suyo not in pozos:
            problemas.append(
                f"el servidor DHCP {d.get('name')} apunta al pozo {suyo}, que no existe")

    # Sólo las interfaces USADAS: una que no está en ningún puente ni lleva ninguna
    # dirección puede estar sin cable, y eso no es un fallo de este laboratorio.
    for e in tablas["interface/ethernet"]:
        if e.get("name") in usadas and str(e.get("disabled")) != "true":
            if str(e.get("running")) != "true":
                problemas.append(f"la interfaz {e.get('name')} no tiene enlace")
    return problemas


def main():
    if len(sys.argv) < 3:
        print("uso: routeros_wait.py <segundos> <ip> [<ip> ...]")
        return 2
    limite = int(sys.argv[1])
    equipos = sys.argv[2:]
    usuario, clave, esquema, puerto = credenciales()
    autenticacion = (usuario, clave)
    sesion = requests.Session()
    sesion.auth = autenticacion

    arranque = time.time()
    problemas = {"(sin contestar)": []}
    pendientes = list(equipos)
    while pendientes and (time.time() - arranque) < limite:
        problemas = {}
        pendientes = []
        for ip in equipos:
            base = f"{esquema}://{ip}:{puerto}"
            try:
                if requests.get(f"{base}/rest/system/identity", auth=autenticacion,
                                timeout=5).status_code != 200:
                    pendientes.append(ip)
                    continue
            except Exception:
                pendientes.append(ip)
                continue
            suyos = funciona(sesion, base, autenticacion)
            if suyos:
                problemas[ip] = suyos
        pendientes = pendientes + list(problemas.keys())
        if pendientes:
            time.sleep(5)

    if pendientes or problemas:
        print(f"FALLO: tras {limite} s hay equipos de red que no están listos:")
        for ip in equipos:
            if ip in problemas:
                for p in problemas[ip]:
                    print(f"  {ip}: {p}")
            elif ip in pendientes:
                print(f"  {ip}: no contesta su API")
        return 1

    print(
        f"OK: los {len(equipos)} equipos de red contestan su API Y funcionan "
        f"(puentes en marcha, direcciones vivas, rutas activas), tras "
        f"{time.time() - arranque:.0f} s."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
