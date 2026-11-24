#!/bin/sh
# Jenkins가 환경별 lock을 잡은 동안 한 번씩 실행한다.
set -eu
: "${CONTAINER_NAME:?}" "${APP_PORT:?}" "${TARGET_ENV:?}"
router="$CONTAINER_NAME-router"
volume="$router-config"
network=${AGENT_NETWORK:-app-net}
proxy_image=nginx:1.28-alpine@sha256:a8b39bd9cf0f83869a2162827a0caf6137ddf759d50a171451b335cecc87d236
dir=$(dirname "$0")

exists() {
    # 조회 실패를 '없음'으로 취급하면 이미 실행 중인 슬롯을 재사용할 수 있다.
    names=$(docker container ls -a --format '{{.Names}}' --filter "name=^/$1$") || exit 1
    [ "$names" = "$1" ]
}

running() {
    state=$(docker inspect -f '{{.State.Running}}' "$1") || exit 1
    [ "$state" = true ]
}

other() {
    case "$1" in blue) echo green;; green) echo blue;; *) exit 1;; esac
}

status() {
    docker exec "$CONTAINER_NAME-$1" python -c '
import json, sys, urllib.request
with urllib.request.urlopen("http://127.0.0.1:" + sys.argv[1] + "/internal/deployment/status", timeout=5) as r:
    state = json.load(r)
if state["slot"] != sys.argv[2]:
    sys.exit(1)
print("idle" if state["idle"] else "busy")' "$APP_PORT" "$1"
}

ready() {
    n=0
    until status "$1" >/dev/null 2>&1; do
        n=$((n+1))
        if [ "$n" -ge 60 ]; then echo "Slot $1 did not become ready" >&2; return 1; fi
        sleep 2
    done
}

config() {
    image=$(docker inspect -f '{{.Image}}' "$CONTAINER_NAME-$1") || return 1
    sed -e "s/@IMAGE@/$image/g" -e "s/@SLOT@/$1/g" -e "s/@SERVICE@/$CONTAINER_NAME/g" -e "s/@PORT@/$APP_PORT/g" "$dir/router.conf.in"
}

loaded() {
    docker exec "$router" wget -qO- "http://127.0.0.1:$APP_PORT/deployment-active"
}

disk_slot() {
    docker exec "$router" sed -n '1s/^# active //p' /etc/nginx/conf.d/default.conf
}

verify() {
    n=0
    until [ "$(loaded)" = "$1" ]; do
        n=$((n+1))
        if [ "$n" -ge 20 ]; then echo "Router failed verification for $1" >&2; return 1; fi
        sleep 1
    done
}

reload() {
    docker exec "$router" sh -ec '
        cd /etc/nginx/conf.d
        # nginx master의 기존 자식이 모두 끝날 때까지 drain한다.
        { cat /proc/sys/kernel/random/boot_id; cut -d " " -f22 /proc/1/stat; cat /proc/1/task/1/children; } > workers.next
        mv workers.next workers.previous
        nginx -s reload'
}

routed_health() {
    docker exec "$router" wget -T 5 -qO /dev/null "http://127.0.0.1:$APP_PORT/health"
}

switch_to() {
    docker exec "$router" wget -T 5 -qO /dev/null "http://$CONTAINER_NAME-$1:$APP_PORT/health"
    content=$(config "$1")
    printf '%s\n' "$content" | docker exec -i "$router" sh -ec '
        cd /etc/nginx/conf.d
        cat > default.next
        test -s default.next
        cp default.conf default.backup
        mv default.next default.conf
        if ! nginx -t; then mv default.backup default.conf; exit 1; fi
        mv default.backup default.previous'
    if reload && verify "$1" && routed_health; then return 0; fi

    # 후보가 요청을 받은 뒤에도 두 슬롯을 보존하고 신규 요청만 원래 슬롯으로 돌린다.
    docker exec "$router" sh -ec '
        cd /etc/nginx/conf.d
        cp default.conf default.failed
        cp default.previous default.restore
        mv default.restore default.conf
        mv default.failed default.previous'
    reload
    verify "$active"
    routed_health
    return 1
}

# nginx 이전 worker는 전환 직전에 받은 요청과 열린 WebSocket을 마저 처리한다.
idle() {
    docker exec "$router" sh -ec '
        cd /etc/nginx/conf.d
        if [ -e workers.previous ] && [ "$(sed -n 1p workers.previous)" = "$(cat /proc/sys/kernel/random/boot_id)" ] && [ "$(sed -n 2p workers.previous)" = "$(cut -d " " -f22 /proc/1/stat)" ]; then
            for pid in $(tail -n +3 workers.previous); do
                [ ! -e "/proc/$pid" ] || exit 1
            done
        fi' || return 1
    [ "$(status "$1")" = idle ]
}

start_candidate() {
    docker run -d --name "$CONTAINER_NAME-$1" --restart unless-stopped \
        --network "$network" --env-file "$ENV_SRC" -e "APP_ENV=$TARGET_ENV" \
        -e "APP_PORT=$APP_PORT" -e "DEPLOYMENT_SLOT=$1" "$IMAGE_TAG" \
        uvicorn app.main:app --host 0.0.0.0 --port "$APP_PORT" --workers 1
}

command=${1:?deploy, rollback or collect}
case "$command" in deploy|rollback-blue|rollback-green|collect) ;; *) exit 2;; esac
# Docker 조회 오류가 빈 결과처럼 보이지 않게 한다.
docker info --format '{{.ID}}' >/dev/null
if exists "$router"; then
    if ! running "$router"; then docker start "$router"; fi
    active=$(disk_slot)
    target=$(other "$active")
    # 중간에 Jenkins가 종료되어도 디스크 설정과 worker가 일치한 후에만 슬롯을 정리한다.
    if [ "$(loaded)" != "$active" ]; then
        reload
        verify "$active"
    fi
    case "$command" in
        deploy|collect) verify "$active"; routed_health;;
    esac
else
    [ "$command" = deploy ] || exit 1
    active=''
    target=blue
fi

candidate="$CONTAINER_NAME-$target"
case "$command" in
    collect)
        if exists "$candidate" && running "$candidate"; then
            idle "$target" || exit 75
            docker stop "$candidate"
        fi
        exit 0;;
    rollback-blue|rollback-green)
        [ "${command#rollback-}" != "$active" ] || exit 0
        exists "$candidate" || { echo "No previous slot" >&2; exit 1; }
        # 이전에 공개된 슬롯만 롤백할 수 있다. 준비 실패한 후보는 대상에서 제외한다.
        previous=$(docker exec "$router" sed -n '1s/^# active //p' /etc/nginx/conf.d/default.previous)
        [ "$previous" = "$target" ] || exit 1
        expected=$(docker exec "$router" sed -n '2s/^# image //p' /etc/nginx/conf.d/default.previous)
        [ "$(docker inspect -f '{{.Image}}' "$candidate")" = "$expected" ] || exit 1
        if ! running "$candidate"; then docker start "$candidate"; fi
        ready "$target"
        switch_to "$target"
        exit 0;;
esac

: "${IMAGE_TAG:?}" "${ENV_SRC:?}"
image_id=$(docker image inspect -f '{{.Id}}' "$IMAGE_TAG")
if [ -n "$active" ] && [ "$(docker inspect -f '{{.Image}}' "$CONTAINER_NAME-$active")" = "$image_id" ]; then
    exit 0
fi
if exists "$candidate"; then
    if [ "$(docker inspect -f '{{.Image}}' "$candidate")" != "$image_id" ]; then
        if running "$candidate"; then
            [ -n "$active" ] && idle "$target" || { echo "Slot $target still in use" >&2; exit 1; }
            docker stop "$candidate"
        fi
        docker rm "$candidate"
    fi
fi
new_candidate=false
if exists "$candidate"; then
    if ! running "$candidate"; then docker start "$candidate"; fi
else
    start_candidate "$target"
    new_candidate=true
fi
if ! ready "$target"; then
    if [ "$new_candidate" = true ]; then docker stop "$candidate"; fi
    exit 1
fi
if [ -z "$active" ]; then
    docker run --rm --network "$network" "$proxy_image" wget -T 5 -qO /dev/null "http://$candidate:$APP_PORT/health"
    content=$(config "$target")
    printf '%s\n' "$content" | docker run --rm -i -v "$volume:/config" "$proxy_image" \
        sh -ec 'cat > /config/default.next; if [ -s /config/default.conf ]; then cmp /config/default.conf /config/default.next; fi; mv /config/default.next /config/default.conf'
    docker run -d --name "$router" --network "$network" --restart unless-stopped \
        -v "$volume:/etc/nginx/conf.d" "$proxy_image"
    verify "$target"
    routed_health
else
    switch_to "$target"
fi
