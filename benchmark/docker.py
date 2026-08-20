"""Bringing the benchmark's Postgres up, and knowing when it is ready.

Default path for a local run. CI passes `--database-url` at a service
container instead and never comes through here, so there is exactly one code
path for the benchmark itself and only the database plumbing differs.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path


COMPOSE_FILE = Path(__file__).parent / "compose" / "docker-compose.yml"
PROJECT = "django-overlay-bench"


class DockerUnavailable(Exception):
    """Docker is not installed, not running, or not usable."""


def available():
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "info"], capture_output=True, text=True, check=False,
    )
    return result.returncode == 0


def _compose(*args, env=None, capture=True):
    command = ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", PROJECT, *args]
    return subprocess.run(
        command, capture_output=capture, text=True, check=False, env=env,
    )


def _environment(postgres_version, work_mem, shared_buffers, host_port):
    return {
        **os.environ,
        "POSTGRES_VERSION": str(postgres_version),
        "WORK_MEM": work_mem,
        "SHARED_BUFFERS": shared_buffers,
        "HOST_PORT": str(host_port),
    }


def up(postgres_version=17, work_mem="4MB", shared_buffers="128MB",
       host_port=55432, say=print, timeout=120):
    """Start the container and block until it answers. Returns a database URL."""
    if not available():
        raise DockerUnavailable(
            "docker is not available. Start Docker, or point the benchmark at a "
            "database you already have with --database-url postgres://..."
        )

    env = _environment(postgres_version, work_mem, shared_buffers, host_port)
    say(f"starting postgres {postgres_version} on port {host_port} "
        f"(work_mem {work_mem}, shared_buffers {shared_buffers})")
    result = _compose("up", "-d", env=env)
    if result.returncode != 0:
        raise DockerUnavailable(f"docker compose up failed:\n{result.stderr.strip()}")

    deadline = time.time() + timeout
    while time.time() < deadline:
        check = _compose(
            "exec", "-T", "postgres",
            "pg_isready", "-U", "postgres", "-d", "django_overlay_bench",
            env=env,
        )
        if check.returncode == 0:
            return url(host_port)
        time.sleep(1)
    raise DockerUnavailable(f"postgres did not become ready within {timeout}s")


def down(postgres_version=17, keep_data=True, say=print):
    """Stop the container.

    The volume is left alone unless asked otherwise -- it holds the loaded
    graph, which is the expensive thing.
    """
    env = _environment(postgres_version, "4MB", "128MB", 55432)
    args = ["down"] if keep_data else ["down", "-v"]
    say("stopping postgres" + ("" if keep_data else " and deleting its data volume"))
    _compose(*args, env=env)


def url(host_port=55432):
    return f"postgres://postgres:benchmark@localhost:{host_port}/django_overlay_bench"
