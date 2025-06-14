import stripe
from cachetools import TTLCache
from datetime import datetime, timezone
from typing import Any, TypeVar
import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import Session
from unboil_fastapi_stripe.config import Config
from unboil_fastapi_stripe.models import Models
from unboil_fastapi_stripe.utils import delete, fetch_all, fetch_one, paginate, save

T = TypeVar("T")
UNSET: Any = object()

class Service:
    
    def __init__(self, models: Models, config: Config):
        self.models = models
        self.config = config
        self._fetch_price_cache = TTLCache(maxsize=100, ttl=60)
        
    async def fetch_price(self, price_id: str) -> stripe.Price:
        if price_id in self._fetch_price_cache:
            return self._fetch_price_cache[price_id]
        result = await stripe.Price.retrieve_async(
            api_key=self.config.stripe_api_key,
            id=price_id,
        )
        self._fetch_price_cache[price_id] = result
        return result

    async def find_subscription(
        self,
        db: AsyncSession | Session,
        user_id: Any = UNSET,
        stripe_subscription_item_id: str = UNSET,
        stripe_product_id_in: list[str] = UNSET,
    ):
        query = select(self.models.Subscription)
        if user_id is not UNSET:
            query = query.where(
                self.models.Subscription.customer.has(
                    self.models.Customer.user_id == user_id,
                ),
            )
        if stripe_product_id_in is not UNSET:
            query = query.where(
                self.models.Subscription.stripe_product_id.in_(stripe_product_id_in),
            )
        if stripe_subscription_item_id is not UNSET:
            query = query.where(
                self.models.Subscription.stripe_subscription_item_id == stripe_subscription_item_id,
            )
        return await fetch_one(db=db, query=query)

    async def list_subscriptions(
        self,
        db: AsyncSession | Session,
        offset: int = 0,
        limit: int | None = None,
        user_id: Any = UNSET,
        stripe_subscription_item_ids: list[str] = UNSET,
    ):
        query = select(self.models.Subscription).order_by(
            self.models.Subscription.created_at.desc()
        )
        if user_id is not UNSET:
            query = query.where(
                self.models.Subscription.customer.has(
                    self.models.Customer.user_id == user_id
                ),
            )
        if stripe_subscription_item_ids is not UNSET:
            query = query.where(
                self.models.Subscription.stripe_subscription_item_id.in_(stripe_subscription_item_ids),
            )
        return await paginate(db=db, query=query, offset=offset, limit=limit)


    async def create_or_update_subscription(
        self,
        db: AsyncSession | Session,
        stripe_subscription_item_id: str,
        stripe_product_id: str,
        customer_id: uuid.UUID,
        current_period_end: datetime | int,
        auto_commit: bool = True,
    ):
        if isinstance(current_period_end, int):
            current_period_end = datetime.fromtimestamp(current_period_end, tz=timezone.utc)
        subscription = await self.find_subscription(
            db=db,
            stripe_subscription_item_id=stripe_subscription_item_id,
        )
        if subscription is None:
            subscription = self.models.Subscription(
                customer_id=customer_id,
                current_period_end=current_period_end,
                stripe_product_id=stripe_product_id,
                stripe_subscription_item_id=stripe_subscription_item_id,
            )
            await save(db=db, instances=subscription, auto_commit=auto_commit)
        else:
            subscription.customer_id = customer_id
            subscription.current_period_end = current_period_end
            subscription.stripe_product_id = stripe_product_id
            subscription.stripe_subscription_item_id = stripe_subscription_item_id
            await save(db=db, instances=subscription, auto_commit=auto_commit)
        return subscription


    async def create_or_update_subscriptions_from_stripe_subscription(
        self,
        db: AsyncSession | Session,
        stripe_subscription: stripe.Subscription,
    ):
        if isinstance(stripe_subscription.customer, stripe.Customer):
            stripe_customer_id = stripe_subscription.customer.id
        else:
            stripe_customer_id = stripe_subscription.customer
        customer = await self.find_customer(
            db=db, stripe_customer_id=stripe_customer_id
        )
        if customer is None:
            return
        subscription_items: list[stripe.SubscriptionItem] = stripe_subscription["items"]["data"]
        for item in subscription_items:
            if isinstance(item.price.product, stripe.Product):
                product_id = item.price.product.id
            else:
                product_id = item.price.product
            await self.create_or_update_subscription(
                db=db,
                customer_id=customer.id,
                stripe_product_id=product_id,
                stripe_subscription_item_id=item.id,
                current_period_end=item.current_period_end,
            )


    async def delete_subscriptions_from_stripe_subscription(
        self,
        db: AsyncSession | Session,
        stripe_subscription: stripe.Subscription,
    ):
        subscription_items: list[stripe.SubscriptionItem] = stripe_subscription["items"]["data"]
        subscriptions = self.list_subscriptions(
            db=db,
            stripe_subscription_item_ids=[item.id for item in subscription_items]
        )
        await delete(
            db=db,
            instances=subscriptions,
        )

    async def create_customer(
        self, 
        db: AsyncSession | Session,
        user_id: Any,
        name: str | None = None,
        email: str | None = None,
    ):
        stripe_customer = stripe.Customer.create(
            api_key=self.config.stripe_api_key,
            name=name or "",
            email=email or "",
        )
        customer = self.models.Customer(
            user_id=user_id,
            stripe_customer_id=stripe_customer.id,
        )
        await save(db=db, instances=customer)
        return customer


    async def find_customer(
        self, 
        db: AsyncSession | Session, 
        user_id: Any = UNSET,
        stripe_customer_id: str = UNSET,
    ):
        query = select(self.models.Customer)
        if user_id is not UNSET:
            query = query.where(
                self.models.Customer.user_id == user_id,
            )
        if stripe_customer_id is not UNSET:
            query = query.where(
                self.models.Customer.stripe_customer_id == stripe_customer_id
            )
        return await fetch_one(db=db, query=query)

    async def ensure_customer(
        self,
        db: AsyncSession | Session,
        user_id: Any,
        name: str | None = None,
        email: str | None = None,
    ):
        found = await self.find_customer(
            db=db, user_id=user_id
        )
        if found is not None:
            return found
        return await self.create_customer(
            db=db,
            user_id=user_id,
            name=name,
            email=email,
        )


# async def update_subscription_from_stripe(
#     db: AsyncSession,
#     stripe_subscription: stripe.Subscription
# ):
