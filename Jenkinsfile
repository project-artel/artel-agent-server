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

        stage('Resolve Target') {
            steps {
                script {
                    env.IS_PR = isPullRequestBuild() ? 'true' : 'false'

                    if (env.IS_PR == 'true') {
                        env.TARGET_BRANCH = env.CHANGE_TARGET
                        env.TEST_IMAGE_TAG = "${IMAGE_NAME}:pr-${env.CHANGE_ID}-${env.BUILD_NUMBER}-test"
                    } else {
                        env.TARGET_ENV = resolveTargetEnv(env.BRANCH_NAME)
                        env.CONTAINER_NAME = "${APP_NAME}-${env.TARGET_ENV}"
                        env.IMAGE_TAG = "${IMAGE_NAME}:${env.TARGET_ENV}-${env.BUILD_NUMBER}"
                        env.TEST_IMAGE_TAG = "${IMAGE_NAME}:${env.TARGET_ENV}-${env.BUILD_NUMBER}-test"
                        env.ENV_FILE = ".env.${env.TARGET_ENV}"
                    }
                }
            }
        }

        stage('Test') {
            steps {
                sh 'docker build --target test -t $TEST_IMAGE_TAG .'
            }
        }

        stage('Docker Build') {
            when {
                expression { env.IS_PR != 'true' }
            }

            steps {
                sh 'docker build --target runtime -t $IMAGE_TAG .'
            }
        }

        stage('Deploy') {
            when {
                expression { env.IS_PR != 'true' }
            }

            steps {
                sh '''
                    test -f $ENV_FILE

                    docker stop $CONTAINER_NAME || true
                    docker rm $CONTAINER_NAME || true

                    docker run -d \
                      --name $CONTAINER_NAME \
                      --restart unless-stopped \
                      --network app-net \
                      -e APP_ENV=$TARGET_ENV \
                      -e APP_PORT=$APP_PORT \
                      -v $(pwd)/$ENV_FILE:/app/.env:ro \
                      $IMAGE_TAG
                '''
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
