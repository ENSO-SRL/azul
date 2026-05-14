Add-Type @"
using System;
using System.Net;
using System.Net.Http;
using System.Security.Cryptography.X509Certificates;
using System.Threading.Tasks;

public class TLSChecker {
    public static string CheckCert(string hostname, int port) {
        try {
            var req = WebRequest.Create("https://" + hostname + ":" + port + "/health") as HttpWebRequest;
            req.Timeout = 10000;
            req.Method = "GET";
            string certResult = "(no cert captured yet)";
            ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12 | SecurityProtocolType.Tls13;
            ServicePointManager.ServerCertificateValidationCallback = (sender, cert, chain, errors) => {
                var x509 = new X509Certificate2(cert);
                certResult = "Subject: " + x509.Subject
                    + "\nIssuer: " + x509.Issuer
                    + "\nNotBefore: " + x509.NotBefore
                    + "\nNotAfter: " + x509.NotAfter
                    + "\nThumbprint: " + x509.Thumbprint
                    + "\nSSL Errors: " + errors.ToString();
                return true;
            };
            try { req.GetResponse().Close(); } catch {}
            return certResult;
        } catch (Exception ex) {
            return "Error: " + ex.Message;
        }
    }

    public static string CheckEndpoint(string url, bool skipCert) {
        try {
            var handler = new HttpClientHandler();
            if (skipCert) handler.ServerCertificateCustomValidationCallback = HttpClientHandler.DangerousAcceptAnyServerCertificateValidator;
            var client = new HttpClient(handler);
            client.Timeout = TimeSpan.FromSeconds(8);
            var resp = client.GetAsync(url).Result;
            return ((int)resp.StatusCode).ToString();
        } catch (Exception ex) {
            var inner = ex.InnerException;
            return "ERROR: " + (inner != null ? inner.Message : ex.Message);
        }
    }
}
"@ -Language CSharp

$ECS_HOST = "pa-ad2039d606764cfd953ed8909489a2ec.ecs.us-east-2.on.aws"
$ECS_URL  = "https://$ECS_HOST"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  VERIFICACION CERTIFICADO TLS - ECS" -ForegroundColor Cyan
Write-Host "========================================`n" -ForegroundColor Cyan

Write-Host "[1] INFORMACION DEL CERTIFICADO TLS" -ForegroundColor Yellow
$certInfo = [TLSChecker]::CheckCert($ECS_HOST, 443)
Write-Host $certInfo -ForegroundColor Green

Write-Host "`n[2] ENDPOINTS - Sin validar cert (como cliente permisivo)" -ForegroundColor Yellow
foreach ($ep in @("/health", "/cert")) {
    $status = [TLSChecker]::CheckEndpoint("$ECS_URL$ep", $true)
    if ($status -match "^[0-9]") {
        Write-Host "  [OK $status] $ep" -ForegroundColor Green
    } else {
        Write-Host "  [FAIL] $ep -> $status" -ForegroundColor Red
    }
}

Write-Host "`n[3] CERTIFICADO ESTRICTO (como lo ve Modirum)" -ForegroundColor Yellow
[System.Net.ServicePointManager]::ServerCertificateValidationCallback = $null
$strictStatus = [TLSChecker]::CheckEndpoint("$ECS_URL/health", $false)
if ($strictStatus -match "^[0-9]") {
    Write-Host "  Certificado ACEPTADO por validacion estricta: HTTP $strictStatus" -ForegroundColor Green
    Write-Host "  -> El certificado ES valido. El problema podria ser otro." -ForegroundColor Yellow
} else {
    Write-Host "  Certificado RECHAZADO: $strictStatus" -ForegroundColor Red
    Write-Host "  >>> CONFIRMADO: Este es el motivo por el que Modirum no llega al callback" -ForegroundColor Magenta
}

Write-Host "`n[4] Probando /cert/notify/ y /cert/term/ directamente" -ForegroundColor Yellow
foreach ($ep in @("/cert/notify/TEST-RUN-123", "/cert/term/TEST-RUN-123")) {
    $status = [TLSChecker]::CheckEndpoint("$ECS_URL$ep", $true)
    Write-Host "  [$status] $ep" -ForegroundColor $(if ($status -match "^[0-9]") { "Green" } else { "Red" })
}
