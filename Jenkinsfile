pipeline {
    agent any

    options {
        timestamps()
        disableConcurrentBuilds()
    }

    // Runs 3x/day IST: 9:00 AM, 2:00 PM, 7:00 PM
    // Change the hours in "0 9,14,19 * * *" if you want different times.
    triggers {
        cron('TZ=Asia/Kolkata\n0 9,14,19 * * *')
    }

    environment {
        // "naukri-creds" must be created in Jenkins as a
        // "Username with password" credential (see setup guide).
        NAUKRI_CREDS       = credentials('naukri-creds')
        NAUKRI_EMAIL       = "${env.NAUKRI_CREDS_USR}"
        NAUKRI_PASSWORD    = "${env.NAUKRI_CREDS_PSW}"

        // >>> UPDATE THIS to the real path of your resume file on the
        // Jenkins machine before the first run <<<
        NAUKRI_RESUME_PATH = 'D:\\Naukri\\Santhosh_QA_11_11.pdf'

        NAUKRI_HEADLESS    = 'true'
    }

    stages {
        stage('Install dependencies') {
            steps {
                bat 'python -m pip install --quiet playwright'
                bat 'python -m playwright install chromium'
            }
        }

        stage('Run Naukri uploader') {
            steps {
                // >>> UPDATE THIS to the real path where you saved
                // naukri_resume_uploader.py on the Jenkins machine <<<
                bat 'python C:\\NaukriAutomation\\naukri_resume_uploader.py'
            }
        }
    }

    post {
        success {
            script {
                try {
                    mail to: 'YOUR_EMAIL@example.com',
                         subject: "SUCCESS: Naukri Resume Upload - Build #${env.BUILD_NUMBER}",
                         body: "The resume upload ran successfully.\n\nBuild: ${env.BUILD_URL}"
                } catch (Exception e) {
                    echo "Email notification failed (build itself still succeeded): ${e.getMessage()}"
                }
            }
        }
        failure {
            script {
                try {
                    mail to: 'YOUR_EMAIL@example.com',
                         subject: "FAILED: Naukri Resume Upload - Build #${env.BUILD_NUMBER}",
                         body: "The resume upload failed. Check the console log and logs/ folder for screenshots.\n\nConsole: ${env.BUILD_URL}console"
                } catch (Exception e) {
                    echo "Email notification failed: ${e.getMessage()}"
                }
            }
        }
        always {
            archiveArtifacts artifacts: 'logs/**', allowEmptyArchive: true
        }
    }
}
