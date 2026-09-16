# POC — Microservices Architecture Style

Proof of Concept que demuestra, de forma práctica, los conceptos investigados sobre **Microservices
Architecture**: tres microservicios independientes (Customer Service, Order Service y Processing Service),
cada uno con responsabilidad única, persistencia propia y comunicación mediante **REST APIs**, empaquetados en
contenedores Docker independientes y orquestados con **Docker Compose**.

Grupo #1 — Investigación: Microservices Architecture Style
Universidad Invenio — Licenciatura en Tecnologías de la Información

## 1. Arquitectura

```
                    Cliente / Usuario
                            │
                            ▼  REST (HTTP/JSON)
                    ┌───────────────┐
      1. GET /customers/{id}   │  Order Service │  2. POST /process
        ┌───────────────────── │  (puerto 8002) │ ─────────────────────┐
        ▼                      └───────────────┘                       ▼
┌────────────────┐                                            ┌─────────────────────┐
│ Customer Service│                                            │ Processing Service  │
│  (puerto 8001)  │                                            │ (interno, escalable)│
└────────────────┘                                            └─────────────────────┘
```

Ver también `architecture-diagram.svg` / `architecture-diagram.png` para el diagrama completo.

Cada servicio:

- Corre en su propio contenedor, construido desde su propio `Dockerfile`.
- Mantiene su propio almacenamiento en memoria (simulando "database per service").
- Expone documentación interactiva OpenAPI/Swagger en `/docs`.
- Se comunica con los demás únicamente mediante su API REST, nunca accediendo a datos internos ajenos.

## 2. Requisitos

- Docker y Docker Compose (plugin `docker compose`) instalados.
- Puertos `8001` y `8002` libres en el host.

## 3. Cómo ejecutar el stack

```bash
cd microservices-poc
docker compose up --build
```

Esto construye y levanta los tres contenedores. Cuando estén listos:

| Servicio            | URL base                | Swagger / OpenAPI              |
|----------------------|--------------------------|---------------------------------|
| Customer Service     | http://localhost:8001    | http://localhost:8001/docs      |
| Order Service        | http://localhost:8002    | http://localhost:8002/docs      |
| Processing Service   | interno (sin puerto de host publicado; accesible solo dentro de la red de Compose) |

> Processing Service se deja **sin puerto publicado al host a propósito** (`expose` en vez de `ports`), para
> poder escalarlo a varias réplicas sin choques de puertos (ver sección 6). Se puede probar igualmente en
> aislamiento con `docker compose exec order-service curl http://processing-service:8000/health` o
> temporalmente agregando `ports: ["8003:8000"]` en `docker-compose.yml`.

Para detener y limpiar:

```bash
docker compose down
```

## 4. Demostración funcional (flujo completo)

### 4.1 Crear un cliente

```bash
curl -s -X POST http://localhost:8001/customers \
  -H "Content-Type: application/json" \
  -d '{"name": "Jose Pablo Monestel", "email": "jose@example.com"}' | jq
```

Respuesta (201 Created), incluye el `id` generado (UUID) — cópielo para el siguiente paso.

### 4.2 Crear una orden (Order Service valida el cliente y la envía a procesar)

```bash
curl -s -X POST http://localhost:8002/orders \
  -H "Content-Type: application/json" \
  -d '{
        "customer_id": "<ID_DEL_CLIENTE>",
        "items": ["laptop", "mouse"],
        "total": 950.00
      }' | jq
```

Internamente Order Service:

1. Llama a `GET customer-service:8000/customers/{id}` para validar que el cliente existe.
2. Persiste la orden en estado `created`.
3. Llama a `POST processing-service:8000/process` para procesar (validar inventario/pago).
4. Actualiza el estado final de la orden a `approved` o `rejected` según la respuesta.

### 4.3 Consultar la orden procesada

```bash
curl -s http://localhost:8002/orders/<ID_DE_LA_ORDEN> | jq
```

### 4.4 Documentación Swagger/OpenAPI

Abrir en el navegador:

- http://localhost:8001/docs (Customer Service)
- http://localhost:8002/docs (Order Service)

Desde ahí también se pueden probar todos los endpoints de forma interactiva ("Try it out").

## 5. Comunicación REST entre microservicios

- `order-service` → `customer-service`: `GET /customers/{id}` (validación síncrona antes de crear la orden).
- `order-service` → `processing-service`: `POST /process` (envío de la orden para su procesamiento).
- Todas las llamadas usan JSON sobre HTTP y se resuelven por nombre de servicio gracias al DNS interno de la
  red `microservices-net` creada por Docker Compose (no se usan IPs fijas).

## 6. Ejemplo de escalado independiente

Como `processing-service` es, en un escenario real, el que concentra más carga (validaciones de inventario y
pago), se puede escalar **de forma independiente** al resto sin tocar Customer Service ni Order Service:

```bash
docker compose up -d --scale processing-service=3
docker compose ps
```

Esto crea 3 réplicas del contenedor `processing-service`. Al no tener un puerto de host fijo (se usa `expose`,
no `ports`), Compose puede levantar varias réplicas sin conflicto. Order Service sigue llamando a
`http://processing-service:8000`; el DNS interno de Docker resuelve ese nombre entre las réplicas disponibles.

> Nota técnica para la investigación: en un entorno de producción real, para lograr *balanceo de carga* real
> entre las réplicas (y no solo resolución DNS) se añadiría un proxy/gateway (NGINX, Traefik) o se usaría un
> orquestador como Kubernetes con su propio mecanismo de Service + kube-proxy.

## 7. ¿Qué sucede si un servicio deja de estar disponible?

Este POC implementa manejo explícito de fallos en `order-service` (timeouts + reintentos + fallback) para
ilustrar el principio de **aislamiento de fallos**:

**Caso A — Customer Service cae:**

```bash
docker compose stop customer-service
curl -i -X POST http://localhost:8002/orders -H "Content-Type: application/json" \
  -d '{"customer_id":"cualquier-id","items":["item"],"total":10}'
```

Order Service detecta el `ConnectError`/timeout, reintenta un par de veces y responde
**`503 Service Unavailable`** con un mensaje claro, en vez de quedarse colgado o crashear. La orden **no se
crea** (no se puede garantizar que el cliente exista), evitando datos inconsistentes.

```bash
docker compose start customer-service   # restaurar el servicio
```

**Caso B — Processing Service cae (después de que la orden ya fue validada):**

```bash
docker compose stop processing-service
curl -s -X POST http://localhost:8002/orders -H "Content-Type: application/json" \
  -d '{"customer_id":"<ID_DEL_CLIENTE>","items":["item"],"total":10}' | jq
```

En este caso Order Service **no pierde la orden**: la persiste con estado `processing_unavailable` y responde
`201 Created` igualmente (degradación elegante). Al recuperar el servicio, se puede reprocesar manualmente:

```bash
docker compose start processing-service
curl -s -X POST http://localhost:8002/orders/<ID_DE_LA_ORDEN>/retry-processing | jq
```

Esto demuestra en la práctica el concepto de **aislamiento de fallos**: la caída de un microservicio no
provoca una caída en cascada de todo el sistema, y el sistema puede recuperarse ("self-healing" manual en este
POC didáctico; en producción esto se automatizaría con colas de mensajes/reintentos programados).

## 8. Estructura del proyecto

```
microservices-poc/
├── customer-service/
│   ├── main.py
│   ├── requirements.txt
│   └── Dockerfile
├── order-service/
│   ├── main.py
│   ├── requirements.txt
│   └── Dockerfile
├── processing-service/
│   ├── main.py
│   ├── requirements.txt
│   └── Dockerfile
├── architecture-diagram.svg
├── docker-compose.yml
└── README.md
```

## 9. Tecnologías utilizadas

- Python 3.12
- FastAPI + Uvicorn
- httpx (cliente HTTP asíncrono para la comunicación entre servicios)
- Docker / Docker Compose
- OpenAPI / Swagger (generado automáticamente por FastAPI)
