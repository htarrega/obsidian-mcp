"""Fake ``ob`` (Obsidian Headless 0.0.14) CLI for tests.

Installed as an executable named ``ob`` by the ``fake_ob`` fixture. Behaviour
is controlled through environment variables (inherited from the test
process), ``<SUB>`` being the subcommand upper-cased with ``-`` → ``_``
(``SYNC_STATUS``, ``SYNC_CONFIG``, ``VERSION``…):

FAKE_OB_LOG            append one JSON line per invocation: argv, pid, cwd,
                       and ``sync_running`` (a sync child was alive)
FAKE_OB_STATE          JSON file holding the sync config (read by
                       sync-status, rewritten by sync-config)
FAKE_OB_EXIT_<SUB>     exit code
FAKE_OB_STDOUT_<SUB>   replace normal stdout with this text
FAKE_OB_STDERR_<SUB>   write this text to stderr
FAKE_OB_SLEEP_<SUB>    sleep this many seconds first
FAKE_OB_BIG_<SUB>      print this many bytes of filler
FAKE_OB_SYNC_LINES     newline-separated lines printed by ``sync``
FAKE_OB_SYNC_EXIT      ``sync`` exits with this code after its lines
FAKE_OB_SYNC_EXIT_ONCE when set to a path: only exit (FAKE_OB_SYNC_EXIT) on
                       the first run, i.e. while that file does not exist
FAKE_OB_SYNC_PIDS      directory: ``sync`` keeps ``<pid>`` there while alive
FAKE_OB_SYNC_IGNORE_TERM  ``sync`` ignores SIGTERM
"""

import json
import os
import signal
import sys
import time

DEFAULT_STATE = {
    "vaultId": "0123456789abcdef0123456789abcdef",
    "vaultName": "Test Vault",
    "vaultPath": None,
    "syncMode": "bidirectional",
    "conflictStrategy": "merge",
    "deviceName": "test-box",
    "configDir": ".obsidian",
    "fileTypes": ["image"],
    "configs": ["app"],
    "excludedFolders": [],
    "encryptionKey": "SuperSecretEncryptionKeyValue123",
}

args = sys.argv[1:]
sub = args[0] if args else ""
key = sub.lstrip("-").upper().replace("-", "_")
env = os.environ


def sync_running():
    pids_dir = env.get("FAKE_OB_SYNC_PIDS")
    return bool(pids_dir and os.path.isdir(pids_dir) and os.listdir(pids_dir))


if env.get("FAKE_OB_LOG"):
    with open(env["FAKE_OB_LOG"], "a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "argv": args,
                    "pid": os.getpid(),
                    "cwd": os.getcwd(),
                    "sync_running": sync_running() if sub != "sync" else None,
                }
            )
            + "\n"
        )

if env.get(f"FAKE_OB_SLEEP_{key}"):
    time.sleep(float(env[f"FAKE_OB_SLEEP_{key}"]))


def load_state():
    path = env.get("FAKE_OB_STATE")
    state = dict(DEFAULT_STATE, vaultPath=os.getcwd())
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            state.update(json.load(handle))
    return state


def finish(default_stdout=""):
    if env.get(f"FAKE_OB_BIG_{key}"):
        sys.stdout.write("x" * int(env[f"FAKE_OB_BIG_{key}"]))
    sys.stdout.write(env.get(f"FAKE_OB_STDOUT_{key}", default_stdout))
    sys.stderr.write(env.get(f"FAKE_OB_STDERR_{key}", ""))
    sys.stdout.flush()
    sys.exit(int(env.get(f"FAKE_OB_EXIT_{key}", "0")))


def flag_value(name):
    if name in args:
        return args[args.index(name) + 1]
    return None


def run_sync():
    pids_dir = env.get("FAKE_OB_SYNC_PIDS")
    pid_file = None
    if pids_dir:
        os.makedirs(pids_dir, exist_ok=True)
        pid_file = os.path.join(pids_dir, str(os.getpid()))
        with open(pid_file, "w") as handle:
            handle.write("x")

    def cleanup():
        if pid_file and os.path.exists(pid_file):
            os.remove(pid_file)

    def on_term(signum, frame):
        print("Received SIGTERM, releasing lock", flush=True)
        cleanup()
        sys.exit(0)

    if env.get("FAKE_OB_SYNC_IGNORE_TERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    else:
        signal.signal(signal.SIGTERM, on_term)

    print(f"Starting sync: {flag_value('--path')}", flush=True)
    for line in env.get("FAKE_OB_SYNC_LINES", "").splitlines():
        if line:
            print(line, flush=True)
    exit_code = env.get("FAKE_OB_SYNC_EXIT")
    once = env.get("FAKE_OB_SYNC_EXIT_ONCE")
    if exit_code is not None and (not once or not os.path.exists(once)):
        if once:
            with open(once, "w") as handle:
                handle.write("x")
        cleanup()
        sys.exit(int(exit_code))
    while True:
        time.sleep(0.05)


if sub == "--version":
    finish("0.0.14\n")
elif sub == "sync":
    run_sync()
elif sub == "sync-status":
    finish(json.dumps(load_state()))
elif sub == "sync-list-local":
    local_path = env.get("FAKE_OB_LOCAL_PATH", os.getcwd())
    finish(
        json.dumps(
            {
                "vaults": [
                    {"id": "fedcba9876543210fedcba9876543210", "path": "/elsewhere", "host": "h"},
                    {"id": "0123456789abcdef0123456789abcdef", "path": local_path, "host": "h"},
                ]
            }
        )
    )
elif sub == "sync-list-remote":
    finish(
        json.dumps(
            {
                "vaults": [
                    {"id": "0123456789abcdef0123456789abcdef", "name": "Test Vault", "region": "eu"}
                ],
                "shared": [],
            }
        )
    )
elif sub == "sync-config":
    state = load_state()
    mapping = {
        "--excluded-folders": "excludedFolders",
        "--file-types": "fileTypes",
        "--configs": "configs",
    }
    for flag, field in mapping.items():
        value = flag_value(flag)
        if value is not None:
            state[field] = [item for item in value.split(",") if item]
    strategy = flag_value("--conflict-strategy")
    if strategy is not None:
        state["conflictStrategy"] = strategy
    if env.get("FAKE_OB_STATE"):
        with open(env["FAKE_OB_STATE"], "w", encoding="utf-8") as handle:
            json.dump(state, handle)
    finish(json.dumps(state) if "--json" in args else "Updated\n")
else:
    sys.stderr.write(f"unknown command {sub}\n")
    sys.exit(64)
