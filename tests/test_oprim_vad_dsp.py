"""Phase 1 triage 定向测试: vad 内联 DSP 与 audio canonical 实现行为一致。

锁定 3O vite 决议: veya/oprim/vad.py 不得 import veya.oprim.audio
(oprim -> oprim 禁止互调); 内联的三个纯函数必须与 audio.py 的 canonical
实现逐值一致, 防漂移。
"""

from __future__ import annotations

import struct

from veya.oprim import audio as audio_mod
from veya.oprim import vad as vad_mod
from veya.oprim.types import AudioFrame, VADState


def _pcm_frame(*samples: int, sample_rate: int = 16000) -> AudioFrame:
    return AudioFrame(data=struct.pack(f"<{len(samples)}h", *samples), sample_rate=sample_rate)


def test_local_helpers_match_canonical_audio_impls():
    pcm = struct.pack("<6h", 0, 1000, -1000, 32767, -32768, 123)
    assert vad_mod._bytes_to_int16(pcm) == audio_mod.bytes_to_int16(pcm)
    assert vad_mod._bytes_to_int16(b"") == audio_mod.bytes_to_int16(b"")

    samples = vad_mod._bytes_to_int16(pcm)
    assert vad_mod._compute_rms(samples) == audio_mod.compute_rms(samples)
    assert vad_mod._compute_rms([]) == audio_mod.compute_rms([]) == 0.0

    for rms in (0.0, -1.0, 1.0, 100.0, 32767.0):
        assert vad_mod._linear_to_db(rms) == audio_mod.linear_to_db(rms)


def test_vad_energy_silence_and_speech():
    silence = _pcm_frame(*([0] * 320))
    loud = _pcm_frame(*([16000] * 320))

    quiet_result = vad_mod.vad_energy(silence)
    assert quiet_result.state == VADState.SILENCE
    assert not quiet_result.is_speech

    loud_result = vad_mod.vad_energy(loud)
    assert loud_result.is_speech
    assert loud_result.energy_db > -40.0


def test_vad_module_has_no_oprim_peer_import():
    import ast
    import pathlib

    tree = ast.parse(
        pathlib.Path(vad_mod.__file__).read_text(encoding="utf-8"),
        filename=str(vad_mod.__file__),
    )
    peer_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            peer_imports.extend(a.name for a in node.names if a.name.startswith("veya.oprim."))
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and (node.module or "").startswith("veya.oprim.")
            and (node.module or "") != "veya.oprim.types"
        ):
            peer_imports.append(node.module or "")
    assert peer_imports == [], f"oprim peer imports found: {peer_imports}"
