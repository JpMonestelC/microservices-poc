"""
Order Service
-------------
Microservicio responsable del dominio "Orden". Es el orquestador simple del
flujo de negocio del POC:

  1. Recibe la petición de creación de una orden.
  2. Valida que el cliente exista llamando, vía REST, a Customer Service.
  3. Envía la orden a Processing Service para su procesamiento (pago/inventario).
  4. Persiste el resultado en su propio almacenamiento (aislado del resto).

Este servicio demuestra:
- Comunicación síncrona entre microservicios mediante REST APIs.
- Manejo explícito de fallos de red (timeouts, servicio caído) para no
  propagar una caída en cascada: aislamiento de fallos.
- Independencia de despliegue/datos: Order Service NO accede a las bases de
  datos de Customer Service ni de Processing Service, solo a sus APIs.
"""
import os
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(
    title="Order Service",
    description="Microservicio encargado de la gestión y orquestación de órdenes.",
    version="1.0.0",
)

# CORS: permite que el dashboard HTML (abierto como archivo local o en otro
# puerto) pueda llamar a esta API directamente desde el navegador. Solo para
# fines de demo del POC; en producción se restringiría a orígenes conocidos.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# URLs de los otros microservicios. En Docker Compose se resuelven por el nombre
# del servicio gracias al DNS interno de la red de Compose (service discovery).
CUSTOMER_SERVICE_URL = os.getenv("CUSTOMER_SERVICE_URL", "http://customer-service:8000")
PROCESSING_SERVICE_URL = os.getenv("PROCESSING_SERVICE_URL", "http://processing-service:8000")

# Timeouts cortos + reintentos controlados: patrón de resiliencia básico
# para no dejar al cliente esperando indefinidamente si un servicio está caído.
REQUEST_TIMEOUT_SECONDS = 3.0
MAX_RETRIES = 2


class OrderStatus(str, Enum):
    CREATED = "created"
    APPROVED = "approved"
    REJECTED = "rejected"
    PROCESSING_UNAVAILABLE = "processing_unavailable"


class OrderCreate(BaseModel):
    customer_id: str
    items: List[str] = Field(..., min_length=1)
    total: float = Field(..., gt=0)
    simulate_processing_failure: bool = False


class Order(BaseModel):
    id: str
    customer_id: str
    items: List[str]
    total: float
    status: OrderStatus
    processing_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime


_orders: Dict[str, Order] = {}


async def _call_with_retries(request_fn, retries: int = MAX_RETRIES):
    """Ejecuta una llamada HTTP con reintentos simples ante errores de red/timeout."""
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
    return {"status": "ok", "service": "order-service"}


@app.post("/orders", response_model=Order, status_code=status.HTTP_201_CREATED, tags=["Orders"])
async def create_order(payload: OrderCreate):
    now = datetime.now(timezone.utc)

    # --- Paso 1: validar que el cliente exista (llamada REST a Customer Service) ---
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        try:
            response = await _call_with_retries(
                lambda: client.get(f"{CUSTOMER_SERVICE_URL}/customers/{payload.customer_id}")
            )
        except (httpx.ConnectError, httpx.TimeoutException):
            # Customer Service no disponible: no podemos garantizar la integridad
            # del pedido, así que fallamos rápido con un mensaje claro (fail fast)
            # en vez de dejar al usuario esperando o crear una orden inconsistente.
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
    """Llama a Processing Service. Si no está disponible, la orden NO se pierde:
    queda marcada como PROCESSING_UNAVAILABLE para poder reintentarse luego.
    Esto ilustra el aislamiento de fallos: la caída de Processing Service no
    tumba a Order Service ni pierde datos ya persistidos."""
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
    """Permite reprocesar una orden que quedó PROCESSING_UNAVAILABLE, por ejemplo
    después de que Processing Service se recupera de una caída."""
    order = _orders.get(order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return await _send_to_processing(order)


@app.get("/orders", response_model=List[Order], tags=["Orders"])
def list_orders():
    return list(_orders.values())


@app.get("/orders/{order_id}", response_model=Order, tags=["Orders"])
def get_order(order_id: str):
    order = _orders.get(order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return order
