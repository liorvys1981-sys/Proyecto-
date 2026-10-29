# YIMI Platform — SaaS empresarial de agentes autónomos

Base funcional para desarrollar una plataforma SaaS multiempresa. Incluye una API FastAPI con autenticación por tenant, un flujo de agentes simulado y una imagen Docker que se ejecuta sin privilegios de root.

## Alcance actual

- `GET /health`: comprobación básica de disponibilidad.
- `POST /v1/agent-runs`: crea una ejecución simulada de análisis, riesgo y ejecución.
- `GET /v1/agent-runs/{run_id}`: consulta una ejecución propia del tenant autenticado.
- La ejecución no contacta sistemas externos ni realiza trading.
- Las ejecuciones se guardan en memoria: se pierden al reiniciar el proceso y no se comparten entre réplicas.

## Ejecutar localmente

Requiere Python 3.11 o posterior.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export TENANT_API_KEYS='{"empresa-demo":"reemplazar-con-una-clave-segura"}'
uvicorn app.main:app --reload
```

En Windows, activa el entorno virtual con `.venv\Scripts\activate`. Para generar una clave local segura, ejecuta `python -c "import secrets; print(secrets.token_urlsafe(32))"` y configura el resultado para el tenant.

La API interactiva queda disponible en `http://localhost:8000/docs`.

## Ejemplo de API

Envía el identificador de tenant y sus credenciales:

```bash
curl -X POST http://localhost:8000/v1/agent-runs \
  -H 'Content-Type: application/json' \
  -H 'X-Tenant-ID: empresa-demo' \
  -H "Authorization: $AUTH_HEADER" \
  -d '{"goal":"Analizar el rendimiento del servicio"}'
```

`AUTH_HEADER` debe contener el esquema de autorización HTTP y la clave del tenant. La respuesta incluye el ID de ejecución; úsalo en `GET /v1/agent-runs/{run_id}` con las mismas credenciales.

## Docker

```bash
docker build -t yimi-platform .
docker run --rm -p 8000:8000 \
  -e 'TENANT_API_KEYS={"empresa-demo":"reemplazar-con-una-clave-segura"}' \
  yimi-platform
```

No uses claves de ejemplo en producción ni las guardes en el repositorio, historial de shell o imágenes. Inyecta claves reales desde un gestor de secretos.

## Modelo SaaS y siguientes pasos

Cada tenant se autentica con una clave propia configurada en `TENANT_API_KEYS`; el servidor valida la clave contra el tenant antes de aceptar una operación. Los registros consultados se filtran por el tenant autenticado y los recursos de otros tenants responden como inexistentes.

Esta base **no está lista para producción**. Antes de ofrecerla como SaaS empresarial, integra un proveedor de identidad y almacenamiento seguro de credenciales, persistencia compartida con aislamiento por tenant, gestión de secretos y rotación, límites de uso, auditoría, métricas y trazas, colas de trabajo, políticas de retención, copias de seguridad y despliegue con TLS. Sustituye el flujo simulado por agentes y herramientas con permisos explícitos, aprobaciones y controles de riesgo.

## Configuración

`TENANT_API_KEYS` es un objeto JSON que relaciona IDs de tenant con claves. La configuración se carga una vez por proceso; para aplicar cambios, reinicia el servicio. Las credenciales ausentes o inválidas reciben `401`; si falta la configuración de tenant, las solicitudes autenticadas reciben `503`.
