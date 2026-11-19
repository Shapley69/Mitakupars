from pathlib import Path
from . import utils
class YoutubeDL:
    last_options = None; behavior = "ok"
    def __init__(self, options): self.options = options; YoutubeDL.last_options = options
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def extract_info(self, url, download=True):
        if YoutubeDL.behavior == "error": raise utils.DownloadError("ERROR: Unsupported URL: " + url)
        out = Path(self.options["outtmpl"].replace("%(id)s", "vid1").replace("%(ext)s", "mp4"))
        for hook in self.options["progress_hooks"]:
            hook({"status": "downloading", "downloaded_bytes": 5, "total_bytes": 10})
        out.write_bytes(b"v" * 2048)
        return {"id": "vid1", "requested_downloads": [{"filepath": str(out)}]}
    def prepare_filename(self, info): return ""
