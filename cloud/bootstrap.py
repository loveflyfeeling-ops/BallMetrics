# -*- coding: utf-8 -*-
"""第一次建立加密狀態檔：把電腦版 data/ 裡的歷史資料、模型、賽事打包加密。
用法：python cloud/bootstrap.py <電腦版 data 資料夾> <輸出 state.enc>   （金鑰取自環境變數 STATE_KEY）"""
import io, json, os, sys, tarfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import crypto

src, out = Path(sys.argv[1]), Path(sys.argv[2])
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w:gz") as tf:
    for name in ("history", "models", "games.json"):
        if (src / name).exists():
            tf.add(src / name, arcname=name)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(crypto.encrypt(buf.getvalue(), os.environ["STATE_KEY"])))
print(f"已建立 {out}（{out.stat().st_size / 1024:.0f} KB）")
