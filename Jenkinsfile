pipeline {
    agent any

    // 빌드 기록에 상한이 없었다. 이 잡의 로그는 pip 설치와 pytest 출력을 통째로 담는데,
    // 아무것도 그것을 지우지 않는다. 상한을 두지 않으면 디스크가 유일한 상한이고, 그 상한은
    // 상관없는 다른 잡을 실패시키는 방식으로 말한다.
    //
    // 30 은 지금 되돌아볼 만한 폭이다. 그보다 오래된 빌드는 로그를 열어 본 적이 없다.
    options {
        buildDiscarder(logRotator(numToKeepStr: '30'))
    }

    environment {
        APP_NAME = 'artel-agent-server'
        IMAGE_NAME = 'artel-agent-server'
        APP_PORT = '8080'
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
            }
        }

        // Only the tag the Test stage needs. Everything the deploy path reads is
        // resolved inside Deploy Pipeline, because resolveTargetEnv errors on a
        // branch that has no deploy target and a feature branch is exactly that.
        stage('Resolve Target') {
            steps {
                script {
                    env.IS_PR = isPullRequestBuild() ? 'true' : 'false'

                    if (env.IS_PR == 'true') {
                        env.TARGET_BRANCH = env.CHANGE_TARGET
                        env.TEST_IMAGE_TAG = "${IMAGE_NAME}:pr-${env.CHANGE_ID}-${env.BUILD_NUMBER}-test"
                    } else {
                        // Branch names carry slashes and non-ASCII, neither of which a
                        // docker tag accepts, so the build number stands in for the
                        // branch. Two branch jobs can collide on it and it costs
                        // nothing: this image is a throwaway label on a build whose
                        // only product is the pytest exit status inside it.
                        env.TEST_IMAGE_TAG = "${IMAGE_NAME}:branch-${env.BUILD_NUMBER}-test"
                    }
                }
            }
        }

        stage('Test') {
            steps {
                sh 'docker build --target test -t $TEST_IMAGE_TAG .'
            }
        }

        // Deploy-target branches only. A PR job's BRANCH_NAME is 'PR-<number>' and a
        // feature branch is its own name, so neither matches — which is the point.
        // Reaching resolveTargetEnv from either one fails the build outright, and
        // that failure looked exactly like a test regression on every feature branch.
        stage('Deploy Pipeline') {
            when {
                anyOf {
                    branch 'main'
                    branch 'operation'
                    branch 'develop'
                    branch 'stage'
                }
            }

            stages {
                stage('Docker Build') {
                    steps {
                        script {
                            env.TARGET_ENV = resolveTargetEnv(env.BRANCH_NAME)
                            env.CONTAINER_NAME = "${APP_NAME}-${env.TARGET_ENV}"
                            env.IMAGE_TAG = "${IMAGE_NAME}:${env.TARGET_ENV}-${env.BUILD_NUMBER}"
                            env.ENV_FILE = ".env.${env.TARGET_ENV}"
                        }

                        // GIT_SHA and IMAGE_TAG are baked in so a QA run can record which
                        // build produced it. The agent's structure is versioned by commit
                        // and image rather than by keeping old structures in the tree, so
                        // without these a past run cannot be reproduced.
                        sh '''
                            docker build --target runtime \
                              --build-arg GIT_SHA=$GIT_COMMIT \
                              --build-arg IMAGE_TAG=$IMAGE_TAG \
                              -t $IMAGE_TAG .
                        '''
                    }
                }

                stage('Deploy') {
                    steps {
                        withCredentials([file(credentialsId: "agent-server-env-${env.TARGET_ENV}", variable: 'ENV_SRC')]) {
                            sh '''
                                cp "$ENV_SRC" "$ENV_FILE"
                                test -f $ENV_FILE

                                docker stop $CONTAINER_NAME || true
                                docker rm $CONTAINER_NAME || true

                                docker run -d \
                                  --name $CONTAINER_NAME \
                                  --restart unless-stopped \
                                  --network app-net \
                                  -e APP_ENV=$TARGET_ENV \
                                  -e APP_PORT=$APP_PORT \
                                  --env-file "$ENV_FILE" \
                                  $IMAGE_TAG
                            '''
                        }

                        // 배포 이미지는 빌드 번호로 태그가 갈려 재사용되지 않는다. 지우지 않으면
                        // 배포할 때마다 앞의 것이 디스크에 영원히 남는다. 셋을 남기는 것은 지금
                        // 도는 것 하나와, 손으로 되돌릴 자리 둘이다.
                        //
                        // `-f` 를 쓰지 않는다. 컨테이너가 쓰고 있는 이미지는 `docker rmi` 가
                        // 거절하고, 그 거절이 도는 stage 를 발밑에서 빼가지 않게 하는 유일한
                        // 방어다.
                        //
                        // 생성 시각이 아니라 빌드 번호로 정렬한다. `docker images` 의 기본
                        // 순서를 믿고 짰다가 같은 초에 만들어진 다섯 개가 뒤섞여 나오는 것을
                        // 봤다 — 그 정렬은 초 단위라 동률에서 순서를 보장하지 않는다. 태그
                        // 뒤쪽의 빌드 번호는 단조 증가하므로 동률이 없다.
                        sh '''
                            docker images --filter "reference=$IMAGE_NAME:$TARGET_ENV-*" \
                              --format '{{.Tag}}' \
                              | sort -t- -k2 -n -r \
                              | tail -n +4 \
                              | sed "s|^|$IMAGE_NAME:|" \
                              | xargs -r docker rmi || true
                        '''
                    }
                }
            }
        }
    }

    post {
        always {
            // Test 스테이지가 만든 이미지는 빌드마다 태그가 다르고, 그 안에서 나온 pytest 종료
            // 코드가 산출물의 전부다. 'Resolve Target' 의 주석이 이미 버리는 물건이라고 부르는데,
            // 정작 버리는 코드가 없었다. PR 에 푸시할 때마다 하나씩 남았다.
            //
            // 한 번이 가볍지도 않다. Dockerfile 의 test 타깃은 `COPY app` 이 `RUN pip install`
            // 위에 있어서, 앱을 한 줄만 고쳐도 의존성 레이어가 통째로 새로 만들어진다. 빌드끼리
            // 그 레이어를 나눠 쓰지 못한다.
            //
            // 실패한 빌드에서는 태그가 붙기 전에 죽어 `rmi` 가 헛돈다. 그때 남는 것은 태그 없는
            // 레이어와 빌드 캐시이고, 뒤의 둘이 그것을 가져간다.
            //
            // 뒤의 둘은 남의 것을 건드리지 않는 것만 고른다 — `agent any` 라 세 레포의 잡이 같은
            // 에이전트를 나눠 쓴다. `image prune` 은 태그 없는 고아만, `builder prune` 은 일주일
            // 넘게 안 쓰인 캐시만 가져간다. `system prune -a` 였다면 옆에서 도는 잡의 이미지를
            // 죽였을 것이다.
            sh '''
                docker rmi "$TEST_IMAGE_TAG" || true
                docker image prune -f
                docker builder prune -f --filter until=168h
            '''

            // 워크스페이스는 브랜치마다 · PR 마다 따로 파이고, 멀티브랜치 잡은 존재했던 모든 PR
            // 것을 남긴다. 여기 남는 것 중 다음 빌드가 쓰는 것은 없다 — 빌드는 워크스페이스가
            // 아니라 도커 안에서 돌고, `checkout scm` 이 트리를 다시 만든다.
            deleteDir()
        }
    }
}

def resolveTargetEnv(String branchName) {
    if (branchName == 'main' || branchName == 'operation') {
        return 'operation'
    }

    if (branchName == 'develop' || branchName == 'stage') {
        return 'stage'
    }

    error "Unsupported branch for deployment: ${branchName}"
}

def isPullRequestBuild() {
    return env.CHANGE_ID?.trim()
}
