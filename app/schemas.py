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


class AdminLoginRequest(BaseModel):
    token: str


class AdminMe(BaseModel):
    telegram_id: int
    username: str | None
    first_name: str | None
    session_expires_at: datetime


class AdminUserItem(BaseModel):
    id: int
    telegram_id: int
    username: str | None
    first_name: str | None
    access_status: str
    is_superuser: bool
    # Срок, который что-то значит для этого пользователя: триалу — свой,
    # подписчику — свой, члену группы — ничего.
    expires_at: datetime | None
    subscription_active: bool
    trial_active: bool
    filters_count: int
    blocks_count: int
    payments_count: int
    last_event_at: datetime | None


class AdminUserPage(BaseModel):
    items: list[AdminUserItem]
    total: int
    limit: int
    offset: int
    # Сколько людей в каждом статусе при текущем поиске — для чипов-фильтров.
    status_counts: dict[str, int]


class AdminUserAccess(BaseModel):
    status: str
    in_group: bool
    checked_at: datetime | None
    subscription_active: bool
    subscription_expires_at: datetime | None
    trial_used: bool
    trial_active: bool
    trial_started_at: datetime | None
    trial_expires_at: datetime | None


class AdminUserShort(BaseModel):
    id: int
    telegram_id: int
    username: str | None


class AdminUserReferral(BaseModel):
    invite_code: str | None
    invited_count: int
    bonuses_granted: int
    referrer: AdminUserShort | None


class AdminUserFilter(BaseModel):
    id: int
    enabled: bool
    label: str


class AdminUserBlock(BaseModel):
    id: int
    kind: str
    label: str


class AdminUserPayment(BaseModel):
    id: int
    amount: float
    currency: str
    status: str
    period_days: int
    created_at: datetime | None
    paid_at: datetime | None


class AdminUserEvent(BaseModel):
    event: str
    created_at: datetime | None


class AdminUserNotifications(BaseModel):
    sent_count: int
    last_sent_at: datetime | None


class AdminUserDetail(BaseModel):
    id: int
    telegram_id: int
    username: str | None
    first_name: str | None
    access_status: str
    is_superuser: bool
    is_active: bool
    created_at: datetime | None
    access: AdminUserAccess
    referral: AdminUserReferral
    filters: list[AdminUserFilter]
    blocks: list[AdminUserBlock]
    payments: list[AdminUserPayment]
    events: list[AdminUserEvent]
    notifications: AdminUserNotifications


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
