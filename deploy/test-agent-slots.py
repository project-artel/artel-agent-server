"""실제 Docker에서 세션 생성, WebSocket 전환, rollback과 drain을 확인한다.

저장소 루트에서 python3 deploy/test-agent-slots.py <runtime image> 로 실행한다.
"""

import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from uuid import uuid4


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, text=True, **kwargs)


def main(image):
    name = "agent-test-" + uuid4().hex[:10]
    second = name + ":second"
    legacy = name
    router = name + "-router"
    catalog = name + "-catalog"
    client = name + "-client"
    cache = name + "-redis"
    store = os.environ.get("TEST_SESSION_STORE", "memory")
    base = f"http://{router}:8080/internal"
    processes = []
    with tempfile.TemporaryDirectory() as directory:
        env_file = Path(directory, "agent.env")
        env_file.write_text(f"SESSION_STORE={store}\nREDIS_URL=redis://{cache}:6379/0\nORCHESTRATION_BASE_URL=http://{catalog}:8000\n")
        env = dict(os.environ, CONTAINER_NAME=name, TARGET_ENV="test", APP_PORT="8080",
                   AGENT_NETWORK=name, IMAGE_TAG=image, ENV_SRC=str(env_file))

        def deploy(action, expected=0, tag=image, script="deploy/agent-slots.sh"):
            result = subprocess.run(["sh", script, action],
                                    env=dict(env, IMAGE_TAG=tag))
            assert result.returncode == expected, (action, result.returncode)

        def post(host, path, body):
            script = """import sys, urllib.request
request = urllib.request.Request(sys.argv[1], data=sys.stdin.buffer.read(),
                                 headers={"Content-Type": "application/json"})
print(urllib.request.urlopen(request, timeout=10).read().decode())"""
            result = docker("exec", "-i", client, "python", "-c", script, host + path,
                            input=json.dumps(body), capture_output=True)
            return json.loads(result.stdout)

        def session(host=base):
            return post(host, "/sessions", {"user_input": ""})["session_id"]

        def socket(sid):
            script = """import asyncio, json, sys
from websockets import connect
async def run():
    async with connect(sys.argv[1], open_timeout=10) as ws:
        await ws.send('{"type":"probe"}')
        assert json.loads(await asyncio.wait_for(ws.recv(), 5))['code'] == 'bad_request'
        print('connected', flush=True)
        await asyncio.to_thread(input)
        await ws.send('{"type":"close"}')
        assert json.loads(await asyncio.wait_for(ws.recv(), 5))['type'] == 'closed'
asyncio.run(run())"""
            proc = subprocess.Popen(
                ["docker", "exec", "-i", client, "python", "-c", script,
                 base.replace("http://", "ws://") + "/sessions/" + sid],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            processes.append(proc)
            assert proc.stdout.readline().strip() == "connected"
            return proc

        def close(proc):
            proc.communicate("\n", timeout=15)
            assert proc.returncode == 0

        try:
            docker("network", "create", "--internal", name)
            if store == "redis":
                docker("run", "-d", "--name", cache, "--network", name, "redis:7-alpine")
            docker("build", "-t", second, "-",
                   input=f"FROM {image}\nLABEL rollout-test={name}\n")
            docker("run", "-d", "--name", client, "--network", name, image, "sleep", "600")
            docker("run", "-d", "--name", catalog, "--network", name, image, "sh", "-ec",
                   "mkdir -p /tmp/internal; cd /tmp; exec python -m http.server 8000")
            docker("cp", "tests/fixtures/model_catalog.json", catalog + ":/tmp/internal/llm-models")
            docker("run", "-d", "--name", legacy, "--network", name, "--env-file", str(env_file),
                   image, "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080")
            deploy("deploy")
            old_id = session(f"http://{legacy}:8080/internal")
            assert "-" not in old_id
            close(socket(old_id))
            payload = {"user_input": "", "game_context": {"sample": "x" * (1024 * 1024)}}
            pending = post(base, "/sessions", payload)["session_id"]
            live = socket(session())
            assert pending.startswith("blue-")
            # nginx 자체는 정상 기동하지만 후보로 /health를 전달하지 못하는 조건.
            broken = Path(directory, "broken")
            broken.mkdir()
            shutil.copy("deploy/agent-slots.sh", broken)
            template = Path("deploy/router.conf.in").read_text()
            (broken / "router.conf.in").write_text(template.replace(
                "    location / {", "    location = /health { return 503; }\n    location / {"))
            deploy("deploy", expected=1, tag=second, script=str(broken / "agent-slots.sh"))
            assert session().startswith("blue-")
            deploy("deploy", tag=second)
            green = session()
            assert green.startswith("green-")
            deploy("collect", expected=75)
            close(live)
            deploy("collect", expected=75)
            close(socket(pending))
            # 전환 후 Jenkins가 종료된 상태를 재현한다. marker가 일치해도 장애가 난
            # active 대신 요청을 받을 이전 슬롯을 collect가 중지하면 안 된다.
            docker("exec", router, "sh", "-ec",
                   "sed -i '/    location \\/ {/i\\    location = /health { return 503; }' "
                   "/etc/nginx/conf.d/default.conf; nginx -s reload")
            for _ in range(30):
                probe = subprocess.run(["docker", "exec", router, "wget", "-qO", "/dev/null",
                                        "http://127.0.0.1:8080/health"], capture_output=True)
                if probe.returncode:
                    break
                time.sleep(0.2)
            else:
                raise AssertionError("expected unhealthy active slot")
            result = subprocess.run(["sh", "deploy/agent-slots.sh", "collect"], env=env)
            assert result.returncode not in (0, 75)
            assert docker("inspect", "-f", "{{.State.Running}}", name + "-blue",
                          capture_output=True).stdout.strip() == "true"
            deploy("rollback-blue")
            deploy("rollback-blue")
            assert session().startswith("blue-")
            deploy("collect", expected=75)
            post(base, f"/sessions/{green}/approve", {})
            for _ in range(15):
                result = subprocess.run(["sh", "deploy/agent-slots.sh", "collect"], env=env)
                if result.returncode != 75:
                    assert result.returncode == 0
                    break
                time.sleep(1)
            assert docker("inspect", "-f", "{{.State.Running}}", name + "-green",
                          capture_output=True).stdout.strip() == "false"
            print("PASS: legacy, pending, live WebSocket, cutover, rollback, drain")
        finally:
            for proc in processes:
                if proc.poll() is None:
                    proc.stdin.close()
            for container in [router, name + "-blue", name + "-green", legacy, catalog, client, cache]:
                subprocess.run(["docker", "rm", "-f", container], capture_output=True)
            subprocess.run(["docker", "volume", "rm", router + "-config"], capture_output=True)
            subprocess.run(["docker", "network", "rm", name], capture_output=True)
            subprocess.run(["docker", "rmi", second], capture_output=True)


if __name__ == "__main__":
    main(sys.argv[1])
