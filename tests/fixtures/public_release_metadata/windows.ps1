# Stub transport only; execute the real installer until its first write.
$ErrorActionPreference = "Stop"

function Invoke-RestMethod {
    param([string]$Uri, [hashtable]$Headers = @{})
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

. "$env:TEST_ROOT/install.ps1"
exit $LASTEXITCODE
