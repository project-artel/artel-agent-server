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

        stage('Test') {
            steps {
                sh 'python -m pip install -e ".[dev]"'
                sh 'python -m pytest'
            }
        }

        stage('Docker Build') {
            steps {
                script {
                    env.TARGET_ENV = resolveTargetEnv(env.BRANCH_NAME)
                    env.CONTAINER_NAME = "${APP_NAME}-${env.TARGET_ENV}"
                    env.IMAGE_TAG = "${IMAGE_NAME}:${env.TARGET_ENV}-${env.BUILD_NUMBER}"
                    env.ENV_FILE = ".env.${env.TARGET_ENV}"
                }

                sh 'docker build -t $IMAGE_TAG .'
            }
        }

        stage('Deploy') {
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
