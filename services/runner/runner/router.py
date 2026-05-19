# services/runner/runner/router.py
import os
import socket
from typing import Protocol

import docker
from docker.errors import APIError, NotFound
from docker.models.networks import Network


def _get_docker_host_ip() -> str:
    """The host IP reachable from inside a Docker container on a default bridge."""
    try:
        return socket.gethostbyname("host.docker.internal")
    except socket.gaierror:
        return "172.17.0.1"


class Router(Protocol):
    """Routes connections from containers back to services on the daemon."""

    def attach(self, net: Network) -> None: ...
    def detach(self, net: Network) -> None: ...
    def address_for(self, net: Network, port: int) -> str: ...


class HostRouter:
    """Daemon runs on the host. Sim reaches it via the docker bridge gateway."""

    def attach(self, net: Network) -> None:
        pass

    def detach(self, net: Network) -> None:
        pass

    def address_for(self, net: Network, port: int) -> str:
        return f"{_get_docker_host_ip()}:{port}"


class ContainerRouter:
    """Daemon runs in a container. Attach the daemon to each sim network and
    hand back the daemon's IP on that network."""

    def __init__(self, container_id: str, client: docker.DockerClient):
        self._id = container_id
        self._client = client

    def attach(self, net: Network) -> None:
        net.connect(self._id)

    def detach(self, net: Network) -> None:
        try:
            net.disconnect(self._id)
        except (NotFound, APIError):
            pass

    def address_for(self, net: Network, port: int) -> str:
        c = self._client.containers.get(self._id)
        c.reload()
        ip = c.attrs["NetworkSettings"]["Networks"][net.name]["IPAddress"]
        return f"{ip}:{port}"


def router_from_env(client: docker.DockerClient) -> Router:
    """Pick a router based on whether DAEMON_CONTAINER_ID is set."""
    if os.environ.get("IS_DOCKER_CONTAINER") is not None:
        return ContainerRouter(socket.gethostname(), client)
    return HostRouter()