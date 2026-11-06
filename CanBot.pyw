"""CanBot masaüstü uygulaması (Windows) — kaynak koddan çalıştırma.

Çift tıklayınca konsol (cmd) penceresi açılmadan başlar. Önce Kurulum.bat'ı bir kez çalıştırın.
Hazır programı tercih ederseniz CanBot.exe / CanBot-Kurulum.exe kullanın (README'ye bakın).
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PYW = ROOT / ".venv" / "Scripts" / "pythonw.exe"

# Kurulum.bat'ın oluşturduğu sanal ortam varsa program onunla yeniden başlatılır.
if VENV_PYW.exists() and Path(sys.executable).resolve() != VENV_PYW.resolve():
    subprocess.Popen([str(VENV_PYW), str(Path(__file__).resolve()), *sys.argv[1:]], cwd=str(ROOT))
    sys.exit(0)

sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from run import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from bot.desktop import main  # noqa: E402

sys.exit(main())
