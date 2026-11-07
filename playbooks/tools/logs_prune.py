#!/usr/bin/env python3
"""Retention for `logs/` — the only directory in this repository that grows without bound.

Ansible's `log_path` is append-only and Ansible never rotates it, so one file
accumulates every run forever. Two things bound that:

  * a live file larger than `live_max_mb` is ROTATED into `logs/archive/` and
    emptied, so no single file grows without limit;
  * archives older than `keep_days`, or beyond `keep_count`, are DELETED, so the
    directory cannot grow without limit either.

The policy lives in `inventory/group_vars/all.yml` with the rest of the declared
state, and every verb applies it before it does anything — so a lab driven only
through the verbs never needs an operator. Run it standalone for the case where
nobody runs the framework for days:

    python3 playbooks/tools/logs_prune.py --dry-run
    python3 playbooks/tools/logs_prune.py

Why copy-and-truncate rather than rename: Ansible holds the log file open for the
whole run (Python's logging opens it in append mode), so renaming it would leave
Ansible writing into the archive while the live path stays empty. Copying the
contents out and truncating the original keeps the writer's descriptor valid. The
journal is opened and closed per write, so it would tolerate a rename — both files
take the same path deliberately, one behaviour to reason about.

The window between the copy and the truncate is where a line can be lost if
another run is writing at that instant. The verbs prune BEFORE they start, so in
practice there is no concurrent writer; two simultaneous runs is the only case
where a line can go missing, and a missing log line is not worth a lock.

Exit code is 0 even when it did nothing, so the verbs can call it on every run.
It exits non-zero only when it could not do its job, which is worth failing on.
"""

import argparse
import json
import os
import shutil
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MANIFEST = os.path.join(REPO, "inventory/group_vars/all.yml")
LOG_DIR = os.path.join(REPO, "logs")
ARCHIVE_DIR = os.path.join(LOG_DIR, "archive")

# The files that grow, relative to logs/. This list is framework knowledge, not
# lab policy, so it lives here next to the writers rather than in the manifest:
#   ansible.log         ansible.cfg -> log_path (the human log of every verb)
#   pve-api.jsonl       playbooks/tracing/sitecustomize.py (the API journal)
#   framework-check.log framework_check.py, which points its own runs here so
#                       their megabyte JSON output stops landing in the human log
LIVE_FILES = ("ansible.log", "pve-api.jsonl", "framework-check.log")

# Fallbacks, used only when the manifest does not carry the policy at all.
DEFAULTS = {"live_max_mb": 8, "keep_days": 14, "keep_count": 10}


def say(message):
    print(message, flush=True)


def warn(message):
    """Diagnostics go to stderr, never stdout: the verbs parse stdout as JSON."""
    print(message, file=sys.stderr, flush=True)


def policy_from_manifest():
    """Read `pve_log_retention` from the declared state, with defaults under it."""
    policy = dict(DEFAULTS)
    try:
        import yaml
    except ImportError as exc:
        warn(f"logs_prune: cannot read the manifest ({exc}); using the built-in defaults")
        return policy
    try:
        with open(MANIFEST, encoding="utf-8") as handle:
            declared = (yaml.safe_load(handle) or {}).get("pve_log_retention") or {}
    except OSError as exc:
        warn(f"logs_prune: cannot read {MANIFEST} ({exc}); using the built-in defaults")
        return policy
    for key in DEFAULTS:
        if key in declared and declared[key] is not None:
            policy[key] = declared[key]
    return policy


def stamp():
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime())


def rotate(name, cap_bytes, dry_run):
    """Copy a live file into the archive and empty it. Returns the archive path or None."""
    live = os.path.join(LOG_DIR, name)
    try:
        size = os.path.getsize(live)
    except OSError:
        return None  # not written yet
    if size <= cap_bytes:
        return None

    archived = os.path.join(ARCHIVE_DIR, f"{name}.{stamp()}")
    suffix = 1
    while os.path.exists(archived):  # two rotations inside one second
        archived = os.path.join(ARCHIVE_DIR, f"{name}.{stamp()}.{suffix}")
        suffix += 1
    if dry_run:
        return archived

    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    # Copy, then truncate in place: the writer's open descriptor stays valid and
    # keeps appending to the same inode. See the note at the top of this file.
    shutil.copyfile(live, archived)
    with open(live, "w", encoding="utf-8"):
        pass
    return archived


def archives():
    """Every archive this script owns, newest first. Anything it does not
    recognise is left strictly alone — a retention tool that deletes by wildcard
    in a directory is how someone loses a file that was not a log."""
    try:
        names = os.listdir(ARCHIVE_DIR)
    except OSError:
        return []
    owned = []
    for name in names:
        if not name.startswith(tuple(f"{live}." for live in LIVE_FILES)):
            continue
        path = os.path.join(ARCHIVE_DIR, name)
        if os.path.isfile(path):
            owned.append(path)
    return sorted(owned, key=lambda p: os.path.getmtime(p), reverse=True)


def prune(keep_days, keep_count, dry_run):
    """Delete archives that are too old, or beyond keep_count. Newest first."""
    now = time.time()
    max_age = keep_days * 86400
    doomed = []
    survivors = []
    for index, path in enumerate(archives()):  # newest first
        # The newest archive is never deleted, whatever the policy says: it is the
        # log someone would actually reach for, and "keep 0" is not a policy.
        if index == 0:
            survivors.append(path)
            continue
        too_old = keep_days >= 0 and (now - os.path.getmtime(path)) > max_age
        beyond_count = index >= keep_count
        if too_old or beyond_count:
            doomed.append(path)
        else:
            survivors.append(path)
    if not dry_run:
        for path in doomed:
            try:
                os.remove(path)
            except OSError as exc:
                warn(f"logs_prune: could not delete {path}: {exc}")
                return doomed, survivors, 1
    return doomed, survivors, 0


def human_bytes(count):
    return f"{count / 1048576:.1f} MB" if count >= 1048576 else f"{count / 1024:.0f} KB"


def main():
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="say what would happen, change nothing")
    parser.add_argument("--json", action="store_true", help="machine-readable result (the verbs read this)")
    parser.add_argument("--live-max-mb", type=float, help="override the declared rotate threshold")
    parser.add_argument("--keep-days", type=int, help="override the declared maximum age")
    parser.add_argument("--keep-count", type=int, help="override the declared maximum number kept")
    args = parser.parse_args()

    policy = policy_from_manifest()
    if args.live_max_mb is not None:
        policy["live_max_mb"] = args.live_max_mb
    if args.keep_days is not None:
        policy["keep_days"] = args.keep_days
    if args.keep_count is not None:
        policy["keep_count"] = args.keep_count
    cap = int(policy["live_max_mb"] * 1048576)
    keep_count = max(1, int(policy["keep_count"]))  # never keep zero: always leave the newest

    rotated = []
    for name in LIVE_FILES:
        result = rotate(name, cap, args.dry_run)
        if result:
            rotated.append(os.path.relpath(result, REPO))

    doomed, survivors, status = prune(int(policy["keep_days"]), keep_count, args.dry_run)

    total = 0
    for name in LIVE_FILES:
        try:
            total += os.path.getsize(os.path.join(LOG_DIR, name))
        except OSError:
            pass
    for path in survivors:
        try:
            total += os.path.getsize(path)
        except OSError:
            pass

    result = {
        "dry_run": args.dry_run,
        "policy": {k: policy[k] for k in DEFAULTS},
        "rotated": [os.path.relpath(p, REPO) for p in rotated],
        "pruned": [os.path.relpath(p, REPO) for p in doomed],
        "kept": len(survivors),
        "total_bytes": total,
        "changed": bool(rotated or doomed),
    }

    if args.json:
        say(json.dumps(result, sort_keys=True))
    else:
        verb = "would rotate" if args.dry_run else "rotated"
        say(
            f"policy: rotate over {policy['live_max_mb']} MB, keep {policy['keep_days']} days "
            f"and at most {policy['keep_count']} archives"
        )
        say(f"  {verb}: {result['rotated'] or '(nothing)'}")
        say(f"  {'would delete' if args.dry_run else 'deleted'}: {result['pruned'] or '(nothing)'}")
        say(f"  kept {len(survivors)} archive(s); logs/ now holds {human_bytes(total)}")
    return status


if __name__ == "__main__":
    sys.exit(main())
