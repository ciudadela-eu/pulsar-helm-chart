#!/usr/bin/env python3
#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#

import base64
import json
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class AuditError(RuntimeError):
    pass


class HttpError(AuditError):
    def __init__(self, url: str, status: int | None, body: str):
        super().__init__(f"HTTP {status or 'connection error'} for {url}: {body[:500]}")
        self.status = status
        self.body = body


class Kubectl:
    def __init__(self, namespace: str, context: str | None = None):
        self.namespace = namespace
        self.context = context

    def command(self, *args: str) -> list[str]:
        command = ["kubectl"]
        if self.context:
            command.extend(["--context", self.context])
        command.extend(args)
        return command

    def run(self, *args: str) -> str:
        result = subprocess.run(
            self.command(*args),
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout

    def current_context(self) -> str:
        if self.context:
            return self.context
        return self.run("config", "current-context").strip()

    def ready_pod(self, selector: str) -> str:
        payload = json.loads(
            self.run(
                "get",
                "pods",
                "-n",
                self.namespace,
                "-l",
                selector,
                "-o",
                "json",
            )
        )
        ready = []
        for pod in payload.get("items", []):
            statuses = pod.get("status", {}).get("containerStatuses", [])
            if (
                pod.get("status", {}).get("phase") == "Running"
                and statuses
                and all(status.get("ready") for status in statuses)
            ):
                ready.append(pod["metadata"]["name"])
        if not ready:
            raise AuditError(f"No Ready pod found for selector {selector!r}")
        return sorted(ready)[0]


def unused_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class PortForward:
    def __init__(
        self,
        kubectl: Kubectl,
        resource: str,
        remote_port: int,
        health_path: str,
    ):
        self.kubectl = kubectl
        self.resource = resource
        self.remote_port = remote_port
        self.health_path = health_path
        self.local_port = unused_local_port()
        self.process: subprocess.Popen[str] | None = None
        self.log_file = tempfile.NamedTemporaryFile(
            mode="w+", prefix="pulsar-audit-port-forward-", delete=False
        )

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.local_port}"

    def __enter__(self) -> "PortForward":
        try:
            command = self.kubectl.command(
                "port-forward",
                "-n",
                self.kubectl.namespace,
                self.resource,
                f"{self.local_port}:{self.remote_port}",
                "--address",
                "127.0.0.1",
            )
            self.process = subprocess.Popen(
                command,
                stdout=self.log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    break
                try:
                    http_json(f"{self.base_url}{self.health_path}", timeout=2)
                    return self
                except HttpError:
                    time.sleep(0.25)
            self.log_file.flush()
            log = Path(self.log_file.name).read_text(errors="replace")
            raise AuditError(
                f"Could not establish port-forward to {self.resource}: {log}"
            )
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.log_file.close()
        Path(self.log_file.name).unlink(missing_ok=True)

    def __exit__(self, *_: object) -> None:
        self.close()


def http_json(url: str, timeout: int = 30) -> Any:
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw_body = response.read()
            if not raw_body:
                raise HttpError(url, response.status, "empty response")
            try:
                return json.loads(raw_body)
            except json.JSONDecodeError as error:
                body = raw_body.decode("utf-8", errors="replace")
                raise HttpError(url, response.status, body) from error
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise HttpError(url, error.code, body) from error
    except (URLError, TimeoutError) as error:
        raise HttpError(url, None, str(error)) from error


def decode_custom_metadata(metadata: dict[str, str]) -> dict[str, str]:
    decoded = {}
    for key, value in metadata.items():
        try:
            decoded[key] = base64.b64decode(value).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            decoded[key] = value
    return decoded


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def schema_parts(schema_id: str) -> tuple[str, str, str]:
    parts = schema_id.split("/", 2)
    if len(parts) != 3 or not all(parts):
        raise AuditError(f"Invalid schema id in BookKeeper metadata: {schema_id!r}")
    return parts[0], parts[1], parts[2]
