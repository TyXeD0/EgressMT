#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
from typing import Any

VERSION = "0.1.0-rc1"
PROVISIONER = Path("/usr/local/libexec/mtproxyl-egress-provision")
STATE_DIR = Path("/var/lib/mtproxyl-egress/jobs")
RUN_DIR = Path("/run/mtproxyl-egress/panel-jobs")
JOB_RE = re.compile(r"^j-[0-9a-f]{16}$")


def now() -> int: return int(time.time())


def atomic_json(path: Path, data: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2); f.write("\n"); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try: os.unlink(tmp)
        except FileNotFoundError: pass


def job_path(job_id: str) -> Path:
    if not JOB_RE.fullmatch(job_id): raise ValueError("invalid job id")
    return STATE_DIR / f"{job_id}.json"


def read_job(job_id: str) -> dict[str, Any]:
    p = job_path(job_id)
    if not p.is_file(): raise FileNotFoundError("job not found")
    return json.loads(p.read_text(encoding="utf-8"))


def write_job(job_id: str, **changes: Any) -> dict[str, Any]:
    try: data = read_job(job_id)
    except FileNotFoundError: data = {"id": job_id}
    data.update(changes); data["updated_at"] = now(); atomic_json(job_path(job_id), data); return data


def public_action(action: str) -> tuple[str, str]:
    if action == "add": return "add", "Добавление EXIT-ноды"
    if action == "remove": return "remove", "Удаление EXIT-ноды"
    if action == "upgrade": return "upgrade", "Обновление транспорта EXIT-ноды до AWG 3.1"
    raise ValueError("action must be add/remove/upgrade")


def start_job() -> None:
    request = json.load(sys.stdin); action, label = public_action(str(request.get("action") or "")); job_id = "j-" + secrets.token_hex(8)
    RUN_DIR.mkdir(parents=True, exist_ok=True, mode=0o700); STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(RUN_DIR, 0o700); os.chmod(STATE_DIR, 0o700)
    req_path = RUN_DIR / f"{job_id}.request"; atomic_json(req_path, request, 0o600)
    status = {"id":job_id,"action":action,"label":label,"state":"queued","stage":"queued","message":"Операция поставлена в очередь","created_at":now(),"updated_at":now(),"finished_at":None,"result":None,"error":None}
    atomic_json(job_path(job_id), status)
    try:
        subprocess.Popen([sys.executable,__file__,"run",job_id,str(req_path)],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True,close_fds=True)
    except Exception:
        try: req_path.unlink()
        except FileNotFoundError: pass
        raise
    print(json.dumps(status, ensure_ascii=False))


def run_job(job_id: str, request_path: Path) -> None:
    try: request = json.loads(request_path.read_text(encoding="utf-8"))
    finally:
        try: request_path.unlink()
        except FileNotFoundError: pass
    action = str(request.get("action") or "")
    stages={"add":("provisioning","Настройка EXIT-ноды"),"remove":("removing","Удаление EXIT-ноды"),"upgrade":("transport","Обновление AWG-транспорта")}
    stage,message=stages.get(action,("running","Выполнение операции"))
    write_job(job_id,state="running",stage=stage,message=message)
    if not PROVISIONER.is_file() or not os.access(PROVISIONER,os.X_OK): raise RuntimeError("EXIT provisioner is not installed")
    raw=json.dumps(request,ensure_ascii=False); request.clear()
    env={"PATH":"/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin","LANG":"C.UTF-8","HOME":"/root"}
    if os.environ.get("SSH_AUTH_SOCK"): env["SSH_AUTH_SOCK"]=os.environ["SSH_AUTH_SOCK"]
    proc=subprocess.run([str(PROVISIONER),"request"],input=raw,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=900,check=False,env=env); raw=""
    if proc.returncode != 0:
        msg=(proc.stderr or proc.stdout or "provisioner failed").strip()[-4000:]
        write_job(job_id,state="error",stage="failed",message="Операция завершилась ошибкой",error=msg,finished_at=now()); return
    try: result=json.loads(proc.stdout)
    except Exception: result={"output":proc.stdout.strip()[:4000]}
    write_job(job_id,state="done",stage="complete",message="Операция успешно завершена",result=result,error=None,finished_at=now())


def command_run(job_id: str, request_path: str) -> None:
    try: run_job(job_id,Path(request_path))
    except Exception as exc:
        try: write_job(job_id,state="error",stage="failed",message="Операция завершилась ошибкой",error=f"{type(exc).__name__}: {exc}",finished_at=now())
        except Exception: pass
        try: Path(request_path).unlink()
        except FileNotFoundError: pass


def gc_jobs(max_age_days: int=7) -> None:
    cutoff=now()-max_age_days*86400; STATE_DIR.mkdir(parents=True,exist_ok=True,mode=0o700); removed=0
    for p in STATE_DIR.glob("j-*.json"):
        try:
            d=json.loads(p.read_text(encoding="utf-8")); ts=int(d.get("finished_at") or d.get("updated_at") or 0)
            if ts and ts<cutoff and d.get("state") in {"done","error"}: p.unlink(); removed+=1
        except Exception: continue
    print(json.dumps({"removed":removed},ensure_ascii=False))


def main() -> None:
    ap=argparse.ArgumentParser(description="EgressMT panel background jobs"); ap.add_argument("--version",action="store_true"); sub=ap.add_subparsers(dest="cmd")
    sub.add_parser("start"); p=sub.add_parser("run"); p.add_argument("job"); p.add_argument("request_path"); p=sub.add_parser("status"); p.add_argument("job"); p=sub.add_parser("gc"); p.add_argument("--days",type=int,default=7); args=ap.parse_args()
    if args.version: print(f"egressmt-panel-job {VERSION}")
    elif args.cmd=="start": start_job()
    elif args.cmd=="run": command_run(args.job,args.request_path)
    elif args.cmd=="status": print(json.dumps(read_job(args.job),ensure_ascii=False))
    elif args.cmd=="gc": gc_jobs(max(1,min(args.days,90)))
    else: ap.print_help()


if __name__=="__main__":
    try: main()
    except Exception as exc:
        print(f"ERROR: {exc}",file=sys.stderr); raise SystemExit(1)
