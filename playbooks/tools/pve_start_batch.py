#!/usr/bin/env python3
# ===========================================================================
# pve_start_batch.py — arranque optimista, con comprobación y reintento.
#
# Un escalón se pide de golpe, sin limitar trabajadores: si el hipervisor puede
# con todos a la vez, los levanta a todos y el escalón está arriba en un momento.
# Después se comprueba —en UN proceso, no con una tarea de Ansible por invitado—
# y se relanza SÓLO lo que se quedó parado.
#
# Eso es lo que hace falta de verdad, medido y no supuesto: pidiendo siete CHR a
# la vez, cinco fallaron con `qmstart ... start failed: got timeout` en el log del
# hipervisor. Un límite de trabajadores evita el problema, pero paga el precio de
# serializar todo SIEMPRE, incluso cuando el hipervisor habría podido. El
# reintento paga sólo cuando hace falta.
#
# Y distingue dos cosas que no son lo mismo: un invitado PARADO es un fallo y se
# relanza; uno que aún está arrancando se espera. Relanzar a quien ya está
# arrancando es como se provoca un error de bloqueo del fichero de configuración.
#
#   pve_start_batch.py --manifest M --secrets S --timeout 180 --label "order=2" 160 161 ...
#
# Sale 0 si todo acabó en marcha, 1 diciendo qué no, 2 si se usa mal.
# ===========================================================================
import argparse
import json
import os
import sys
import time

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("pve_start_batch.py: PyYAML is required (python3-yaml)")
try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("pve_start_batch.py: the requests library is required")

CRED_ALIASES = {
    "host": ("PROXMOX_HOST", "host", "pve_api_host"),
    "token_id": ("PROXMOX_TOKEN_ID", "token_id", "pve_api_token_id"),
    "token_secret": ("PROXMOX_TOKEN_SECRET", "token_secret", "pve_api_token_secret"),
    "ca_path": ("PROXMOX_CA_PATH", "ca_path", "pve_api_ca_path"),
}

# Los estados que NO son «parado» y por los que no se reintenta: el invitado está
# en marcha o en camino, y volver a pedirlo es como se choca con su propio bloqueo.
EN_CAMINO = {"running", "prelaunch", "paused", "suspended"}


def credenciales(args):
    creds = {k: os.environ.get(nombres[0], "") for k, nombres in CRED_ALIASES.items()}
    if args.secrets and os.path.exists(args.secrets):
        datos = yaml.safe_load(open(args.secrets)) or {}
        for clave, nombres in CRED_ALIASES.items():
            for nombre in nombres:
                if datos.get(nombre):
                    creds[clave] = str(datos[nombre])
                    break
    faltan = [k for k, v in creds.items() if not v and k != "ca_path"]
    if faltan:
        sys.exit(f"pve_start_batch.py: faltan credenciales: {', '.join(faltan)}")
    return creds


def tipos(manifest):
    """{vmid: 'lxc'|'qemu'} leído de la declaración, que es la fuente de verdad."""
    datos = yaml.safe_load(open(manifest)) or {}
    mapa = {}
    for clave, tipo in (("pve_containers", "lxc"), ("pve_vms", "qemu")):
        for item in datos.get(clave) or []:
            if isinstance(item, dict) and item.get("vmid") is not None:
                mapa[int(item["vmid"])] = tipo
    return datos, mapa


def estado(sesion, base, tipo, vmid):
    """El estado, o None si no se puede leer (y entonces no se toca)."""
    try:
        r = sesion.get(f"{base}/{tipo}/{vmid}/status/current", timeout=30)
        if r.status_code >= 400:
            return None
        return (r.json().get("data") or {}).get("status", "")
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description="Arranca un lote y asegura que levanta.")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--secrets", help="YAML con las credenciales")
    ap.add_argument("--timeout", type=int, default=180, help="segundos de margen total")
    ap.add_argument("--label", default="", help="nombre del escalón, para el mensaje")
    ap.add_argument("--espera", type=int, default=8, help="segundos entre comprobaciones")
    ap.add_argument("vmids", nargs="+", type=int)
    args = ap.parse_args()

    creds = credenciales(args)
    datos, mapa = tipos(args.manifest)
    nodo = datos.get("pve_node", "")
    if not nodo:
        sys.exit("pve_start_batch.py: la declaración no dice cuál es el nodo")

    desconocidos = [v for v in args.vmids if v not in mapa]
    if desconocidos:
        sys.exit(
            f"pve_start_batch.py: {desconocidos} no están en la declaración. No se "
            "arranca nada que la declaración no nombre."
        )

    sesion = requests.Session()
    sesion.headers.update({
        "Authorization": f"PVEAPIToken={creds['token_id']}={creds['token_secret']}"})
    sesion.verify = creds["ca_path"] or True
    base = f"https://{creds['host']}:8006/api2/json/nodes/{nodo}"
    etiqueta = f" [{args.label}]" if args.label else ""

    arranque = time.time()
    reintentos = []          # quién hizo falta relanzar, en orden
    ultimos = {}

    # Primera petición: el lote ENTERO de golpe, sin límite de trabajadores.
    peticion = sesion.post(f"{base}/startall", timeout=60,
                           data={"vms": ",".join(str(v) for v in args.vmids)})
    if peticion.status_code >= 400:
        sys.exit(f"pve_start_batch.py: startall devolvió HTTP {peticion.status_code}: {peticion.text[:200]}")

    while True:
        time.sleep(args.espera)
        ultimos = {v: estado(sesion, base, mapa[v], v) for v in args.vmids}
        parados = [v for v, e in ultimos.items() if e == "stopped"]
        ilegibles = [v for v, e in ultimos.items() if e is None]

        if not parados and not ilegibles:
            listos = [v for v, e in ultimos.items() if e in EN_CAMINO]
            segundos = time.time() - arranque
            extra = f", {len(reintentos)} relanzado(s): {', '.join(str(v) for v in reintentos)}" if reintentos else ""
            print(f"OK{etiqueta}: {len(listos)}/{len(args.vmids)} en marcha tras {segundos:.0f} s{extra}")
            return 0

        if (time.time() - arranque) >= args.timeout:
            colgados = parados + ilegibles
            print(
                f"FALLO{etiqueta}: tras {args.timeout} s siguen sin arrancar "
                f"{', '.join(str(v) for v in colgados)}. Estados: "
                + json.dumps({str(v): ultimos.get(v) for v in colgados})
            )
            return 1

        if parados:
            # Sólo los PARADOS: son los que de verdad fallaron. Relanzarlos es lo
            # que convierte un «arranca cinco de siete» en un «arranca los siete».
            reintentos.extend(v for v in parados if v not in reintentos)
            sesion.post(f"{base}/startall", timeout=60,
                        data={"vms": ",".join(str(v) for v in parados)})


if __name__ == "__main__":
    sys.exit(main())
