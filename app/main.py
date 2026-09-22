from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

import structlog
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Scope

import asyncio

from app.agent_graph import agent_graph_context
from app.conversation_memory import sweep_loop
from app.auth import (
    CurrentUser,
    get_current_user,
    oidc_settings,
    require_user,
    scope_customer_id,
)
from app.database import (
    create_order_row,
    database_counts,
    initialize_database,
    list_order_rows,
    list_product_rows,
)
from app.routes.agent import router as agent_router
from app.temporal_service import initialize_temporal_client

# Compiled Angular bundle (`cd frontend && npm run build` writes it here).
UI_DIR = Path(__file__).parent / "static"


def configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ]
    )


configure_logging()
logger = structlog.get_logger()


class ProductCategory(StrEnum):
    LAPTOP = "laptop"
    PRINTER = "printer"
    SPARE_PART = "spare_part"


class OrderStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    PROCESSING = "processing"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class Product(BaseModel):
    sku: str
    name: str
    category: ProductCategory
    price: Decimal = Field(gt=0)


class OrderCreate(BaseModel):
    customer_id: str = Field(min_length=1)
    product_sku: str
    quantity: int = Field(default=1, ge=1, le=10)


class Order(BaseModel):
    id: str
    customer_id: str
    product: Product
    quantity: int
    total: Decimal
    status: OrderStatus
    created_at: datetime


class Health(BaseModel):
    status: str
    orders: int
    products: int


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    order_count, product_count = database_counts()
    await initialize_temporal_client()
    async with agent_graph_context() as agent_graph:
        app.state.agent_graph = agent_graph
        # Closes conversations that have gone quiet, writing their summary to
        # Postgres. Runs for the lifetime of the process.
        sweeper = asyncio.create_task(sweep_loop(agent_graph))
        logger.info(
            "application_started",
            orders=order_count,
            products=product_count,
        )
        try:
            yield
        finally:
            sweeper.cancel()
            try:
                await sweeper
            except asyncio.CancelledError:
                pass
    logger.info("application_stopped")


app = FastAPI(title="Computer Store API", version="0.1.0", lifespan=lifespan)
app.include_router(agent_router)


@app.middleware("http")
async def log_request(request: Request, call_next):
    request_id = str(uuid4())
    structlog.contextvars.bind_contextvars(request_id=request_id)
    try:
        response = await call_next(request)
        logger.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
        )
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        structlog.contextvars.clear_contextvars()


@app.get("/health", response_model=Health, tags=["ops"])
def health() -> Health:
    order_count, product_count = database_counts()
    return Health(status="ok", orders=order_count, products=product_count)


@app.get("/auth/config", tags=["auth"])
def auth_config() -> dict:
    """Non-secret OIDC settings so the SPA does not hardcode the issuer."""
    return oidc_settings()


@app.get("/me", tags=["auth"])
def me(user: CurrentUser | None = Depends(get_current_user)) -> dict:
    """Who the caller is, as the API sees them. Anonymous is a valid answer."""
    if user is None:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "userId": user.user_id,
        "username": user.username,
        "customerId": user.customer_id,
        "roles": sorted(user.roles),
        "isStaff": user.is_staff,
        "isApprover": user.is_approver,
    }


@app.get("/products", response_model=list[Product], tags=["catalog"])
def list_products(category: ProductCategory | None = None) -> list[Product]:
    return [Product(**row) for row in list_product_rows(category)]


@app.get("/orders", response_model=list[Order], tags=["ordering"])
def list_orders(
    status: OrderStatus | None = None,
    customer_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    user: CurrentUser = Depends(require_user),
) -> list[Order]:
    """List orders the caller is entitled to see.

    Staff see every order, or one customer's when they ask. A customer only
    ever sees their own: the scope comes from their token claim, so passing a
    different customer_id is rejected rather than honoured.
    """
    scope = scope_customer_id(user, customer_id)
    return [order_from_row(row) for row in list_order_rows(status, limit, scope)]


@app.post("/orders", response_model=Order, status_code=201, tags=["ordering"])
def create_order(
    order_input: OrderCreate,
    user: CurrentUser = Depends(require_user),
) -> Order:
    """Create an order for the authenticated customer.

    The customer_id in the request body is advisory only; staff may use it to
    order on a customer's behalf, and for a customer it must match their own
    claim. The value actually written comes from scope_customer_id.
    """
    customer_id = scope_customer_id(user, order_input.customer_id)
    if customer_id is None:
        raise HTTPException(
            status_code=400,
            detail="customer_id is required when ordering as staff",
        )
    row = create_order_row(
        customer_id,
        order_input.product_sku,
        order_input.quantity,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Product not found")
    order = order_from_row(row)
    logger.info("order_created", order_id=order.id, customer_id=order.customer_id)
    return order


def order_from_row(row: dict) -> Order:
    return Order(
        id=f"ORD-{row['id']:03d}",
        customer_id=row["customer_id"],
        product=Product(
            sku=row["sku"],
            name=row["name"],
            category=row["category"],
            price=row["price"],
        ),
        quantity=row["quantity"],
        total=row["total"],
        status=row["status"],
        created_at=row["created_at"],
    )


# --- User interface -------------------------------------------------------
class SpaStaticFiles(StaticFiles):
    """Serve the bundle, falling back to index.html for client-side routes.

    The Angular router owns paths like /customer. Plain StaticFiles would 404 on
    a direct visit or refresh, because no such file exists on disk.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        # With html=True a missing file raises rather than returning a 404.
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as error:
            if error.status_code != 404:
                raise
            return await super().get_response("index.html", scope)

        if response.status_code == 404:
            return await super().get_response("index.html", scope)
        return response


# Mounted last so every API route above keeps priority over the SPA catch-all.
if (UI_DIR / "index.html").exists():
    app.mount("/", SpaStaticFiles(directory=UI_DIR, html=True), name="ui")
else:

    @app.get("/", include_in_schema=False)
    async def ui_missing() -> HTMLResponse:
        return HTMLResponse(
            "<h1>UI not built</h1>"
            "<p>Run <code>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</code>, "
            "then reload. During development use <code>npm start</code> on port 4200 instead.</p>"
            '<p><a href="/docs">Open the API docs</a></p>',
            status_code=503,
        )
