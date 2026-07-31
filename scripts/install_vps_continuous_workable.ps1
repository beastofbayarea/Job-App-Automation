# Installs or repairs the persistent one-application-at-a-time Workable worker.
param(
    [string]$RemoteRepoPath = "/root/Job-App-Automation",
    [string]$ConfigPath = "config/vps_config.json",
    [string]$ServiceTemplatePath = "scripts/job-app-continuous-ats.service.template"
)

& "$PSScriptRoot\install_vps_continuous_ats.ps1" `
    -AtsPlatform workable `
    -RemoteRepoPath $RemoteRepoPath `
    -ConfigPath $ConfigPath `
    -ServiceTemplatePath $ServiceTemplatePath
exit $LASTEXITCODE
