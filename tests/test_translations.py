"""Every state an enum sensor or select can report has a translation.

Hassfest only checks that state keys are well formed; this pins them to what
the entities actually report, so the two can't drift apart.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from homeassistant.components.sensor import SensorDeviceClass

from custom_components.lumagen.select import SELECTS
from custom_components.lumagen.sensor import SENSORS

_COMPONENT = Path(__file__).parent.parent / "custom_components" / "lumagen"
_EN = json.loads((_COMPONENT / "translations" / "en.json").read_text())

_SELECT_TRANSLATIONS = _EN["entity"]["select"]

# Enum sensors always translate their states; selects only where they opt in
# (aspect and memory show their options as-is).
_TRANSLATED = [("sensor", d) for d in SENSORS if d.device_class is SensorDeviceClass.ENUM] + [
    ("select", d) for d in SELECTS if "state" in _SELECT_TRANSLATIONS.get(d.translation_key, {})
]


@pytest.mark.parametrize(
    ("platform", "description"),
    _TRANSLATED,
    ids=[d.key for _, d in _TRANSLATED],
)
def test_every_option_has_a_state_translation(platform: str, description) -> None:
    states = _EN["entity"][platform][description.translation_key].get("state", {})
    assert set(description.options or []) == set(states)


def test_strings_json_matches_english_translation() -> None:
    assert json.loads((_COMPONENT / "strings.json").read_text()) == _EN
