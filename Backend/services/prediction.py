"""Prediction strategy and adapter; preserve the existing model mathematics."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PredictionFeatures:
    age: int
    distance_miles: float
    lead_time_days: int
    previous_no_shows: int


class NoShowPredictor(ABC):
    @property
    def requires_features(self) -> bool:
        return True

    @abstractmethod
    def predict_probability(self, features: PredictionFeatures) -> float:
        """Return no-show risk in percent, not a fraction."""
        raise NotImplementedError


class SklearnNoShowAdapter(NoShowPredictor):
    """Adapt scaler/model APIs to the domain's single prediction operation."""

    def __init__(self, model, scaler):
        self._model = model
        self._scaler = scaler

    def predict_probability(self, features: PredictionFeatures) -> float:
        import numpy as np
        values = np.array([[features.age, features.distance_miles,
                            features.lead_time_days, features.previous_no_shows]])
        scaled = self._scaler.transform(values)
        # Preserve legacy class-column selection, scaling and rounding exactly.
        return float(round(self._model.predict_proba(scaled)[0][0] * 100, 2))


class UnavailableModelPredictor(NoShowPredictor):
    """Legacy booking fallback, NOT a clinically meaningful low-risk estimate."""

    @property
    def requires_features(self) -> bool:
        return False

    def predict_probability(self, features: PredictionFeatures) -> float:
        return 0.0


def load_booking_predictor() -> NoShowPredictor:
    """Composition helper, not a GoF Factory Method implementation."""
    import joblib
    directory = Path(__file__).resolve().parents[1]
    try:
        return SklearnNoShowAdapter(
            joblib.load(directory / "noshow_model.pkl"),
            joblib.load(directory / "scaler.pkl"),
        )
    except Exception:
        logger.warning("Booking model unavailable; preserving legacy zero-risk fallback", exc_info=True)
        return UnavailableModelPredictor()
