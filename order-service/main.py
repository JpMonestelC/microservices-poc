"""
Order Service
-------------
Microservicio responsable del dominio "Orden". Es el orquestador simple del
flujo de negocio del POC: es el único de los tres servicios que llama a los
otros dos, coordinando el caso de uso completo "crear una orden".

Flujo que implementa `create_order`:

  1. Recibe la petición de creación de una orden.
  2. Valida que el cliente exista llamando, vía REST, a Customer Service
     (GET /customers/{id}).
  3. Persiste la orden en su propio almacenamiento, en estado `created`.
  4. Envía la orden a Processing Service para su procesamiento
     (POST /process), y actualiza el estado final según la respuesta.

Este servicio demuestra en código varios de los conceptos investigados:

- Comunicación síncrona entre microservicios mediante REST APIs (usando
  `httpx.AsyncClient`, un cliente HTTP asíncrono, para no bloquear el event
  loop de FastAPI mientras se espera la respuesta de otro servicio).
- Manejo explícito de fallos de red (timeouts cortos + reintentos) para no
  propagar una caída en cascada: aislamiento de fallos ("fault isolation").
- Independencia de despliegue/datos: Order Service NO accede a las bases de
  datos de Customer Service ni de Processing Service, solo a sus APIs REST
  ("database per service" — cada servicio es dueño exclusivo de sus datos).
- Degradación elegante ("graceful degradation"): si Processing Service no
  responde, la orden no se pierde ni se rompe el flujo; queda en un estado
  intermedio (`processing_unavailable`) listo para reintentarse.
"""
import os
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(
    title="Order Service",
    description="Microservicio encargado de la gestión y orquestación de órdenes.",
    version="1.0.0",
)

# CORS: permite que un cliente HTTP en el navegador (Swagger UI de otro
# origen, por ejemplo) pueda llamar a esta API directamente. Solo para fines
# de demo del POC; en producción se restringiría a una lista blanca de
# orígenes. El dashboard propio de este servicio (ver más abajo) ni siquiera
# necesita CORS, porque se sirve desde el mismo origen que consume.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# URLs de los otros microservicios. Se leen de variables de entorno (ver
# `environment:` en docker-compose.yml) con un valor por defecto que asume
# que se está corriendo dentro de la red de Docker Compose, donde el nombre
# del servicio funciona como hostname gracias al DNS interno (service
# discovery) — por eso "http://customer-service:8000" y no una IP fija.
CUSTOMER_SERVICE_URL = os.getenv("CUSTOMER_SERVICE_URL", "http://customer-service:8000")
PROCESSING_SERVICE_URL = os.getenv("PROCESSING_SERVICE_URL", "http://processing-service:8000")

# Timeouts cortos + reintentos controlados: patrón de resiliencia básico
# para no dejar al cliente esperando indefinidamente si un servicio está
# caído o muy lento (evita que un fallo en cascada bloquee a este servicio).
REQUEST_TIMEOUT_SECONDS = 3.0
MAX_RETRIES = 2


class OrderStatus(str, Enum):
    """Estados posibles del ciclo de vida de una orden.

    CREATED                 -> la orden existe y el cliente fue validado,
                                pero aún no se le envió a Processing Service
                                (estado transitorio, dura muy poco tiempo).
    APPROVED / REJECTED     -> Processing Service respondió y decidió.
    PROCESSING_UNAVAILABLE  -> Processing Service no respondió; la orden NO
                                se perdió, solo queda pendiente de un nuevo
                                intento (ver /orders/{id}/retry-processing).
    """

    CREATED = "created"
    APPROVED = "approved"
    REJECTED = "rejected"
    PROCESSING_UNAVAILABLE = "processing_unavailable"


class OrderCreate(BaseModel):
    """Datos que se envían para crear una orden nueva.

    `simulate_processing_failure` no es un dato de negocio real: es un flag
    exclusivamente para la demo, que Order Service reenvía tal cual a
    Processing Service para forzar un caso "rejected" reproducible (lo usa
    el botón "Simular rechazo" del dashboard).
    """

    customer_id: str
    items: List[str] = Field(..., min_length=1)
    total: float = Field(..., gt=0)
    simulate_processing_failure: bool = False


class Order(BaseModel):
    """Representación completa de una orden, tal como la persiste este
    servicio y tal como se devuelve en las respuestas de la API."""

    id: str
    customer_id: str
    items: List[str]
    total: float
    status: OrderStatus
    processing_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime


# "Base de datos" propia de este servicio (in-memory, exclusiva de Order
# Service). Igual que en los otros dos servicios, es deliberadamente simple
# para el POC; en un caso real sería una base de datos propia (principio
# "database per service"). Importante: al reiniciarse el contenedor, este
# diccionario se vacía — no hay persistencia en disco ni volumen montado.
_orders: Dict[str, Order] = {}


async def _call_with_retries(request_fn, retries: int = MAX_RETRIES):
    """Ejecuta una llamada HTTP (recibida como función sin argumentos, para
    poder reintentarla) y reintenta ante errores de conexión o timeout.

    No reintenta ante errores HTTP "normales" (4xx/5xx con respuesta), solo
    ante fallos de red (el servicio ni siquiera respondió): un 404, por
    ejemplo, no se reintenta porque reintentar no cambiaría el resultado.
    Si se agotan los intentos, vuelve a lanzar la última excepción de red
    capturada para que el llamador decida cómo manejarla.
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await request_fn()
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            last_exc = exc
            continue
    raise last_exc


@app.get("/health", tags=["Health"])
def health_check():
    """Endpoint de salud de este servicio (usado por Docker y por quien
    quiera verificar manualmente que Order Service está arriba)."""
    return {"status": "ok", "service": "order-service"}


@app.post("/orders", response_model=Order, status_code=status.HTTP_201_CREATED, tags=["Orders"])
async def create_order(payload: OrderCreate):
    """Crea una orden nueva, orquestando la llamada a los otros dos servicios.

    Este es el endpoint central del POC: el único que efectivamente coordina
    una transacción de negocio que involucra a los tres microservicios.
    """
    now = datetime.now(timezone.utc)

    # --- Paso 1: validar que el cliente exista (llamada REST a Customer Service) ---
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        try:
            response = await _call_with_retries(
                lambda: client.get(f"{CUSTOMER_SERVICE_URL}/customers/{payload.customer_id}")
            )
        except (httpx.ConnectError, httpx.TimeoutException):
            # Customer Service no disponible: no podemos garantizar la integridad
            # del pedido (no sabemos si el cliente realmente existe), así que
            # fallamos rápido con un mensaje claro ("fail fast") en vez de dejar
            # al usuario esperando o crear una orden inconsistente. Nótese que
            # aquí SÍ se corta el flujo por completo (no se crea la orden),
            # a diferencia de lo que pasa si el que falla es Processing Service
            # (ver más abajo) — la decisión de diseño es que sin cliente
            # validado no tiene sentido continuar.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Customer Service no está disponible en este momento. Intente más tarde.",
            )

        if response.status_code == status.HTTP_404_NOT_FOUND:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
        response.raise_for_status()

    # --- Paso 2: crear la orden en estado inicial (persistencia propia) ---
    order = Order(
        id=str(uuid4()),
        customer_id=payload.customer_id,
        items=payload.items,
        total=payload.total,
        status=OrderStatus.CREATED,
        created_at=now,
        updated_at=now,
    )
    _orders[order.id] = order

    # --- Paso 3: enviar la orden a Processing Service ---
    await _send_to_processing(order, simulate_failure=payload.simulate_processing_failure)
    return order


async def _send_to_processing(order: Order, simulate_failure: bool = False) -> Order:
    """Envía una orden a Processing Service y actualiza su estado según la respuesta.

    Se usa en dos lugares: dentro de `create_order` (primer intento) y desde
    `retry_processing` (reintentos manuales posteriores) — por eso está
    separada en su propia función, para no duplicar la lógica.

    Si Processing Service responde con éxito, la orden pasa a APPROVED o
    REJECTED según el resultado. Si Processing Service NO está disponible
    (caído o sin responder a tiempo), la orden **no se pierde**: queda
    marcada como PROCESSING_UNAVAILABLE, con su `processing_reason`
    explicando por qué, lista para reintentarse más tarde. Esto ilustra el
    aislamiento de fallos: la caída de Processing Service no tumba a Order
    Service ni descarta datos ya persistidos (a diferencia del caso de
    Customer Service, donde el fallo sí impide crear la orden — la
    diferencia de tratamiento es intencional y refleja qué tan crítico es
    cada paso del flujo de negocio).
    """
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        try:
            response = await _call_with_retries(
                lambda: client.post(
                    f"{PROCESSING_SERVICE_URL}/process",
                    json={
                        "order_id": order.id,
                        "customer_id": order.customer_id,
                        "items": order.items,
                        "total": order.total,
                        "simulate_failure": simulate_failure,
                    },
                )
            )
            response.raise_for_status()
            result = response.json()
            order.status = OrderStatus.APPROVED if result["status"] == "approved" else OrderStatus.REJECTED
            order.processing_reason = result.get("reason")
        except (httpx.ConnectError, httpx.TimeoutException):
            order.status = OrderStatus.PROCESSING_UNAVAILABLE
            order.processing_reason = "Processing Service no disponible; la orden quedó pendiente de reprocesar."

    order.updated_at = datetime.now(timezone.utc)
    _orders[order.id] = order
    return order


@app.post("/orders/{order_id}/retry-processing", response_model=Order, tags=["Orders"])
async def retry_processing(order_id: str):
    """Reintenta el procesamiento de una orden existente.

    Pensado para el caso PROCESSING_UNAVAILABLE: una vez que Processing
    Service se recupera de una caída, este endpoint permite reprocesar la
    misma orden (sin perder sus datos originales) y que quede resuelta con
    normalidad. También se puede llamar sobre una orden ya APPROVED/REJECTED
    para volver a procesarla, aunque en el flujo normal de la demo solo se
    usa sobre órdenes pendientes. Es exactamente lo que dispara el botón
    "Reintentar procesamiento" del dashboard.
    """
    order = _orders.get(order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return await _send_to_processing(order)


@app.get("/orders", response_model=List[Order], tags=["Orders"])
def list_orders():
    """Devuelve todas las órdenes registradas hasta el momento. La usa el
    dashboard para pintar la tabla "Órdenes registradas"."""
    return list(_orders.values())


@app.get("/orders/{order_id}", response_model=Order, tags=["Orders"])
def get_order(order_id: str):
    """Consulta una orden por id (endpoint auxiliar de inspección, útil vía
    Swagger para revisar el detalle completo de una orden puntual)."""
    order = _orders.get(order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return order


# ---------------------------------------------------------------------------
# Dashboard visual del POC + endpoints "gateway"
#
# Order Service ya tiene httpx configurado para hablar con Customer Service y
# Processing Service, así que también sirve como punto de entrada único para
# el dashboard HTML: el navegador solo habla con ESTE origen (Order Service),
# y es Order Service quien reenvía ("proxea") las llamadas a los otros dos
# servicios por detrás, del lado del servidor. Esto evita cualquier problema
# de CORS o de proxys de autenticación (por ejemplo, el de GitHub Codespaces,
# que redirige peticiones anónimas entre puertos) al abrir el dashboard: no
# importa si se corre local, con Docker Compose o en Codespaces, el navegador
# siempre habla con un solo origen ("same-origin"), sin configurar URLs.
#
# En términos de la investigación, esto es un patrón de API Gateway simple
# y hecho a mano: un servicio que concentra el tráfico de un cliente externo
# (aquí, el navegador) y lo reparte hacia los microservicios internos.
# ---------------------------------------------------------------------------

DASHBOARD_FILE = os.path.join(os.path.dirname(__file__), "dashboard.html")


@app.get("/dashboard", response_class=HTMLResponse, tags=["Dashboard"])
def dashboard():
    """Sirve el panel visual (dashboard.html) como HTML directamente desde
    Order Service, para poder demostrar el flujo completo del POC desde el
    navegador sin usar la terminal ni Swagger."""
    with open(DASHBOARD_FILE, "r", encoding="utf-8") as f:
        return f.read()


@app.post("/api/customers", tags=["Dashboard"])
async def proxy_create_customer(payload: dict):
    """Reenvía la creación de un cliente a Customer Service (para el dashboard).

    Es un simple "pass-through": recibe el mismo payload que espera
    POST /customers en Customer Service, lo reenvía tal cual, y devuelve la
    misma respuesta (mismo status code y mismo body) al navegador. El único
    valor agregado es que esta llamada ocurre del lado del servidor (Order
    Service a Customer Service, dentro de la red de Docker), evitando que el
    navegador tenga que hablar directamente con Customer Service.
    """
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        try:
            response = await _call_with_retries(
                lambda: client.post(f"{CUSTOMER_SERVICE_URL}/customers", json=payload)
            )
        except (httpx.ConnectError, httpx.TimeoutException):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Customer Service no está disponible en este momento.",
            )
    return JSONResponse(status_code=response.status_code, content=response.json())


@app.get("/api/health/customer", tags=["Dashboard"])
async def proxy_health_customer():
    """Reenvía el health check de Customer Service, para que el dashboard
    pueda pintar su semáforo de estado sin llamar directamente a ese
    servicio desde el navegador (mismo motivo que `proxy_create_customer`)."""
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        try:
            response = await client.get(f"{CUSTOMER_SERVICE_URL}/health")
            return JSONResponse(status_code=response.status_code, content=response.json())
        except (httpx.ConnectError, httpx.TimeoutException):
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="unavailable")


@app.get("/api/health/processing", tags=["Dashboard"])
async def proxy_health_processing():
    """Reenvía el health check de Processing Service, para el semáforo de
    estado del dashboard. Es especialmente relevante para este servicio en
    particular porque, al no tener un puerto de host publicado (ver
    docker-compose.yml), el navegador no podría llamarlo directamente aunque
    quisiera: este proxy es la única forma en que el dashboard sabe si
    Processing Service está arriba o abajo."""
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        try:
            response = await client.get(f"{PROCESSING_SERVICE_URL}/health")
            return JSONResponse(status_code=response.status_code, content=response.json())
        except (httpx.ConnectError, httpx.TimeoutException):
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="unavailable")
