#!/usr/bin/env python3
"""The framework's own test: does it still behave like a convergent system?

Two properties hold this framework up, and both are easy to lose without
noticing, because a regression in either one still produces a green run:

  1. A run over an already-converged lab changes NOTHING. If a role starts
     reporting `changed` every time, the run is still green and the property is
     gone — and with it the only cheap signal that the declaration is the truth.
  2. A run COVERS every declared guest. With `serial`, one guest that fails its
     reachability check stops the remaining batches, and the recap simply lists
     fewer hosts: a failure that looks like a faster run.

  `invariant` runs a verb twice and asserts both, using Ansible's own JSON
  callback rather than parsing the human recap, and comparing the two host sets
  rather than trusting a count.

  `drill` proves the framework can still SEE and REPAIR a difference, which is
  the one thing no amount of `changed=0` can prove:

      preflight -> no DRIFT
      mutate one guest out of band, through the PVE API
      preflight -> that guest must report DRIFT
      apply     -> must change something and fail nothing
      preflight -> no DRIFT again

  The mutation is a `description` on a container: the engine compares it, it
  needs no restart, and the declaration converges it back — so the drill is its
  own cleanup and leaves the lab exactly as it found it.

Run it from anywhere:  python3 playbooks/tools/framework_check.py invariant
                       python3 playbooks/tools/framework_check.py drill
"""

import argparse
import json
import os
import re
import subprocess
import sys
from typing import NoReturn

import requests
import yaml

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MANIFEST = os.path.join(REPO, "inventory/group_vars/all.yml")
SECRETS = os.path.join(REPO, "playbooks/vars/secrets.yml")
# La declaración de RouterOS vive en los valores por defecto del rol: la lista de
# menús que el framework reclama como suyos es lo que da contra qué comparar.
ROUTEROS_DEFAULTS = os.path.join(REPO, "configure/roles/routeros/defaults/main.yml")
# Where this script's own runs log. They must not share logs/ansible.log: the
# JSON callback is orders of magnitude larger than the human output, and mixing
# the two is what made that file unreadable. See run_verb().
FRAMEWORK_LOG = os.path.join(REPO, "logs/framework-check.log")

# `141: IN SYNC | exists` / `142: DRIFT | ...` — the line both verbs print per
# guest. The drill reads the framework's own verdict instead of re-deriving it.
_VERDICT = re.compile(r"^(\d+): (IN SYNC|DRIFT)\b", re.MULTILINE)


def say(message):
    print(message, flush=True)


def fail(message) -> NoReturn:
    say(f"\nFAIL: {message}")
    sys.exit(1)


def run_verb(verb):
    """Run a verb and return (returncode, {host: recap}, [task messages]).

    The JSON callback is used instead of the human recap on purpose: the recap is
    a formatted table that a count can be read out of but a host LIST cannot be
    trusted from, and telling "the guest was skipped" apart from "the guest was
    never in the play" is exactly what this test is for.
    """
    env = dict(os.environ)
    env["ANSIBLE_STDOUT_CALLBACK"] = "json"
    env["ANSIBLE_LOAD_CALLBACK_PLUGINS"] = "1"
    # Do NOT inherit the human run log. The JSON callback emits megabytes per run,
    # and this script already reads that output from stdout — so letting it land
    # in logs/ansible.log (append-only, never rotated by Ansible) is how that file
    # reached 78 MB, 99% of it machine output nobody reads. Send it to its own
    # file instead: the human log stays readable, and a failed run is still on
    # disk to debug. The file is covered by pve_log_retention like the others.
    env["ANSIBLE_LOG_PATH"] = FRAMEWORK_LOG
    proc = subprocess.run(
        ["ansible-playbook", verb], cwd=REPO, env=env, capture_output=True, text=True
    )
    start = proc.stdout.find("{")
    if start < 0:
        fail(f"`ansible-playbook {verb}` produced no JSON at all. stderr:\n{proc.stderr[-2000:]}")
    try:
        data = json.loads(proc.stdout[start:])
    except json.JSONDecodeError as exc:
        fail(f"could not parse the JSON callback output of {verb}: {exc}")

    recap, messages = {}, []
    # ansible-core 2.19 puts the run's recap at the TOP level of the callback
    # output; older versions nested it per play. Read both, so the check does not
    # silently see an empty recap on one of them — which is exactly how it failed
    # the first time it was written.
    for source in [data.get("stats") or {}] + [p.get("stats") or {} for p in data.get("plays") or []]:
        for host, stats in source.items():
            recap[host] = stats
    for play in data.get("plays") or []:
        for task in play.get("tasks") or []:
            for result in (task.get("hosts") or {}).values():
                messages.extend(_messages_in(result))
    if not recap:
        fail(
            f"`ansible-playbook {verb}` ran but its recap is empty, so nothing can be "
            f"asserted about host coverage. The callback output did not carry a per-host "
            f"recap where this script looks for it."
        )
    return proc.returncode, recap, messages


def assert_clean(recap, label, allow_changes):
    """A run is clean when nothing failed, nothing was unreachable, and — for a
    run over an already-converged lab — nothing changed."""
    problems = []
    for host in sorted(recap):
        stats = recap[host]
        if stats.get("unreachable"):
            problems.append(f"{host}: {stats['unreachable']} unreachable")
        if stats.get("failures"):
            problems.append(f"{host}: {stats['failures']} failure(s)")
        if not allow_changes and stats.get("changed"):
            problems.append(
                f"{host}: {stats['changed']} change(s) on a run that should have changed nothing"
            )
    if problems:
        fail(f"{label} is not clean:\n  " + "\n  ".join(problems))
    say(f"  ok  {label}: {len(recap)} host(s), "
        f"{'changes allowed' if allow_changes else 'changed=0'}, no failures")


def _messages_in(result):
    """Every message a task result carries, including its per-item ones.

    A task with `loop` does NOT put its message at the top of the result: it
    nests one result per item under `results`. A check that only reads the top
    level therefore finds nothing at all for exactly the tasks that report per
    guest — which is how the drill failed the first time it was run.
    """
    if not isinstance(result, dict):
        return []
    found = [str(result["msg"])] if result.get("msg") else []
    for item in result.get("results") or []:
        if isinstance(item, dict) and item.get("msg"):
            found.append(str(item["msg"]))
    return found


def drift_verdicts(messages):
    return {vmid: verdict for vmid, verdict in _VERDICT.findall("\n".join(messages))}


def invariant(verb):
    say(f"== invariant: `{verb}` twice ==")
    say(f"  run 1/2 (this one is allowed to change things)")
    rc, first, _ = run_verb(verb)
    if rc != 0:
        fail(f"{verb} exited {rc} on the first run")
    assert_clean(first, "run 1", allow_changes=True)

    say(f"  run 2/2 (this one must change nothing)")
    rc, second, _ = run_verb(verb)
    if rc != 0:
        fail(f"{verb} exited {rc} on the second run")
    assert_clean(second, "run 2", allow_changes=False)

    if set(first) != set(second):
        only_first = sorted(set(first) - set(second))
        only_second = sorted(set(second) - set(first))
        fail(
            f"the two runs did not cover the same hosts.\n"
            f"  only in run 1: {only_first or '(none)'}\n"
            f"  only in run 2: {only_second or '(none)'}\n"
            f"A host that disappears from the second recap was dropped, not converged."
        )
    say(f"  ok  both runs covered the same {len(first)} host(s)")

    # Do not claim more than was checked. A verb whose play runs on the control
    # node only (apply.yml, preflight.yml) has no guests in its recap, so the
    # coverage assertion above is vacuous for it — and a run that quietly stopped
    # short would look identical. Say so instead of printing a PASS that reads as
    # broader than it is.
    if set(first) <= {"localhost", "127.0.0.1"}:
        say(
            f"  note this verb's play targets the control node, so its recap cannot show "
            f"whether every declared guest was covered. For that, run the verb whose play "
            f"targets the guests:\n"
            f"       python3 playbooks/tools/framework_check.py invariant --verb configure/site.yml"
        )
    say("\nPASS: the verb is convergent" +
        ("" if set(first) <= {"localhost", "127.0.0.1"} else " and covers every host it claims to") +
        ".")
    return 0


def _api():
    creds = yaml.safe_load(open(SECRETS)) or {}
    session = requests.Session()
    session.verify = creds.get("pve_api_ca_path", True)
    session.headers["Authorization"] = (
        f"PVEAPIToken={creds['pve_api_token_id']}={creds['pve_api_token_secret']}"
    )
    return session, f"https://{creds['pve_api_host']}:8006/api2/json"


def drill(verb, preflight):
    say("== drill: can the framework still SEE a difference, and REPAIR it? ==")

    manifest = yaml.safe_load(open(MANIFEST)) or {}
    node = manifest.get("pve_node", "")
    containers = [c for c in (manifest.get("pve_containers") or []) if c.get("vmid")]
    if not containers:
        fail("the declared state has no containers to drill with")
    victim = containers[0]["vmid"]
    session, base = _api()

    say(f"  1. {preflight} (expect no DRIFT)")
    rc, _, messages = run_verb(preflight)
    if rc != 0:
        fail(f"{preflight} exited {rc} before the drill even started")
    verdicts = drift_verdicts(messages)
    if not verdicts:
        fail(
            "no per-guest verdict was found in the preflight output, so the drill cannot "
            "tell DRIFT from IN SYNC. Did the verb stop reporting per guest?"
        )
    if "DRIFT" in verdicts.values():
        fail(
            f"the lab already reports DRIFT before the drill: "
            f"{sorted(k for k, v in verdicts.items() if v == 'DRIFT')}. "
            f"Converge it first — a drill that starts dirty proves nothing."
        )
    say(f"     ok  {len(verdicts)} guest(s), all IN SYNC")

    stamp = f"drift injected by framework_check.py at {os.getpid()}"
    say(f"  2. mutate vmid {victim} out of band (description)")
    # PUT, not POST. PVE updates a container's config with PUT and a VM's with
    # POST — the same conceptual operation, two different verbs — and POST on an
    # LXC answers 501 "Method not implemented".
    response = session.put(
        f"{base}/nodes/{node}/lxc/{victim}/config", data={"description": stamp}, timeout=30
    )
    if response.status_code != 200:
        fail(f"the out-of-band mutation was refused: HTTP {response.status_code} {response.text[:200]}")

    say(f"  3. {preflight} (vmid {victim} must now report DRIFT)")
    rc, _, messages = run_verb(preflight)
    if rc != 0:
        fail(f"{preflight} exited {rc} on the dirty lab; it should report drift, not die")
    verdicts = drift_verdicts(messages)
    if verdicts.get(str(victim)) != "DRIFT":
        fail(
            f"the framework did NOT notice the drift it was just handed: vmid {victim} "
            f"reports {verdicts.get(str(victim))!r}. The declared state has stopped being the "
            f"authority for that guest."
        )
    say(f"     ok  vmid {victim} reports DRIFT")

    say(f"  4. {verb} (must repair it)")
    rc, recap, _ = run_verb(verb)
    if rc != 0:
        fail(f"{verb} exited {rc} while converging the injected drift")
    assert_clean(recap, "converge", allow_changes=True)
    changed = sum(s.get("changed", 0) for s in recap.values())
    if changed == 0:
        fail("apply reported no change at all, so it did not repair the drift it had just seen")
    say(f"     ok  repaired ({changed} change(s) reported)")

    say(f"  5. {preflight} (expect no DRIFT again)")
    rc, _, messages = run_verb(preflight)
    if rc != 0:
        fail(f"{preflight} exited {rc} after the repair")
    verdicts = drift_verdicts(messages)
    dirty = sorted(k for k, v in verdicts.items() if v == "DRIFT")
    if dirty:
        fail(f"still DRIFT after converging: {dirty}")
    say(f"     ok  {len(verdicts)} guest(s), all IN SYNC")

    say("\nPASS: the framework sees an out-of-band change and converges it back.")
    return 0


LABCHECK = os.path.join(REPO, "playbooks/tools/lab-check.sh")


# ---------------------------------------------------------------------------
# `agree`: la comprobación que SÍ puede encontrar lo que falta.
#
# El mapa de lab-check.sh es una copia a mano de parte de la declaración, y por
# eso puede desincronizarse sin que nada lo note: el script no puede detectar
# que su propia lista está mal, porque la lista es su ENTRADA. Un test que sólo
# pregunta por las direcciones que ya tiene nunca echa en falta ninguna — y esa
# fue exactamente la forma del último error: una dirección que era el VMID del
# invitado y no su IP.
#
# La cura es no comparar elemento a elemento, sino CONJUNTOS: derivar de la
# declaración qué direcciones DEBEN existir, derivar del mapa cuáles se van a
# probar, y quedarse con lo que falta en cada lado. La ausencia es el dato.
# ---------------------------------------------------------------------------
def declared_addresses():
    """Las direcciones que la declaración dice que hay. La fuente de verdad."""
    m = yaml.safe_load(open(MANIFEST, encoding="utf-8"))
    out = {}
    for c in m.get("pve_containers", []):
        cfg = c.get("config") or {}
        if "bootstrap" in cfg:
            net0 = (c.get("netif") or {}).get("net0", "")
            ip = re.search(r"\bip=([\d.]+)/", net0)
            if ip:
                out[c["hostname"]] = ip.group(1)
        elif "site" in c and c.get("address"):
            out[c["hostname"]] = c["address"]
    for v in m.get("pve_vms", []):
        if not v.get("chr"):
            continue
        ros = (v.get("config") or {}).get("routeros") or {}
        for a in ros.get("addresses", []):
            if a.get("interface") == "ether1":
                out[v["name"]] = a["address"].split("/")[0]
    return out


def mapped_addresses():
    """Las direcciones que el script va a probar, leídas de su propio mapa."""
    out = {}
    for line in open(LABCHECK, encoding="utf-8"):
        if "LAB=" not in line:
            continue
        for mm in re.finditer(r"([\w-]+) ([\d.]+) \d+ \w+", line):
            out[mm.group(1)] = mm.group(2)
    return out


def agree():
    say("== agree: el mapa del propio test contra la declaración ==")
    decl = declared_addresses()
    mapped = mapped_addresses()
    say(f"  la declaración declara {len(decl)} direcciones; el mapa prueba {len(mapped)}")

    by_addr = {a: n for n, a in decl.items()}
    seen = {a: n for n, a in mapped.items()}
    missing = {a: n for a, n in by_addr.items() if a not in seen}
    extra = {a: n for a, n in seen.items() if a not in by_addr}

    for a, n in sorted(missing.items()):
        say(f"  FALTA en el mapa : {a:<15} ({n}) — declarado, y el script no lo prueba")
    for a, n in sorted(extra.items()):
        say(f"  SOBRA en el mapa : {a:<15} ({n}) — el script prueba algo que nadie declara")

    if missing or extra:
        fail("el mapa del test y la declaración no dicen lo mismo")
    say("\nPASS: el test prueba exactamente lo que la declaración declara.")
    return 0


# --- lo que la declaración no cubre -----------------------------------------
# Los roles convergen lo que la declaración DICE y lo verifican releyéndolo. Eso
# prueba la dirección de ida: lo declarado está. No prueba la de vuelta: si algo
# apareció en el equipo que nadie declaró —un servicio que volvió a abrirse, un
# usuario, un trabajo programado, un cliente DHCP—, el run sale verde igual.
#
# El export de RouterOS lo ve. `POST /rest/export` escribe en el equipo un fichero
# con su configuración desplegada y `GET /rest/file/<f>` lo devuelve entero. Y hay
# un detalle que lo hace especialmente útil: un export NO verboso sólo escribe lo
# que NO es el valor de fábrica, así que es, en sí mismo, la lista de todo lo que
# alguien cambió en ese equipo. Comparado por MENÚS contra los que la declaración
# declara como suyos, señala exactamente lo que hay y nadie reclamó.
#
# Se compara por menús y no línea a línea a propósito: el export es un script
# generado (por la API los `yes/no` salen como `true/false`), así que la línea no
# es una unidad comparable; el menú sí.
def _menu_counts(export):
    """{'ip/address': 3, ...} — los menús del export y cuántas entradas llevan."""
    out = {}
    actual = None
    for line in export.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("/"):
            actual = line.lstrip("/").replace(" ", "/")
            out.setdefault(actual, 0)
        elif actual is not None:
            out[actual] += 1
    return {m: n for m, n in out.items() if n}


def routeros_export():
    say("== export: qué hay en los equipos de red que la declaración no menciona ==")
    manifest = yaml.safe_load(open(MANIFEST)) or {}
    defaults = yaml.safe_load(open(ROUTEROS_DEFAULTS)) or {}
    creds = yaml.safe_load(open(SECRETS)) or {}

    user = defaults.get("routeros_user", "admin")
    password = creds.get("pve_routeros_password", "")
    scheme = defaults.get("routeros_scheme", "http")
    port = defaults.get("routeros_port", 80)
    declared = {t["path"] for t in (defaults.get("routeros_tables") or [])}
    allowed = {m for m in (defaults.get("routeros_undeclared_allowed") or [])}
    forbidden = {m for m in (defaults.get("routeros_forbidden_menus") or [])}

    # Los equipos y su dirección de gestión salen de la declaración: la PRIMERA
    # entrada de `addresses` es la de administración (las demás van a interfaces de
    # datos y pueden traer plantillas, que aquí no se pueden resolver).
    devices = []
    for v in (manifest.get("pve_vms") or []):
        ros = ((v.get("config") or {}).get("routeros") or {})
        addrs = ros.get("addresses") or []
        if not addrs:
            continue
        host = str(addrs[0].get("address", "")).split("/")[0]
        if not host or "{{" in host:
            continue
        devices.append((ros.get("identity") or v.get("name") or str(v.get("vmid")), host))
    if not devices:
        fail("la declaración no tiene ningún equipo RouterOS con dirección de gestión")

    say(f"  {len(devices)} equipo(s) declarados, {len(declared)} menú(s) que la declaración reclama")
    session = requests.Session()
    session.auth = (user, password)
    desconocidos = []
    prohibidos = []

    for name, host in devices:
        base = f"{scheme}://{host}:{port}/rest"
        nombre_fichero = "framework-check.rsc"
        try:
            r = session.post(f"{base}/export", json={"file": nombre_fichero}, timeout=30)
            r.raise_for_status()
            r = session.get(f"{base}/file/{nombre_fichero}", timeout=30)
            r.raise_for_status()
            # La respuesta NO es el texto del fichero: el REST de RouterOS lo
            # devuelve como objeto JSON y el script viaja dentro de `contents`. Leer
            # `.text` tal cual no encuentra ni un menú — y contar cero de cero da
            # verde sin haber mirado nada, que es la peor forma de tener una
            # comprobación. Comprobado contra un equipo real, no deducido.
            try:
                cuerpo = r.json()
                export = cuerpo.get("contents", "") if isinstance(cuerpo, dict) else ""
            except ValueError:
                export = r.text
            if not export.strip():
                fail(f"{name} ({host}): el export volvió vacío, así que no hay nada que comparar")
        except Exception as exc:
            fail(f"{name} ({host}): no se pudo leer el export ({type(exc).__name__}: {exc})")
        finally:
            try:
                session.post(f"{base}/file/remove", json={"numbers": nombre_fichero}, timeout=20)
            except Exception:
                pass

        menus = _menu_counts(export)
        if not menus:
            # Una comprobación que no encuentra nada que comparar no ha comprobado
            # nada: falla, en vez de dar verde.
            fail(f"{name} ({host}): el export no tenía ni un menú con contenido")
        say(f"\n  {name} ({host}): {len(menus)} menú(s) con contenido")
        for m in sorted(menus):
            if m in declared:
                marca = "declarado"
            elif m in allowed:
                marca = "admitido"
            elif m in forbidden:
                marca = "PROHIBIDO"
                prohibidos.append((name, m))
            else:
                marca = "NO DECLARADO"
                desconocidos.append((name, m))
            say(f"    {m:<36} {menus[m]:>3} entrada(s)   {marca}")

    if prohibidos:
        say("")
        for name, m in prohibidos:
            say(f"  PROHIBIDO y presente : {m:<30} en {name}")
        fail("hay un menú que la declaración prohíbe y el equipo tiene")
    if desconocidos:
        say("")
        for name, m in desconocidos:
            say(f"  sin declarar : {m:<36} en {name}")
        fail(
            "hay menús con contenido que la declaración no reclama ni admite. O son "
            "configuración que alguien puso fuera del framework, o son menús que la "
            "declaración debería reclamar. Añádelos a `routeros_undeclared_allowed` "
            "si son esperados, o investiga de dónde salieron."
        )
    say(f"\nPASS: los {len(devices)} equipos sólo tienen lo que la declaración declara o admite.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="mode")
    inv = sub.add_parser("invariant", help="run a verb twice; the second run must change nothing")
    inv.add_argument("--verb", default="playbooks/apply.yml")
    drl = sub.add_parser("drill", help="inject drift, watch preflight see it and apply repair it")
    drl.add_argument("--verb", default="playbooks/apply.yml")
    drl.add_argument("--preflight", default="playbooks/preflight.yml")
    sub.add_parser("agree", help="the check's own map must match the declaration")
    sub.add_parser("export", help="los equipos de red sólo tienen lo que la declaración declara")
    args = parser.parse_args()

    if args.mode == "invariant":
        return invariant(args.verb)
    if args.mode == "drill":
        return drill(args.verb, args.preflight)
    if args.mode == "agree":
        return agree()
    if args.mode == "export":
        return routeros_export()
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
