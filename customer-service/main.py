"""
Customer Service
-----------------
Microservicio responsable exclusivamente del dominio "Cliente" (bounded
context "Customer" en términos de Domain-Driven Design).

Responsabilidad única: este servicio SOLO sabe crear, listar y consultar
clientes. No sabe nada de órdenes ni de procesamiento de pagos — esas son
responsabilidades de Order Service y Processing Service, respectivamente.
Ningún otro servicio lee ni escribe directamente en los datos de este
servicio: si necesitan un dato de cliente, deben pedirlo por su API REST
(por ejemplo, Order Service hace GET /customers/{id} antes de crear una
orden). Esto es lo que en la investigación se describe como "APIs bien
definidas" y "los servicios se tratan entre sí como cajas negras".

Principios de Microservices Architecture demostrados aquí:
- Responsabilidad única (bounded context "Customer").
- Persistencia propia y aislada ("database per service"): el diccionario
  `_customers` de abajo vive únicamente en el proceso de este contenedor;
  cuando el contenedor se reinicia, el "historial" se pierde (es in-memory,
  a propósito, para mantener el POC simple — en un caso real sería una base
  de datos propia de este servicio, por ejemplo PostgreSQL o MongoDB).
- Despliegue independiente: este servicio se compila, prueba y despliega en
  su propio contenedor (ver customer-service/Dockerfile), sin coordinar con
  los otros dos.
- Documentación autogenerada vía OpenAPI/Swagger (disponible en /docs),
  generada automáticamente por FastAPI a partir de los tipos declarados
  abajo — no se escribe a mano.
"""
from datetime import datetime, timezone
from typing import Dict
from uuid import uuid4

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field

app = FastAPI(
    title="Customer Service",
    description="Microservicio encargado de la gestión de clientes.",
    version="1.0.0",
)

# CORS: permite que un cliente HTTP en el navegador (el dashboard, o Swagger
# UI de otro origen) pueda llamar a esta API directamente. Solo para fines de
# demo del POC; en producción se restringiría a una lista blanca de orígenes.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class CustomerCreate(BaseModel):
    """Datos que el cliente de la API debe enviar para crear un Customer.

    FastAPI usa este modelo para: (1) validar automáticamente el body del
    POST /customers, (2) generar el schema que se ve en Swagger (/docs), y
    (3) rechazar con 422 cualquier payload que no cumpla las reglas (por
    ejemplo, un email con formato inválido, gracias a `EmailStr`).
    """

    name: str = Field(..., min_length=1, examples=["Jose Pablo Monestel"])
    email: EmailStr = Field(..., examples=["jose@example.com"])


class Customer(CustomerCreate):
    """Representación completa de un cliente ya persistido.

    Extiende CustomerCreate (hereda name/email) y agrega los campos que
    genera el propio servicio al crear el recurso: un id único (UUID) y la
    marca de tiempo de creación. Es el modelo que se devuelve en las
    respuestas (`response_model=Customer`).
    """

    id: str
    created_at: datetime


# "Base de datos" propia del servicio: un diccionario en memoria, exclusivo
# de este proceso/contenedor. Simula el principio "database per service" sin
# necesitar levantar una base de datos real para el POC. La llave es el id
# del cliente (UUID como string); el valor es el objeto Customer completo.
_customers: Dict[str, Customer] = {}


@app.get("/health", tags=["Health"])
def health_check():
    """Endpoint de salud.

    Lo usan tres consumidores distintos: (1) el `healthcheck` opcional de
    Docker/orquestadores para saber si el contenedor está listo, (2) Order
    Service (indirectamente, vía el dashboard) para pintar el semáforo de
    estado de este servicio, y (3) cualquier humano verificando manualmente
    que el contenedor levantó bien.
    """
    return {"status": "ok", "service": "customer-service"}


@app.post(
    "/customers",
    response_model=Customer,
    status_code=status.HTTP_201_CREATED,
    tags=["Customers"],
)
def create_customer(payload: CustomerCreate):
    """Crea un nuevo cliente.

    Genera un id único (UUID4) y una marca de tiempo, arma el objeto
    `Customer` completo y lo guarda en el almacenamiento en memoria de este
    servicio. Es el único lugar del sistema donde se crean clientes: Order
    Service nunca inserta directamente aquí, solo consulta vía GET.

    Responde 201 Created con el cliente ya creado (incluyendo su `id`), que
    es el dato que se necesita para el siguiente paso del flujo: crear una
    orden a nombre de este cliente.
    """
    customer = Customer(
        id=str(uuid4()),
        name=payload.name,
        email=payload.email,
        created_at=datetime.now(timezone.utc),
    )
    _customers[customer.id] = customer
    return customer


@app.get("/customers", response_model=list[Customer], tags=["Customers"])
def list_customers():
    """Devuelve todos los clientes creados hasta el momento (para depuración
    e inspección manual vía Swagger; el dashboard no usa este endpoint)."""
    return list(_customers.values())


@app.get("/customers/{customer_id}", response_model=Customer, tags=["Customers"])
def get_customer(customer_id: str):
    """Consulta un cliente por id.

    Este es el endpoint clave para la comunicación entre microservicios: es
    exactamente el que Order Service invoca (`GET /customers/{id}`) para
    validar que un cliente existe antes de crear una orden a su nombre. Si
    no existe, responde 404, y Order Service traduce eso en un 404 propio
    hacia quien pidió crear la orden.
    """
    customer = _customers.get(customer_id)
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    return customer
