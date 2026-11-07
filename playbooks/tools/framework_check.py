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


def main():
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="mode")
    inv = sub.add_parser("invariant", help="run a verb twice; the second run must change nothing")
    inv.add_argument("--verb", default="playbooks/apply.yml")
    drl = sub.add_parser("drill", help="inject drift, watch preflight see it and apply repair it")
    drl.add_argument("--verb", default="playbooks/apply.yml")
    drl.add_argument("--preflight", default="playbooks/preflight.yml")
    args = parser.parse_args()

    if args.mode == "invariant":
        return invariant(args.verb)
    if args.mode == "drill":
        return drill(args.verb, args.preflight)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
