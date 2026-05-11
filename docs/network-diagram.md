# Diagrama de Red y Flujos de Datos — Azul Pagos Atlas

**Organización:** iamAtlas  
**Sistema:** Azul Pagos Atlas — Gateway de pagos integrado con Azul (BPD)  
**Versión del documento:** 1.0  
**Fecha de emisión:** 11 de mayo de 2026  
**Próxima revisión:** 11 de noviembre de 2026  
**Clasificación:** Confidencial

---

## 1. Descripción General del Sistema

Azul Pagos Atlas es una plataforma de pagos en la nube diseñada para procesar
transacciones contra el gateway de AZUL de Banco Popular Dominicano. El sistema
opera sobre infraestructura AWS (Amazon Web Services) en la región **us-east-2**
y cumple con los controles de seguridad requeridos por PCI DSS v4.0.

El sistema soporta los siguientes tipos de transacción:

- Pagos únicos con tarjeta (Sale CIT)
- Pagos de servicios (utilities)
- Pagos con tarjeta guardada (CIT con DataVault token)
- Suscripciones recurrentes (MIT con DataVault)
- Tokenización de tarjetas (DataVault CREATE / DELETE)
- Cancelaciones y devoluciones (Void / Refund)
- Autenticación 3D Secure 2.0

---

## 2. Diagrama de Red del CDE

```
┌──────────────────────────────────────────────────────────────────┐
│                          INTERNET                                │
│             (Clientes, aplicaciones integradas)                  │
└───────────────────────────┬──────────────────────────────────────┘
                            │
                            │  HTTPS — TLS 1.2 / TLS 1.3
                            │  Puerto 443
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│         Application Load Balancer (AWS ALB)                      │
│         ecs-express-gateway-alb-6093cc50                        │
│                                                                  │
│         Seguridad de red:                                        │
│         · Security Group: sg-02248386c26b20a1a                   │
│         · Inbound:  TCP 443 — desde Internet (0.0.0.0/0)        │
│         · Outbound: TCP 8000 — hacia ECS Task (VPC interna)     │
│                                                                  │
│         · Terminación TLS — certificado SSL gestionado por AWS  │
│         · Health check: GET /health — respuesta 200 OK          │
└───────────────────────────┬──────────────────────────────────────┘
                            │
                            │  HTTP — Puerto 8000
                            │  Red privada VPC (sin exposición a Internet)
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│         ECS Task — pago-azul (AWS Fargate)                       │
│         Imagen: ECR / pago-azul:latest                          │
│         Runtime: Python 3.12 + FastAPI + Uvicorn                │
│                                                                  │
│         Seguridad de red:                                        │
│         · Security Group: sg-0b48c28bfac35bb6e                   │
│         · Inbound:  TCP 8000 — exclusivamente desde ALB SG      │
│         · Outbound: TCP 443 — hacia AZUL API y AWS APIs         │
│                                                                  │
│         Autenticación de API:                                    │
│         · Header X-API-Key con validación timing-safe           │
│         · Secretos gestionados en AWS Secrets Manager           │
└───────────────┬──────────────────────────────┬───────────────────┘
                │                              │
                │  HTTPS + mTLS               │  HTTPS — TLS 1.2+
                │  Puerto 443                 │  Puerto 443
                │  Certificado de cliente     │  IAM Role (OIDC)
                │  emitido por BPD            │
                ▼                              ▼
┌───────────────────────────┐   ┌─────────────────────────────────┐
│  AZUL Payment Gateway     │   │  AWS Secrets Manager            │
│                           │   │                                 │
│  Sandbox:                 │   │  Secretos almacenados:          │
│  pruebas.azul.com.do      │   │  · Credenciales mTLS (cert/key) │
│                           │   │  · Claves de autenticación API  │
│  Producción:              │   │  · Parámetros de configuración  │
│  pagos.azul.com.do        │   │                                 │
│  contpagos.azul.com.do    │   │  Cifrado: AWS KMS               │
│                           │   │  Acceso: IAM Role mínimo        │
└───────────────┬───────────┘   └─────────────────────────────────┘
                │
                │  Red interna BPD / CARDNET
                ▼
┌───────────────────────────┐
│  BPD / CARDNET            │
│  Redes de tarjetas        │
│  (Visa, Mastercard)       │
└───────────────────────────┘
```

---

## 3. Flujos de Datos de Tarjetahabiente (CHD)

| # | Origen | Destino | Protocolo | Datos transmitidos | Cifrado en tránsito |
|---|---|---|---|---|---|
| 1 | Cliente / aplicación | ALB | HTTPS TLS 1.2+ | PAN, CVV, expiración, nombre | ✅ TLS |
| 2 | ALB | ECS Task | HTTP (VPC privada) | PAN, CVV, expiración, nombre | ✅ VPC aislada |
| 3 | ECS Task | AZUL API | HTTPS + mTLS mutual | PAN, CVV, expiración | ✅ mTLS |
| 4 | ECS Task | Base de datos | Proceso local (contenedor) | Referencia de orden, token | ✅ Contenedor aislado |
| 5 | ECS Task | Secrets Manager | HTTPS TLS | Sin datos CHD | ✅ TLS + IAM |

> La aplicación **no almacena PAN ni CVV** en ningún momento.
> Los datos de tarjeta se envían directamente al vault de AZUL y se devuelve
> únicamente un token opaco (`DataVaultToken`) para cobros futuros.

---

## 4. Datos en Reposo

| Dato | Ubicación | Cifrado en reposo | Mecanismo |
|---|---|---|---|
| Registros de transacciones (estado, monto, referencia) | Base de datos del contenedor | ✅ Cifrado | Clave gestionada en AWS Secrets Manager |
| DataVault tokens (referencias opacas, no son PAN) | Base de datos del contenedor | ✅ Cifrado | Clave gestionada en AWS Secrets Manager |
| PAN / CVV | **No almacenados** | N/A | Tokenización delegada a AZUL DataVault |
| Certificados mTLS | AWS Secrets Manager | ✅ AWS KMS | Nunca escritos en disco del contenedor |
| Claves de API y configuración | AWS Secrets Manager | ✅ AWS KMS | Inyectadas como variables de entorno en runtime |

---

## 5. Seguridad de Red — Security Groups

### ALB — `sg-02248386c26b20a1a`

| Dirección | Puerto | Origen / Destino | Protocolo | Justificación |
|---|---|---|---|---|
| Inbound | 443 | 0.0.0.0/0 | TCP/HTTPS | Recepción de solicitudes de clientes |
| Outbound | 8000 | `sg-0b48c28bfac35bb6e` | TCP | Reenvío exclusivo hacia ECS Task |

### ECS Task — `sg-0b48c28bfac35bb6e`

| Dirección | Puerto | Origen / Destino | Protocolo | Justificación |
|---|---|---|---|---|
| Inbound | 8000 | `sg-02248386c26b20a1a` | TCP | Exclusivamente desde ALB — sin acceso directo de Internet |
| Outbound | 443 | 0.0.0.0/0 | TCP/HTTPS | AZUL API + AWS APIs (Secrets Manager, ECR) |

---

## 6. Controles de Seguridad Implementados

### Autenticación y Acceso

| Control | Implementación |
|---|---|
| Autenticación de API | Header `X-API-Key` obligatorio en todos los endpoints de negocio |
| Comparación de claves | `secrets.compare_digest` — resistente a timing attacks |
| Acceso a infraestructura AWS | IAM Role con OIDC — sin credenciales estáticas |
| Documentación de Swagger | Deshabilitada en entorno de producción |

### Cifrado

| Dato / Canal | Mecanismo |
|---|---|
| Datos en tránsito (clientes → API) | TLS 1.2 / TLS 1.3 terminado en ALB |
| Datos en tránsito (API → AZUL) | HTTPS + mTLS con certificado de cliente emitido por BPD |
| Datos en reposo | Cifrado con clave gestionada en AWS Secrets Manager |
| Secretos y credenciales | AWS Secrets Manager con cifrado AWS KMS |

### Auditoría y Trazabilidad

| Control | Implementación |
|---|---|
| Log de auditoría por request | Middleware de logging: método, ruta, IP de origen, código HTTP, latencia |
| Registro de transacciones | Cada transacción persiste su estado, referencia y resultado en base de datos |
| Política de retención | Registros históricos con eliminación automática configurable |

### Proceso de Despliegue (CI/CD)

| Control | Implementación |
|---|---|
| Análisis estático de seguridad (SAST) | Bandit — ejecutado en cada push a `main` |
| Escaneo de vulnerabilidades en dependencias | pip-audit — ejecutado antes de cada build |
| Autenticación con AWS | OIDC — sin claves estáticas `AWS_ACCESS_KEY_ID` |
| Control de acceso al código | GitHub con protección de rama `main` |

---

## 7. Flujo de Despliegue (CI/CD)

```
Repositorio GitHub (rama main)
         │
         │  Push / aprobación de PR
         ▼
GitHub Actions (Runner administrado)
  ① Análisis SAST — Bandit
  ② Escaneo de dependencias — pip-audit
  ③ Autenticación AWS via OIDC (sin claves estáticas)
  ④ Build de imagen Docker
  ⑤ Push a Amazon ECR
         │
         ▼
Amazon ECR
  293926505005.dkr.ecr.us-east-2.amazonaws.com/pago-azul:latest
         │
         ▼
ECS Service — actualización progresiva (rolling update)
```

> Los runners de CI/CD **no tienen acceso a datos de tarjetahabiente**.
> Solo interactúan con Amazon ECR (imágenes de contenedor) y AWS IAM (OIDC).

---

## 8. Revisión Periódica de Controles de Red

En conformidad con PCI DSS v4.0 Req 1.2.7, las reglas de red del CDE
se revisan formalmente cada seis (6) meses.

| Fecha de revisión | Responsable | Estado |
|---|---|---|
| 11 de mayo de 2026 | iamAtlas DevOps | ✅ Completada |
| 11 de noviembre de 2026 | iamAtlas DevOps | Programada |
| 11 de mayo de 2027 | iamAtlas DevOps | Programada |

---

*Documento de cumplimiento PCI DSS v4.0 — Req 1.2.4, 1.2.7*  
*iamAtlas — Confidencial*
