"""
check_tls.py - Verifica el certificado TLS y alcanzabilidad del endpoint ECS
"""
import ssl
import socket
import urllib.request
import urllib.error
import json
from datetime import datetime

ECS_HOST = "pa-ad2039d606764cfd953ed8909489a2ec.ecs.us-east-2.on.aws"
ECS_URL = f"https://{ECS_HOST}"

def print_header(text, color="cyan"):
    colors = {"cyan": "\033[96m", "yellow": "\033[93m", "green": "\033[92m", "red": "\033[91m", "magenta": "\033[95m", "reset": "\033[0m"}
    c = colors.get(color, "")
    r = colors["reset"]
    print(f"{c}{text}{r}")

print_header("=" * 50)
print_header("  VERIFICACION TLS - ECS Express URL")
print_header("=" * 50)
print()

# ── 1. Certificado TLS ──────────────────────────────────────────────
print_header("[1] CERTIFICADO TLS DEL SERVIDOR", "yellow")
try:
    ctx_permisivo = ssl.create_default_context()
    ctx_permisivo.check_hostname = False
    ctx_permisivo.verify_mode = ssl.CERT_NONE

    conn = ssl.create_connection((ECS_HOST, 443), timeout=8)
    sock = ctx_permisivo.wrap_socket(conn, server_hostname=ECS_HOST)
    cert = sock.getpeercert()
    der_cert = sock.getpeercert(binary_form=True)
    sock.close()

    # Parsear campos del cert
    subject = dict(x[0] for x in cert.get("subject", []))
    issuer  = dict(x[0] for x in cert.get("issuer", []))
    not_before = cert.get("notBefore", "N/A")
    not_after  = cert.get("notAfter",  "N/A")
    san        = cert.get("subjectAltName", [])

    print_header(f"  Subject CN   : {subject.get('commonName', 'N/A')}", "green")
    print_header(f"  Issuer O     : {issuer.get('organizationName', 'N/A')}", "green")
    print_header(f"  Issuer CN    : {issuer.get('commonName', 'N/A')}", "green")
    print_header(f"  Valido Desde : {not_before}", "green")
    print_header(f"  Valido Hasta : {not_after}", "green")
    san_list = [v for t, v in san if t == "DNS"]
    print_header(f"  SANs (DNS)   : {', '.join(san_list[:5])}", "green")

except Exception as e:
    print_header(f"  Error capturando cert: {e}", "red")

print()

# ── 2. Alcanzabilidad ignorando cert ────────────────────────────────
print_header("[2] ALCANZABILIDAD (ignorando cert - prueba de red)", "yellow")
ctx_skip = ssl.create_default_context()
ctx_skip.check_hostname = False
ctx_skip.verify_mode = ssl.CERT_NONE

endpoints = ["/health", "/cert", "/cert/notify/TEST-RUN-123", "/cert/term/TEST-RUN-123"]
for ep in endpoints:
    url = ECS_URL + ep
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=8, context=ctx_skip) as resp:
            print_header(f"  [OK {resp.status}] {ep}", "green")
    except urllib.error.HTTPError as e:
        color = "yellow" if e.code < 500 else "red"
        print_header(f"  [HTTP {e.code}] {ep}", color)
    except Exception as e:
        print_header(f"  [CONN ERROR] {ep} -> {e}", "red")

print()

# ── 3. Validacion estricta (como Modirum) ────────────────────────────
print_header("[3] VALIDACION ESTRICTA TLS (como lo hace Modirum ACS)", "yellow")
ctx_strict = ssl.create_default_context()  # usa la CA store del sistema
ctx_strict.check_hostname = True
ctx_strict.verify_mode = ssl.CERT_REQUIRED

try:
    req = urllib.request.Request(ECS_URL + "/health", method="GET")
    with urllib.request.urlopen(req, timeout=8, context=ctx_strict) as resp:
        print_header(f"  CERT VALIDO para validacion estricta - HTTP {resp.status}", "green")
        print_header("  -> El problema NO es el TLS. Buscar otra causa.", "yellow")
except ssl.SSLCertVerificationError as e:
    print_header(f"  CERT RECHAZADO por SSL: {e}", "red")
    print_header("  >>> CONFIRMADO: Modirum rechazaria este cert - causa del IsoCode 08", "magenta")
except urllib.error.HTTPError as e:
    print_header(f"  Cert OK pero HTTP {e.code} - servidor responde pero ruta no existe", "yellow")
except Exception as e:
    if "CERTIFICATE" in str(e).upper() or "SSL" in str(e).upper():
        print_header(f"  CERT RECHAZADO: {e}", "red")
        print_header("  >>> CONFIRMADO: Este es el problema con Modirum", "magenta")
    else:
        print_header(f"  Error de conexion (no TLS): {e}", "red")
        print_header("  -> Podria ser timeout / servidor caido", "yellow")

print()
print_header("[4] RESUMEN Y RECOMENDACION", "yellow")
print("  Revisa arriba:")
print("  - Si Issuer es 'Amazon' o 'AWS' => cert de AWS, algunos ACMs rechazados por CAs externas")
print("  - Si [3] dice RECHAZADO => TLS es el problema, usar ngrok o dominio propio")
print("  - Si [2] falla con CONN ERROR => ECS no esta corriendo o puerto bloqueado")
