import pytest

from veya.obase.channels.safe_xml import ET


def test_xml_parser_accepts_regular_payload():
    node = ET.fromstring(b"<xml><MsgType>text</MsgType></xml>")
    assert node.findtext("MsgType") == "text"


@pytest.mark.parametrize(
    "payload",
    [
        b'<!DOCTYPE x [<!ENTITY e "boom">]><xml>&e;</xml>',
        b'<!ENTITY e "boom"><xml/>',
        (b"<xml>" + b"x" * (1024 * 1024) + b"</xml>"),
    ],
)
def test_xml_parser_rejects_entity_declarations_and_oversize(payload):
    with pytest.raises((ValueError, Exception)):
        ET.fromstring(payload)
