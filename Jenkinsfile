pipeline {
    agent any

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
                    }
                }
            }
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
