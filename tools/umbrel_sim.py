#!/usr/bin/env python3
"""
Install an app from this store the way umbrelOS does, check it actually works, then
remove it. Used by CI, by the update bot before it ships a new version, and by you:

    python3 tools/umbrel_sim.py canopy-linkding            # install, check, remove
    python3 tools/umbrel_sim.py canopy-linkding --keep     # leave it running to poke at
    python3 tools/umbrel_sim.py canopy-linkding --down     # remove a kept install

What it reproduces from umbrelOS:
  * the app folder is copied to app-data/<app-id> and exposed as ${APP_DATA_DIR}
  * ${UMBREL_ROOT}/data/storage/downloads exists (the Files app's Downloads folder)
  * containers are named <app-id>_<service>_1 (compose --compatibility)
  * the app_proxy service is replaced by a check against APP_HOST:APP_PORT
A pass means: every long-running container stays up (no crash loops), one-shot
containers exit 0, and the web UI answers with a non-5xx status within the timeout.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM_ROOT = os.environ.get("UMBREL_SIM_ROOT", os.path.join(tempfile.gettempdir(), "umbrel-sim"))
CHECK_IMAGE = "curlimages/curl:8.17.0"


def log(msg):
    print(f"[sim] {msg}", flush=True)


def run(cmd, check=True, capture=False, env=None, timeout=None):
    res = subprocess.run(cmd, env=env, text=True, timeout=timeout,
                         stdout=subprocess.PIPE if capture else None,
                         stderr=subprocess.STDOUT if capture else None)
    if check and res.returncode != 0:
        raise SystemExit(f"command failed ({res.returncode}): {' '.join(cmd)}\n{res.stdout or ''}")
    return res


def paths(app_id):
    base = os.path.join(SIM_ROOT, app_id)
    return {
        "base": base,
        "umbrel_root": os.path.join(base, "umbrel"),
        "app_data": os.path.join(base, "umbrel", "app-data", app_id),
        "compose": os.path.join(base, "docker-compose.sim.yml"),
    }


def env_for(app_id, p):
    env = dict(os.environ)
    env.update({
        "APP_DATA_DIR": p["app_data"],
        "UMBREL_ROOT": p["umbrel_root"],
        "DEVICE_HOSTNAME": "umbrel",
        "DEVICE_DOMAIN_NAME": "umbrel.local",
        "TZ": "Etc/UTC",
        "APP_PASSWORD": "sim-app-password",
        "APP_SEED": "0" * 64,
        "APP_DOMAIN": "umbrel.local",
    })
    return env


def prepare(app_id):
    src = os.path.join(ROOT, app_id)
    if not os.path.isdir(src):
        raise SystemExit(f"no app folder named {app_id}")
    p = paths(app_id)
    if os.path.exists(p["base"]):
        down(app_id, quiet=True)
        shutil.rmtree(p["base"], ignore_errors=True)
    os.makedirs(os.path.join(p["umbrel_root"], "data", "storage", "downloads"), exist_ok=True)
    shutil.copytree(src, p["app_data"])
    # umbrelOS runs apps as uid 1000; make the copied data writable the same way
    run(["chown", "-R", "1000:1000", p["umbrel_root"]], check=False)

    with open(os.path.join(src, "docker-compose.yml")) as fh:
        compose = yaml.safe_load(fh)
    proxy = (compose.get("services") or {}).pop("app_proxy", None) or {}
    penv = proxy.get("environment") or {}
    if isinstance(penv, list):
        penv = dict(x.split("=", 1) for x in penv)
    target = (str(penv.get("APP_HOST", "")), str(penv.get("APP_PORT", "")))
    compose.pop("version", None)
    with open(p["compose"], "w") as fh:
        yaml.safe_dump(compose, fh, sort_keys=False)
    return p, target, compose


def compose_cmd(app_id, p, *args):
    return ["docker", "compose", "--compatibility", "-p", app_id, "-f", p["compose"],
            "--project-directory", p["app_data"], *args]


def containers(app_id):
    res = run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={app_id}",
               "--format", "{{json .}}"], capture=True, check=False)
    out = []
    for line in (res.stdout or "").splitlines():
        try:
            c = json.loads(line)
        except ValueError:
            continue
        info = json.loads(run(["docker", "inspect", c["ID"]], capture=True).stdout)[0]
        out.append({
            "name": info["Name"].lstrip("/"),
            "service": info["Config"]["Labels"].get("com.docker.compose.service"),
            "running": info["State"]["Running"],
            "exit": info["State"]["ExitCode"],
            "restarts": info.get("RestartCount", 0),
            "status": info["State"]["Status"],
            "networks": list((info["NetworkSettings"].get("Networks") or {}).keys()),
        })
    return out


def probe(app_id, host, port, path="/", public_port=None):
    """HTTP status of http://host:port/ seen from a container on the app's default network,
    sent with the Host header umbrelOS's app proxy passes through (umbrel.local:<port>)."""
    network = f"{app_id}_default"
    host_header = f"umbrel.local:{public_port}" if public_port else "umbrel.local"
    res = run(["docker", "run", "--rm", "--network", network, CHECK_IMAGE, "-s", "-o", "/dev/null",
               "-H", f"Host: {host_header}", "-m", "10", "-w", "%{http_code}", f"http://{host}:{port}{path}"],
              capture=True, check=False)
    try:
        return int((res.stdout or "0").strip()[-3:])
    except ValueError:
        return 0


def host_has_ipv6():
    return os.path.exists("/proc/net/if_inet6")


def ipv6_only_failure(container):
    res = run(["docker", "logs", "--tail", "30", container], capture=True, check=False)
    return "Address family not supported by protocol" in (res.stdout or "")


def one_shot_services(compose):
    return {name for name, svc in (compose.get("services") or {}).items()
            if str(svc.get("restart", "")).strip('"') == "no"}


def manifest_port(app_id):
    with open(os.path.join(ROOT, app_id, "umbrel-app.yml")) as fh:
        return (yaml.safe_load(fh) or {}).get("port")


def check(app_id, p, target, compose, timeout):
    host, port = target
    public_port = manifest_port(app_id)
    if not host or not port:
        raise SystemExit("app_proxy needs APP_HOST and APP_PORT")
    one_shot = one_shot_services(compose)
    deadline = time.time() + timeout
    code = 0
    stable_since = None
    while time.time() < deadline:
        cs = containers(app_id)
        crashed = [c for c in cs if c["service"] not in one_shot and
                   (c["status"] in ("exited", "dead") or c["restarts"] >= 3)]
        failed_init = [c for c in cs if c["service"] in one_shot and c["status"] == "exited" and c["exit"] != 0]
        if crashed or failed_init:
            if not host_has_ipv6() and any(ipv6_only_failure(c["name"]) for c in crashed + failed_init):
                log(f"SKIP {app_id}: it listens on IPv6, which this machine's kernel doesn't have "
                    "(umbrelOS and GitHub's runners do). Re-run it somewhere with IPv6.")
                return None
            for c in crashed + failed_init:
                log(f"FAIL {c['name']} is {c['status']} (exit {c['exit']}, restarts {c['restarts']})")
                run(["docker", "logs", "--tail", "60", c["name"]], check=False)
            return False
        code = probe(app_id, host, port, public_port=public_port)
        if 0 < code < 500:
            stable_since = stable_since or time.time()
            # stay up for 20 seconds to catch apps that answer once then crash
            if time.time() - stable_since >= 20:
                log(f"PASS {app_id}: http://{host}:{port}/ answered {code}, all containers healthy")
                return True
        else:
            stable_since = None
        time.sleep(5)
    log(f"FAIL {app_id}: http://{host}:{port}/ didn't answer in {timeout}s (last status {code})")
    for c in containers(app_id):
        log(f"  {c['name']}: {c['status']} exit={c['exit']} restarts={c['restarts']}")
        run(["docker", "logs", "--tail", "40", c["name"]], check=False)
    return False


def up(app_id, timeout):
    p, target, compose = prepare(app_id)
    env = env_for(app_id, p)
    log(f"installing {app_id} (data in {p['app_data']})")
    run(compose_cmd(app_id, p, "pull", "--quiet"), env=env, timeout=3600)
    run(compose_cmd(app_id, p, "up", "-d"), env=env, check=False, timeout=900)
    return check(app_id, p, target, compose, timeout)


def down(app_id, quiet=False):
    p = paths(app_id)
    if os.path.exists(p["compose"]):
        env = env_for(app_id, p)
        run(compose_cmd(app_id, p, "down", "-v", "--remove-orphans", "-t", "5"), env=env, check=False,
            capture=quiet)
    shutil.rmtree(p["base"], ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("app")
    ap.add_argument("--keep", action="store_true", help="leave the app running")
    ap.add_argument("--down", action="store_true", help="remove a kept install")
    ap.add_argument("--timeout", type=int, default=int(os.environ.get("SIM_TIMEOUT", "300")))
    args = ap.parse_args()
    if args.down:
        down(args.app)
        return 0
    good = False
    try:
        good = up(args.app, args.timeout)
    finally:
        if not args.keep:
            down(args.app, quiet=True)
    if good is None:
        return 3  # skipped: this machine can't run the check
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
