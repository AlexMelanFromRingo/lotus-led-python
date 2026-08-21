"""Protocol tests.

The expectations here are transcribed from the vendor Android app and are the
same ones pinned in ``lotus-led/src/protocol.rs``. If a byte changes on one
side without the other, one of these fails.
"""

import pytest

from lotus_led.protocol import (
    FRAME_HEAD,
    FRAME_TAIL,
    HWMode,
    LightChannel,
    MicEq,
    Pkt,
    cct_to_rgb,
    hsv_to_rgb,
    hw_mode_from_code,
    hw_mode_from_name,
    lerp_color,
    parse_color,
    rgb_to_hsv,
    scale_color,
)


def all_packets():
    packets = [
        Pkt.power_on(), Pkt.power_off(),
        Pkt.power_channel(0xE0, LightChannel.RGB, True),
        Pkt.color(1, 2, 3), Pkt.color_streaming(1, 2, 3),
        Pkt.cct(40, 60), Pkt.single_color(2),
        Pkt.brightness(50), Pkt.brightness(50, LightChannel.RGB),
        Pkt.speed(50),
        Pkt.mic_enabled(True), Pkt.mic_sensitivity(70),
        Pkt.pin_order(1, 2, 3),
        Pkt.laser(5), Pkt.laser_mode(1), Pkt.laser_speed(50),
        Pkt.system_time(12, 30, 0), Pkt.timer(23, 0, 0, False),
        Pkt.countdown(3600),
    ]
    packets += [Pkt.hw_mode(m) for m in HWMode]
    packets += [Pkt.mic_eq(e) for e in MicEq]
    return packets


@pytest.mark.parametrize("packet", all_packets())
def test_every_packet_is_a_well_formed_frame(packet):
    """A malformed frame is dropped by the controller without any error, so
    this invariant is the cheapest bug net available."""
    assert len(packet) == 9
    assert packet[0] == FRAME_HEAD
    assert packet[8] == FRAME_TAIL


def test_packets_match_the_vendor_app():
    """Byte-for-byte against BluetoothLEService in Lotus Lantern 6.5.08."""
    assert list(Pkt.power_on()) == [0x7E, 0x04, 0x04, 0x01, 0x00, 0x01, 0xFF, 0x00, 0xEF]
    assert list(Pkt.power_off()) == [0x7E, 0x04, 0x04, 0x00, 0x00, 0x00, 0xFF, 0x00, 0xEF]
    assert list(Pkt.color(0xAA, 0xBB, 0xCC)) == [0x7E, 0x07, 0x05, 0x03, 0xAA, 0xBB, 0xCC, 0x10, 0xEF]
    assert list(Pkt.color_streaming(0xAA, 0xBB, 0xCC)) == [0x7E, 0x07, 0x05, 0x03, 0xAA, 0xBB, 0xCC, 0x20, 0xEF]
    assert list(Pkt.brightness(100)) == [0x7E, 0x04, 0x01, 100, 0xFF, 0xFF, 0xFF, 0x00, 0xEF]
    assert list(Pkt.brightness(50, LightChannel.RGB)) == [0x7E, 0x04, 0x01, 50, 0x01, 0xFF, 0xFF, 0x00, 0xEF]
    assert list(Pkt.speed(80)) == [0x7E, 0x04, 0x02, 80, 0xFF, 0xFF, 0xFF, 0x00, 0xEF]
    assert list(Pkt.cct(30, 70)) == [0x7E, 0x06, 0x05, 0x02, 30, 70, 0xFF, 0x08, 0xEF]
    assert list(Pkt.mic_enabled(True)) == [0x7E, 0x04, 0x07, 0x01, 0xFF, 0xFF, 0xFF, 0x00, 0xEF]
    assert list(Pkt.mic_sensitivity(60)) == [0x7E, 0x04, 0x06, 60, 0xFF, 0xFF, 0xFF, 0x00, 0xEF]
    assert list(Pkt.mic_eq(MicEq.DISCO)) == [0x7E, 0x05, 0x03, 0x83, 0x04, 0xFF, 0xFF, 0x00, 0xEF]
    assert list(Pkt.pin_order(1, 2, 3)) == [0x7E, 0x06, 0x81, 1, 2, 3, 0xFF, 0x00, 0xEF]
    assert list(Pkt.single_color(4)) == [0x7E, 0x05, 0x05, 0x01, 4, 0xFF, 0xFF, 0x08, 0xEF]


def test_hw_mode_frame_has_the_shape_the_firmware_accepts():
    """The bug that made firmware animations silently do nothing in every
    earlier version: the command byte must be 0x05 and the padding FF FF 00."""
    assert list(Pkt.hw_mode(HWMode.STROBE_7_COLOR)) == [0x7E, 0x05, 0x03, 0x95, 0x03, 0xFF, 0xFF, 0x00, 0xEF]
    assert list(Pkt.hw_mode(HWMode.CROSS_FADE_7_COLOR)) == [0x7E, 0x05, 0x03, 0x8A, 0x03, 0xFF, 0xFF, 0x00, 0xEF]


def test_opcodes_are_contiguous_from_0x80():
    modes = list(HWMode)
    assert len(modes) == 29
    for index, mode in enumerate(modes):
        assert mode.code == 0x80 + index, f"{mode.mode_name} is out of order"
        assert mode.index == index
        assert hw_mode_from_code(mode.code) is mode
    assert modes[0].code == 0x80
    assert modes[-1].code == 0x9C
    assert hw_mode_from_code(0x7F) is None
    assert hw_mode_from_code(0x9D) is None


def test_mode_names_are_unique_and_round_trip():
    seen = set()
    for mode in HWMode:
        assert mode.mode_name not in seen
        seen.add(mode.mode_name)
        assert hw_mode_from_name(mode.mode_name) is mode
        assert hw_mode_from_name(mode.mode_name.upper()) is mode
        assert hw_mode_from_name(mode.mode_name.replace("_", "-")) is mode
    assert hw_mode_from_name("nonsense") is None


def test_legacy_mode_aliases_still_resolve():
    """Configs written against the old (wrong) tables must keep working."""
    assert hw_mode_from_name("fade_7_color") is HWMode.CROSS_FADE_7_COLOR
    assert hw_mode_from_name("FADE_7_COLOR") is HWMode.CROSS_FADE_7_COLOR
    assert hw_mode_from_name("strobe7") is HWMode.STROBE_7_COLOR
    assert hw_mode_from_name("jump7") is HWMode.JUMP_7_COLOR


def test_levels_are_clamped():
    assert Pkt.brightness(255)[3] == 100
    assert Pkt.speed(255)[3] == 100
    assert Pkt.mic_sensitivity(200)[3] == 100
    assert Pkt.pin_order(0, 9, 2)[3] == 1
    assert Pkt.pin_order(0, 9, 2)[4] == 3


def test_raw_rejects_wrong_lengths():
    assert len(Pkt.raw([0x7E] + [0] * 7 + [0xEF])) == 9
    with pytest.raises(ValueError, match="9 bytes"):
        Pkt.raw([0x7E, 0xEF])


@pytest.mark.parametrize("rgb", [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 128, 0), (10, 20, 30), (0, 0, 0)])
def test_hsv_round_trips_through_rgb(rgb):
    back = hsv_to_rgb(*rgb_to_hsv(*rgb))
    assert all(abs(a - b) <= 1 for a, b in zip(rgb, back)), f"{rgb} -> {back}"


def test_lerp_hits_both_endpoints_and_clamps():
    a, b = (0, 50, 100), (200, 150, 0)
    assert lerp_color(a, b, 0.0) == a
    assert lerp_color(a, b, 1.0) == b
    assert lerp_color(a, b, -5.0) == a
    assert lerp_color(a, b, 5.0) == b


def test_scale_color_dims_proportionally():
    assert scale_color((200, 100, 50), 100) == (200, 100, 50)
    assert scale_color((200, 100, 50), 50) == (100, 50, 25)
    assert scale_color((200, 100, 50), 0) == (0, 0, 0)


def test_cct_runs_warm_to_cool():
    warm_r, _, warm_b = cct_to_rgb(2000)
    cool_r, _, cool_b = cct_to_rgb(9000)
    assert warm_r > warm_b
    assert cool_b >= cool_r


@pytest.mark.parametrize(
    "value,expected",
    [
        ("#FF8000", (255, 128, 0)),
        ("ff8000", (255, 128, 0)),
        ("#f80", (255, 136, 0)),
        ("255 136 0", (255, 136, 0)),
        ("255,136,0", (255, 136, 0)),
        ([255, 136, 0], (255, 136, 0)),
        ((255, 136, 0), (255, 136, 0)),
    ],
)
def test_colors_parse_in_every_accepted_spelling(value, expected):
    assert parse_color(value) == expected


@pytest.mark.parametrize("value", ["zzz", "#12345", [1, 2], [1, 2, 3, 4], [300, 0, 0], 42])
def test_bad_colors_are_rejected_with_a_message(value):
    with pytest.raises(ValueError):
        parse_color(value)
