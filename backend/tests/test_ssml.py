from xml.etree import ElementTree as ET

import pytest
from pydantic import ValidationError

from jdx_backend.cloud import CloudConfig, CloudError, synthesis_input
from jdx_backend.schemas import LineSettings


def test_precise_pauses_and_pronunciation_are_escaped_real_ssml():
    settings = LineSettings(ssml=True, pause_after_words={"重庆": 500},
                            pronunciation_overrides={"重庆": "chong2 qing4"})
    _, request = synthesis_input(CloudConfig(), "重庆 & <风景>，又到重庆。", settings.model_dump())
    assert request["enable_ssml"] is True
    xml = ET.fromstring(request["text"])
    assert "".join(xml.itertext()) == "重庆 & <风景>，又到重庆。"
    assert len(xml.findall("break")) == 2
    assert all(item.attrib == {"time": "500ms"} for item in xml.findall("break"))
    assert all(item.attrib == {"alphabet": "py", "ph": "chong2 qing4"} for item in xml.findall("phoneme"))
    assert not xml.findall("风景")


def test_precise_controls_require_supported_voice_and_present_words():
    with pytest.raises(CloudError, match="启用精细控制"):
        synthesis_input(CloudConfig(), "你好", {"pause_after_words": {"你好": 500}})
    with pytest.raises(CloudError, match="不在当前台词"):
        synthesis_input(CloudConfig(), "你好", {"ssml": True, "pause_after_words": {"再见": 500}})
    with pytest.raises(CloudError, match="不支持"):
        synthesis_input(CloudConfig(voice_id="qwen-audio-3.1-tts-plus-vd-test"), "你好", {"ssml": True})


@pytest.mark.parametrize("changes", [
    {"pause_after_words": {"你好": 0}},
    {"pause_after_words": {"你好": 10001}},
    {"pronunciation_overrides": {"重庆": "chong2"}},
    {"pronunciation_overrides": {"重庆": "chong2 qing6"}},
])
def test_invalid_precise_control_settings_rejected(changes):
    with pytest.raises(ValidationError):
        LineSettings(**changes)
