"""Production adapter for the historical AML predictor demo."""

from __future__ import annotations

import json
from pathlib import Path

from predictor import AMLPredictor


def main_process(
    customer_json: str,
    transactions_json: str,
    country_risk_mapping_json: str = "{}",
) -> dict[str, str]:
    """Predict one customer's anti-money-laundering risk from JSON records.

    Args:
        customer_json: JSON object containing the customer profile.
        transactions_json: JSON array containing the customer's transactions.
        country_risk_mapping_json: JSON object mapping country codes to risk scores.

    Returns:
        Stable customer risk classification fields as strings.
    """
    customer = json.loads(customer_json)
    transactions = json.loads(transactions_json)
    country_risk_mapping = json.loads(country_risk_mapping_json)
    if not isinstance(customer, dict):
        raise ValueError("customer_json must decode to an object")
    if not isinstance(transactions, list):
        raise ValueError("transactions_json must decode to an array")
    if not isinstance(country_risk_mapping, dict):
        raise ValueError("country_risk_mapping_json must decode to an object")

    model_path = Path(__file__).resolve().parent / "models" / "aml_model_random_forest.pkl"
    predictor = AMLPredictor(str(model_path))
    result = predictor.predict_customer_risk(
        customer,
        transactions,
        country_risk_mapping,
    )
    return {
        "customer_id": str(result["customer_id"]),
        "is_suspicious": str(result["is_suspicious"]),
        "suspicious_probability": str(result["suspicious_probability"]),
        "risk_level": str(result["risk_level"]),
    }
