"""Create Vurenn's exact Stripe catalog without duplicating existing entries."""

import json
import os

import stripe


CATALOG = {
    "pro_monthly": ("Vurenn Pro", 999, {"interval": "month"}),
    "pro_annual": ("Vurenn Pro", 9900, {"interval": "year"}),
    "premier_monthly": ("Vurenn Premier", 1999, {"interval": "month"}),
    "premier_annual": ("Vurenn Premier", 19900, {"interval": "year"}),
    "credits_50": ("50 Vurenn Credits", 1299, None),
    "credits_100": ("100 Vurenn Credits", 2399, None),
}


def product_for(catalog_id, name):
    products = stripe.Product.list(active=True, limit=100).auto_paging_iter()
    for product in products:
        if product.metadata.get("vurenn_catalog_id") == catalog_id:
            return product
    return stripe.Product.create(
        name=name,
        metadata={"vurenn_catalog_id": catalog_id},
    )


def price_for(catalog_id, product_id, amount, recurring):
    prices = stripe.Price.list(product=product_id, active=True, limit=100)
    for price in prices.auto_paging_iter():
        current_interval = (price.recurring or {}).get("interval")
        expected_interval = (recurring or {}).get("interval")
        if (
            price.currency == "usd"
            and price.unit_amount == amount
            and current_interval == expected_interval
        ):
            return price
    values = {
        "product": product_id,
        "currency": "usd",
        "unit_amount": amount,
        "lookup_key": f"vurenn_{catalog_id}_v1",
        "metadata": {"vurenn_catalog_id": catalog_id},
    }
    if recurring:
        values["recurring"] = recurring
    return stripe.Price.create(**values)


def main():
    secret = os.environ.get("STRIPE_SECRET_KEY")
    if not secret:
        raise SystemExit("STRIPE_SECRET_KEY is not configured.")
    stripe.api_key = secret
    result = {}
    for catalog_id, (name, amount, recurring) in CATALOG.items():
        product = product_for(catalog_id, name)
        price = price_for(catalog_id, product.id, amount, recurring)
        result[catalog_id] = price.id
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
