from phantom.config import load_strategy
from phantom.jev import LEGEND, build_state, parse_answers, questions
from phantom.features import SNAPSHOT_FIELDS

ANSWERS = {
    "regime": {"type": "choice", "choice": "trending_up", "confidence": 0.6,
               "probabilities": {"trending_up": 0.6, "trending_down": 0.1, "ranging": 0.2, "choppy": 0.1}},
    "direction": {"type": "choice", "choice": "up", "confidence": 0.55, "probabilities": {"up": 0.55, "down": 0.2, "sideways": 0.25}},
    "buying_pressure_real": {"type": "noul", "noul": 0.71},
    "setup_quality": {"type": "score", "score": 2.8, "confidence": 0.5, "legend": {0: "a"}, "probabilities": {0: 1.0}},
    "risk_state": {"type": "choice", "choice": "normal", "confidence": 0.8, "probabilities": {"normal": 0.8, "elevated": 0.15, "extreme": 0.05}},
    "tp_before_sl": {"type": "noul", "noul": 0.62},
}


def test_parse_answers():
    v = parse_answers(ANSWERS)
    assert (v.regime_up, v.dir_up, v.pressure, v.quality, v.extreme, v.tp) == (0.6, 0.55, 0.71, 2.8, 0.05, 0.62)
    assert v.regime_top == "trending_up" and v.risk_top == "normal"


def test_questions_cover_every_answer_and_legend_covers_every_field():
    assert set(questions(load_strategy("config/strategy.yaml"))) == set(ANSWERS)
    assert set(LEGEND) == set(SNAPSHOT_FIELDS)


def test_state_carries_no_free_text_from_outside():
    state = build_state({k: 0.0 for k in SNAPSHOT_FIELDS})
    assert set(state) == {"description", "fields", "snapshot"}
    assert all(isinstance(v, float) for v in state["snapshot"].values())
