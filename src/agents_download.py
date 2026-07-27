#!/usr/bin/env python3

from simulator.log import info
from simulator.util import run_parallel
from simulator.hosts import public_ip
from simulator.remote import copy_from_remote


def _agent_download(agent, run_path, run_id):
    info(f"     {public_ip(agent)} Download")

    home = "/opt/simulator" if agent.get("provider") == "kubernetes" else "hazelcast-simulator"
    if run_id == "*":
        dst_path = f"{home}/workers/"
    else:
        dst_path = f"{home}/workers/{run_id}/"

    # copy the files
    # we exclude the uploads directory because it could be very big e.g jars
    copy_from_remote(agent, dst_path, run_path)

    info(f"     {public_ip(agent)} Download completed")


def agents_download(agents, run_path: str, run_id: str):
    info(f"Downloading: starting")
    run_parallel(_agent_download, [(agent, run_path, run_id,) for agent in agents])
    info(f"Downloading: done")
