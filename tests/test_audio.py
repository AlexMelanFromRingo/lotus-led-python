"""Spectral analysis tests. No audio hardware is touched."""

import numpy as np
import pytest

from lotus_led.audio import FFT_SIZE, BeatDetector, PeakFollower, Spectrum, smooth

SR = 48000.0


def tone(freq, length=FFT_SIZE):
    return np.sin(2 * np.pi * freq * np.arange(length) / SR).astype(np.float32)


def test_a_pure_tone_lands_in_the_right_band():
    """The whole basis of the spectrum mode's colour mapping."""
    spectrum = Spectrum(SR)

    assert spectrum.analyze(tone(100))
    bass, mid, treble = spectrum.band(20, 250), spectrum.band(250, 4000), spectrum.band(4000, 16000)
    assert bass > mid and bass > treble, f"100 Hz: {bass=} {mid=} {treble=}"

    assert spectrum.analyze(tone(8000))
    bass, mid, treble = spectrum.band(20, 250), spectrum.band(250, 4000), spectrum.band(4000, 16000)
    assert treble > bass and treble > mid, f"8 kHz: {bass=} {mid=} {treble=}"


def test_silence_produces_no_energy():
    spectrum = Spectrum(SR)
    assert spectrum.analyze(np.zeros(FFT_SIZE, dtype=np.float32))
    assert spectrum.band(20, 16000) < 1e-6


@pytest.mark.parametrize("samples", [np.zeros(0, dtype=np.float32), np.zeros(3, dtype=np.float32), None])
def test_short_input_is_rejected_rather_than_analysed_as_noise(samples):
    assert Spectrum(SR).analyze(samples) is False


def test_peak_follower_normalises_away_the_volume():
    """Volume independence: the same signal at two amplitudes must settle to
    the same normalised level."""

    def settle(amplitude):
        follower = PeakFollower()
        value = 0.0
        for _ in range(200):
            value = follower.normalize(amplitude)
        return value

    quiet, loud = settle(0.01), settle(1.0)
    assert abs(quiet - loud) < 1e-3, f"{quiet=} {loud=}"
    assert quiet > 0.9


def test_peak_follower_clamps_and_never_divides_by_zero():
    follower = PeakFollower()
    assert follower.normalize(0.0) == 0.0
    assert 0.0 <= follower.normalize(1e9) <= 1.0


def test_smoothing_converges_towards_the_target():
    value = 0.0
    for _ in range(200):
        value = smooth(value, 1.0, 0.5)
    assert abs(value - 1.0) < 1e-3
    assert smooth(0.0, 1.0, 0.0) == 1.0


def test_beat_detector_needs_a_transient_not_just_loudness():
    """Steady loud music is not a beat; a spike above the running mean is."""
    detector = BeatDetector(threshold=1.35, min_gap_s=0.0)
    now = 0.0
    for _ in range(20):
        now += 0.05
        assert not detector.feed(1.0, now), "a constant level must not read as beats"
    now += 0.05
    assert detector.feed(3.0, now), "a spike should read as a beat"


def test_beat_detector_honours_the_minimum_gap():
    """One kick must not be counted several times as it decays."""
    detector = BeatDetector(threshold=1.2, min_gap_s=0.2)
    now = 0.0
    for _ in range(10):
        now += 0.02
        detector.feed(1.0, now)
    now += 0.02
    assert detector.feed(5.0, now)
    now += 0.02
    assert not detector.feed(5.0, now), "too soon after the last beat"
    now += 0.5
    assert detector.feed(5.0, now)
