from datetime import datetime

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


class DashboardSummary(BaseModel):
    total_listings: int
    active_listings: int
    analytics_eligible_listings: int
    brand_count: int
    model_count: int
    average_price_usd: float
    median_price_usd: float
    last_seen_at: datetime | None


class BrandStat(BaseModel):
    brand: str
    active_count: int
    average_price_usd: float
    median_price_usd: float


class BrandItem(BaseModel):
    id: int
    name: str
    active_count: int


class ModelItem(BaseModel):
    id: int
    name: str
    active_count: int


class YearItem(BaseModel):
    year: int
    active_count: int
    analytics_eligible_count: int


class RepresentativeListing(BaseModel):
    id: int
    title: str
    image_url: str | None
    source_url: str


class AnomalyItem(BaseModel):
    id: int
    title: str
    year: int
    mileage_km: int | None
    city: str | None
    price_usd: float
    deviation_percent: float
    source_url: str


class AnomalySummary(BaseModel):
    count: int
    low_count: int
    high_count: int
    items: list[AnomalyItem]


class VehicleSummaryTrendPoint(BaseModel):
    period: str
    average_price_usd: float
    median_price_usd: float
    sample_count: int


class VehicleHeader(BaseModel):
    brand: str
    model: str
    generation: str | None
    year: int


class MileageInfo(BaseModel):
    """Что сделали с пробегом: без него честнее показать «поправки не было»."""

    requested_km: int | None
    window: str
    samples_in_window: int
    adjustment_usd: float


class VehicleSummary(BaseModel):
    vehicle: VehicleHeader
    currency: str
    estimated_price: float
    estimated_range: dict[str, float]
    market_average: float
    market_median: float
    samples: int
    confidence: str
    mileage: MileageInfo
    representative_listing: RepresentativeListing | None
    anomalies: AnomalySummary
    trend: list[VehicleSummaryTrendPoint]
