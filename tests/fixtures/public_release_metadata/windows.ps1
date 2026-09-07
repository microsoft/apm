# Stub transport only; execute the real installer until its first write.
$ErrorActionPreference = "Stop"
if ($env:PS_STRICT_METADATA) {
    Set-StrictMode -Version Latest
}
if ($env:PS_METADATA_DEFAULTS) {
    $PSDefaultParameterValues["Invoke-RestMethod:Headers"] = @{ Authorization = "token $env:EXPECTED_TOKEN" }
    $PSDefaultParameterValues["Invoke-*: Cred* "] = [PSCredential]::new(
        "synthetic-user", (ConvertTo-SecureString $env:EXPECTED_TOKEN -AsPlainText -Force)
    )
    $PSDefaultParameterValues["Invoke-RestMethod: UseDefaultCredentials "] = $true
    $PSDefaultParameterValues["Invoke-RestMethod:Authentication"] = "Bearer"
    $PSDefaultParameterValues["Invoke-RestMethod:Token"] = ConvertTo-SecureString $env:EXPECTED_TOKEN -AsPlainText -Force
    $PSDefaultParameterValues["Invoke-RestMethod:WebSession"] = "synthetic-session"
    $PSDefaultParameterValues["Invoke-RestMethod:Proxy"] = "http://proxy.example"
}
$originalDefaults = $PSDefaultParameterValues.Clone()

function Invoke-RestMethod {
    [CmdletBinding()]
    param(
        [string]$Uri,
        [hashtable]$Headers = @{},
        [int]$MaximumRedirection = -1,
        [int]$TimeoutSec,
        [PSCredential]$Credential,
        [switch]$UseDefaultCredentials,
        [string]$Authentication,
        [securestring]$Token,
        $WebSession,
        [string]$Proxy
    )
    if ($MaximumRedirection -ne 0 -or $TimeoutSec -ne 30) {
        throw "Unexpected transport controls"
    }
    if ($Credential -or $UseDefaultCredentials -or $Authentication -or $Token -or $WebSession) {
        throw "Unexpected ambient authentication"
    }
    if ($env:PS_METADATA_DEFAULTS -and $Proxy -ne "http://proxy.example") {
        throw "Unexpected proxy default loss"
    }
    $authenticated = $Headers.ContainsKey("Authorization")
    if ($authenticated -and $Headers.Authorization -cne "token $env:EXPECTED_TOKEN") {
        throw "Unexpected credential selection"
    }
    [Console]::Error.WriteLine("REQUEST " + (@{
        url = $Uri; authenticated = $authenticated
    } | ConvertTo-Json -Compress))
    $responses = @($env:HTTP_RESPONSES | ConvertFrom-Json)
    $response = if ($authenticated) { $responses[0] } else { $responses[-1] }
    if ($response.status -eq 0) {
        throw [System.Net.WebException]::new("Synthetic network failure")
    }
    if ($response.status -ne 200) {
        $httpResponse = [System.Net.Http.HttpResponseMessage]::new($response.status)
        foreach ($header in $response.headers.PSObject.Properties) {
            $httpResponse.Headers.TryAddWithoutValidation($header.Name, [string]$header.Value) | Out-Null
        }
        if ($env:PS_HEADERS_STYLE -eq "legacy") {
            $legacyHeaders = [System.Net.WebHeaderCollection]::new()
            foreach ($header in $response.headers.PSObject.Properties) {
                $legacyHeaders.Add($header.Name, [string]$header.Value)
            }
            $httpResponse = [PSCustomObject]@{
                StatusCode = $response.status; Headers = $legacyHeaders
            }
        }
        $exception = [System.Exception]::new("Synthetic HTTP failure")
        $exception | Add-Member -NotePropertyName Response -NotePropertyValue $httpResponse
        $record = [System.Management.Automation.ErrorRecord]::new(
            $exception, "SyntheticHttpError", "InvalidOperation", $null
        )
        $record.ErrorDetails = [System.Management.Automation.ErrorDetails]::new(
            ($response.body | ConvertTo-Json -Compress)
        )
        throw $record
    }
    return $response.body
}

function New-Item {
    param([string]$ItemType, [switch]$Force, [string]$Path)
    [Console]::Error.WriteLine("METADATA_CHECKPOINT")
    exit 97
}
function Invoke-WebRequest { throw "Unexpected download" }
function Start-Process { throw "Unexpected process/elevation" }
function Move-Item { throw "Unexpected move" }
function Copy-Item { throw "Unexpected copy" }
function Remove-Item { throw "Unexpected delete" }
function Set-Content { throw "Unexpected content write" }
function Add-Content { throw "Unexpected profile write" }

try {
    . "$env:TEST_ROOT/install.ps1"
} finally {
    foreach ($key in $originalDefaults.Keys) {
        if ($PSDefaultParameterValues[$key] -cne $originalDefaults[$key]) {
            throw "Unexpected caller default mutation"
        }
    }
}
exit $LASTEXITCODE
