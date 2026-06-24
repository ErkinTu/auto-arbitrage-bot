from pydantic import BaseModel


class EstimateResponse(BaseModel):
    currency: str
    estimated_price: float
    estimated_range: dict[str, float]
    market_average: float
    market_median: float
    samples: int
    confidence: str
    used_mileage_window: str
    criteria: dict


class TrendPoint(BaseModel):
    date: str
    average_price_usd: float
    sample_count: int


class TrendResponse(BaseModel):
    mileage_bucket: str
    points: list[TrendPoint]
