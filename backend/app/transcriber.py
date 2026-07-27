"""faster-whisper 本機語音轉錄。

輸出 JSON：完整逐字稿 + 帶時間軸的 segments，供後續股票代號校正使用。
"""

import json
import sys
from pathlib import Path

from faster_whisper import WhisperModel

DEFAULT_MODEL = "small"


def transcribe(audio_path: Path, model_size: str = DEFAULT_MODEL) -> Path:
    """轉錄音訊，結果寫成同名 .json，回傳該路徑。"""
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(str(audio_path), language="zh", vad_filter=True)

    result = {"language": info.language, "duration": info.duration, "segments": []}
    parts = []
    for seg in segments:
        result["segments"].append({"start": seg.start, "end": seg.end, "text": seg.text})
        parts.append(seg.text)
        # 進度回報寫 stderr，避免混進 stdout；console 編碼撐不住的字元以 ? 代替
        print(f"[{seg.end:7.1f}s / {info.duration:.0f}s]", file=sys.stderr, flush=True)

    result["text"] = "".join(parts)
    out_path = audio_path.with_suffix(".json")
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


if __name__ == "__main__":
    path = transcribe(Path(sys.argv[1]))
    print(f"Saved: {path}")
