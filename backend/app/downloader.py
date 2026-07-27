"""YouTube 音訊下載。

用 pytubefix 而非 yt-dlp：YouTube 對 yt-dlp 的多數 client 強制 SABR streaming，
拿不到直接下載 URL；少數能拿到 URL 的 client（android_vr）實際抓取時回 403。
pytubefix 目前能解到 non-SABR 的 itag 140 音訊流。
"""

from pathlib import Path

from pytubefix import YouTube


def download_audio(url: str, out_dir: Path) -> Path:
    """下載影片音訊，回傳存檔路徑。檔名為 video_id.m4a。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    yt = YouTube(url)
    stream = yt.streams.get_audio_only()
    if stream is None:
        raise RuntimeError(f"no audio stream available for {url}")
    return Path(stream.download(output_path=str(out_dir), filename=f"{yt.video_id}.m4a"))


if __name__ == "__main__":
    import sys

    path = download_audio(sys.argv[1], Path(sys.argv[2]))
    print(f"Saved: {path} ({path.stat().st_size / 1024 / 1024:.1f} MiB)")
