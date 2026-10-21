"""Optional Python tool: fresh, resource-limited Docker container for each call."""

import json
import shutil
import subprocess
import threading
from uuid import uuid4


DEFAULT_IMAGE = "python:3.11-slim"
OUTPUT_LIMIT = 16384
CODE_LIMIT = 16384
TOOL_TIMEOUT = 10
PYTHON_TOOL = {
    "type": "function",
    "function": {
        "name": "run_python",
        "description": "Execute Python code in an isolated environment. Returns stdout, stderr, and exit code.",
        "parameters": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "Python code to execute."}},
            "required": ["code"],
            "additionalProperties": False,
        },
    },
}

# CPU/file limits supplement Docker's memory/PID limits and host wall timeout.
BOOTSTRAP = """import resource, sys
resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
resource.setrlimit(resource.RLIMIT_FSIZE, (1048576, 1048576))
code = sys.stdin.read()
exec(compile(code, '<run_python>', 'exec'), {'__name__': '__main__'})
"""


class DockerPython:
    def __init__(self, image=DEFAULT_IMAGE):
        if not shutil.which("docker"):
            raise RuntimeError("--python-tool requires Docker. Install/start Docker and pull " + image)
        try:
            subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                           check=True, capture_output=True, text=True, timeout=10)
            inspected = subprocess.run(["docker", "image", "inspect", image, "--format", "{{.Id}}"],
                                       check=True, capture_output=True, text=True, timeout=10)
        except (subprocess.SubprocessError, OSError) as exc:
            raise RuntimeError(f"Start Docker and run: docker pull {image}") from exc
        self.image_id = inspected.stdout.strip()
        self.image = image

    def execute(self, name, arguments):
        if name != "run_python":
            return {"stdout": "", "stderr": "Unknown tool: " + str(name), "exit_code": 1}
        try:
            parsed = json.loads(arguments)
            if not isinstance(parsed, dict) or set(parsed) != {"code"}:
                raise ValueError("Expected exactly one field: code")
            code = parsed["code"]
            if not isinstance(code, str) or not code.strip() or len(code.encode()) > CODE_LIMIT:
                raise ValueError("code must be a nonempty Python string of at most 16384 bytes")
        except (ValueError, TypeError) as exc:
            return {"stdout": "", "stderr": "Invalid tool arguments: " + str(exc), "exit_code": 1}

        container = "llm-number-python-" + uuid4().hex
        command = [
            "docker", "run", "--rm", "--pull=never", "--name", container,
            "--network=none", "--read-only", "--memory=128m", "--memory-swap=128m",
            "--cpus=1", "--pids-limit=32", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--user=65534:65534",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=16m", "--workdir=/tmp", "--log-driver=none",
            "--interactive", "--entrypoint=python", self.image_id, "-I", "-u", "-c", BOOTSTRAP,
        ]
        chunks = {}
        overflow = threading.Event()
        timed_out = False
        process = None
        threads = []

        def read_bounded(stream, key):
            data = stream.read(OUTPUT_LIMIT + 1)
            chunks[key] = data[:OUTPUT_LIMIT].decode("utf-8", errors="replace")
            if len(data) > OUTPUT_LIMIT:
                overflow.set()
                if process.poll() is None:
                    process.kill()

        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for stream, key in [(process.stdout, "stdout"), (process.stderr, "stderr")]:
                thread = threading.Thread(target=read_bounded, args=(stream, key), daemon=True)
                thread.start()
                threads.append(thread)
            try:
                process.stdin.write(code.encode("utf-8"))
            except BrokenPipeError:
                pass
            finally:
                process.stdin.close()
            try:
                process.wait(timeout=TOOL_TIMEOUT)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                process.wait()
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            # Killing the Docker client alone does not reliably stop its container.
            subprocess.run(["docker", "rm", "--force", container], capture_output=True, timeout=10)
            for thread in threads:
                thread.join(timeout=2)
            if process is not None:
                process.stdout.close()
                process.stderr.close()
        return {
            "stdout": chunks.get("stdout", ""), "stderr": chunks.get("stderr", ""),
            "exit_code": 124 if timed_out else process.returncode,
            "timed_out": timed_out, "output_truncated": overflow.is_set(),
        }
